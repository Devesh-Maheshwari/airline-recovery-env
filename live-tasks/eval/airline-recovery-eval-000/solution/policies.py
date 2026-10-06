"""Observation-only baselines and the small public policy plugin contract.

These are scripted policies, not trained models. They deliberately have no imports
from the environment, fault injector, scenario catalogue or verifier.
"""
from __future__ import annotations

from collections import deque
import importlib
import json
import uuid
from typing import Any, Callable

if __package__:
    from .oracle import OraclePolicy
else:  # a Harbor solution/ folder imports this file as a top-level module beside oracle.py
    from oracle import OraclePolicy

Action = dict[str, Any]
Policy = Callable[[dict[str, Any]], Action]


def action(tool: str, **arguments: Any) -> Action:
    return {"tool": tool, "arguments": arguments}


def nop(observation: dict[str, Any]) -> Action:
    """Observe eight traffic windows without repairs, including delayed incidents.

    Immediate finish would fail the verification horizon even on healthy services,
    making it a misleading control. This control can pass a healthy environment.
    """
    return action("finish" if observation.get("step", 0) >= 8 else "probe")


def load_policy(spec: str) -> Policy:
    """Load a callable, never passing it an environment or private reset metadata."""
    if spec == "nop":
        return nop
    if spec == "reference":
        return ReferencePolicy()
    if spec == "reference-adopt":
        return ReferencePolicy(recovery="adopt")
    if spec == "blanket":
        return BlanketPolicy()
    if spec == "source-aware":
        return SourceAwarePolicy()
    if spec == "oracle":
        return OraclePolicy()
    if spec == "blanket-hard":
        return BlanketHardPolicy()
    if spec == "adopt-else-reconcile":
        return AdoptElseReconcilePolicy()
    if spec == "wait-then-reconcile":
        return WaitThenReconcilePolicy()
    module_name, separator, name = spec.partition(":")
    if not separator or not module_name or not name:
        raise ValueError("policy must be 'nop', 'blanket', 'blanket-hard', 'source-aware', 'reference', 'reference-adopt', "
                         "'oracle', 'adopt-else-reconcile', 'wait-then-reconcile', or 'importable.module:callable'")
    obj = getattr(importlib.import_module(module_name), name)
    if not callable(obj):
        raise TypeError(f"{spec} is not callable")
    return obj


def _rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        for key in ("rows", "data", "result", "logs", "events"):
            if key in result:
                return _rows(result[key])
    return []


def _unwrap(result: Any) -> Any:
    while isinstance(result, dict):
        if "data" in result and "tool" in result:
            result = result["data"]
        elif "result" in result:
            result = result["result"]
        else:
            break
    return result


class BlanketPolicy:
    """The v0.2 blanket baseline, retained as a reproducible adverse control."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.queue = deque([
            action("patch_config", service="booking", values={"payment_timeout_ms":10000}),
            action("restart_service", service="pricing"),
            action("restart_service", service="payment"),
            action("invalidate_cache", service="pricing"),
            action("patch_config", service="checkin", values={"consumer_enabled":True,
                   "accepted_schema":2, "batch_size":50}),
            action("query_sql", query="SELECT 'booking' AS kind, booking_id AS id FROM bookings WHERE status='pending' "
                   "UNION ALL SELECT 'event' AS kind, event_id AS id FROM outbox "
                   "WHERE status='pending' AND NOT json_valid(payload) ORDER BY kind,id"),
        ])
        self.looked_up = False

    def __call__(self, observation):
        if not self.queue and not self.looked_up:
            result = observation["result"]
            if not result["ok"] or not isinstance(result["data"], list):
                raise RuntimeError("The public ID lookup did not return rows")
            for row in result["data"]:
                if row["kind"] == "booking":
                    self.queue.append(action("reconcile_booking", booking_id=row["id"]))
                elif row["kind"] == "event":
                    self.queue.append(action("quarantine_event", event_id=row["id"]))
                else:
                    raise RuntimeError("Unexpected public lookup row")
            self.queue.extend([action("probe"), action("probe"), action("finish")])
            self.looked_up = True
        return self.queue.popleft()


class BlanketHardPolicy(BlanketPolicy):
    """Blanket plus restart-all, unscoped invalidation and reconcile-all-pending.

    An adverse control for the hard tier: it restarts the payment worker with
    captures in flight, evicts a held fare's quote, and resumes cancelled or
    duplicate bookings as if they were ordinary interrupted sales.
    """
    def reset(self):
        super().reset()
        self.queue = deque([
            action("patch_config", service="booking", values={"payment_timeout_ms":10000}),
            *(action("restart_service", service=name) for name in ("inventory", "pricing", "payment", "booking", "checkin")),
            action("invalidate_cache", service="pricing"),
            action("patch_config", service="checkin", values={"consumer_enabled":True,
                   "accepted_schema":2, "batch_size":50}),
            self.queue[-1],
        ])


class SourceAwarePolicy(BlanketPolicy):
    """Strong control: infer first charge IDs, try adoption, then ordinary retry.

    It queries pending IDs but never reads the charge ledger. This deliberately
    exploits knowledge of the public source and is a legitimate safe baseline.
    A high score is not evidence of diagnosis or learned improvement.
    """
    def __call__(self, observation):
        if not self.queue and not self.looked_up:
            result = observation["result"]
            if not result["ok"] or not isinstance(result["data"], list):
                raise RuntimeError("The public ID lookup did not return rows")
            for row in result["data"]:
                if row["kind"] == "booking":
                    booking_id = row["id"]
                    charge_id = "chg_" + uuid.uuid5(uuid.NAMESPACE_URL, f"airline-recovery:charge:{booking_id}:1").hex
                    self.queue.extend([
                        action("reconcile_booking", booking_id=booking_id, existing_charge_id=charge_id),
                        action("reconcile_booking", booking_id=booking_id),
                    ])
                elif row["kind"] == "event":
                    self.queue.append(action("quarantine_event", event_id=row["id"]))
                else:
                    raise RuntimeError("Unexpected public lookup row")
            self.queue.extend([action("probe"), action("probe"), action("finish")])
            self.looked_up = True
        return self.queue.popleft()


class AdoptElseReconcilePolicy(OraclePolicy):
    """One-branch control: the oracle's settings work, but every pending booking
    with a charge is adopted and every other one is plainly retried. It never
    voids, never consults the provider and never waits for settlement."""
    def _decide(self, row, at_step):
        booking_id = row.get("booking_id")
        if not booking_id:
            return None
        charges = self._charges(row)
        if charges and charges[0].get("charge_id") and not self.failed.get(booking_id):
            return action("reconcile_booking", booking_id=booking_id, existing_charge_id=charges[0]["charge_id"])
        return action("reconcile_booking", booking_id=booking_id)


class WaitThenReconcilePolicy(OraclePolicy):
    """One-branch control: the oracle's settings work, then it waits half the
    budget for the provider to settle and plainly retries every pending booking
    with the deployed key. It never voids, adopts or consults the provider."""
    def __init__(self, max_rounds: int = 3, wait_steps: int | None = None):
        self.wait_steps = wait_steps
        super().__init__(max_rounds)

    def reset(self):
        super().reset()
        self.wait_until = None

    def _decide(self, row, at_step):
        if self.wait_until is None:
            remaining = self.summary.get("remaining_actions")
            budget = self.wait_steps if self.wait_steps is not None else (
                (remaining + self.step) // 2 if isinstance(remaining, int) else 12)
            self.wait_until = budget
        if self.step < self.wait_until:
            self.deferred[row.get("booking_id") or ""] = "waiting"
            return None
        return action("reconcile_booking", booking_id=row["booking_id"]) if row.get("booking_id") else None


class ReferencePolicy:
    """A bounded diagnostic workflow driven by public measurements and SQL.

    Rules follow service contracts: committed payments need idempotency, accepted
    schema must cover observed events, and an outbox with a malformed head cannot
    advance. This is an inspectable baseline, not an optimal solver or oracle.
    """

    PENDING_SQL = (
        "SELECT b.booking_id, b.request_id, b.flight_id, b.passenger_id, b.status, "
        "(SELECT COUNT(*) FROM charges c WHERE c.booking_id = b.booking_id) AS charge_count, "
        "(SELECT c.idempotency_key FROM charges c WHERE c.booking_id = b.booking_id LIMIT 1) AS idempotency_key, "
        "(SELECT c.charge_id FROM charges c WHERE c.booking_id = b.booking_id LIMIT 1) AS charge_id "
        "FROM bookings b JOIN holds h ON h.booking_id = b.booking_id "
        "WHERE b.status = 'pending' ORDER BY b.booking_id LIMIT 50"
    )
    OUTBOX_SQL = (
        "SELECT event_id, booking_id, payload, status, attempts "
        "FROM outbox WHERE status = 'pending' ORDER BY event_id LIMIT 50"
    )

    def __init__(self, max_rounds: int = 3, recovery: str = "retry"):
        if recovery not in {"retry", "adopt"}:
            raise ValueError("recovery must be retry or adopt")
        self.recovery = recovery
        self.max_rounds = max_rounds
        self.reset()

    def reset(self) -> None:
        self.queue: deque[Action] = deque()
        self.last_action: Action | None = None
        self.observed: dict[str, Any] = {}
        self.round = 0
        self.started = False
        self._inspect()

    def _inspect(self) -> None:
        self.queue.extend([
            action("get_metrics"), action("get_logs"), action("get_config"),
            action("query_sql", query=self.PENDING_SQL),
            action("query_sql", query=self.OUTBOX_SQL),
        ])

    def __call__(self, observation: dict[str, Any]) -> Action:
        # A caller can reuse the object directly without remembering reset().
        if observation.get("step") == 0 and self.started:
            self.reset()
        self.started = True
        if self.last_action is not None:
            key = self.last_action["tool"]
            if key == "query_sql":
                key = "pending" if self.last_action["arguments"]["query"] == self.PENDING_SQL else "outbox"
            self.observed[key] = _unwrap(observation.get("result"))
        if not self.queue:
            if self.last_action and self.last_action["tool"] == "probe":
                probe = self.observed.get("probe") or {}
                if isinstance(probe, dict) and probe.get("healthy"):
                    self.queue.append(action("finish") if probe.get("verification_windows", 0) >= 2 else action("probe"))
                elif self.round >= self.max_rounds:
                    self.queue.append(action("finish"))
                else:
                    self._inspect()
            else:
                repairs = self._repairs()
                self.round += 1
                self.queue.extend(repairs)
                self.queue.append(action("probe"))
        self.last_action = self.queue.popleft()
        return self.last_action

    def _configs(self) -> dict[str, dict[str, Any]]:
        value = self.observed.get("get_config") or {}
        if not isinstance(value, dict):
            return {}
        value = value.get("configs", value.get("config", value))
        return {k: v for k, v in value.items() if isinstance(v, dict)} if isinstance(value, dict) else {}

    def _repairs(self) -> list[Action]:
        repairs: list[Action] = []
        configs = self._configs()
        metrics = self.observed.get("get_metrics") or {}
        services = metrics.get("services", metrics) if isinstance(metrics, dict) else {}
        if isinstance(services, dict):
            for name, measurement in sorted(services.items()):
                if isinstance(measurement, dict) and (
                    measurement.get("running") is False
                    or measurement.get("process_running") is False
                    or measurement.get("alive") is False
                    or measurement.get("status") in {"down", "stopped", "unavailable"}
                ):
                    repairs.append(action("restart_service", service=name))

        patches: dict[str, dict[str, Any]] = {}
        def patch(service: str, key: str, value: Any) -> None:
            patches.setdefault(service, {})[key] = value

        booking, payment = configs.get("booking", {}), configs.get("payment", {})
        timeout, latency = booking.get("payment_timeout_ms"), payment.get("provider_latency_ms")
        if isinstance(timeout, (int, float)) and isinstance(latency, (int, float)) and timeout <= latency:
            patch("booking", "payment_timeout_ms", min(10000, int(latency * 1.5) + 1))
        for service, key in (("booking", "payment_idempotency_enabled"), ("booking", "validate_price"),
                             ("payment", "idempotency_enabled"), ("inventory", "enforce_capacity")):
            if configs.get(service, {}).get(key) is False:
                patch(service, key, True)

        pending = _rows(self.observed.get("pending"))
        events = _rows(self.observed.get("outbox"))
        consumer = configs.get("checkin", {})
        if events and consumer.get("consumer_enabled") is False:
            patch("checkin", "consumer_enabled", True)
        batch_size = consumer.get("batch_size")
        if events and isinstance(batch_size, int) and batch_size < len(events):
            patch("checkin", "batch_size", min(50, len(events)))

        quarantines: list[Action] = []
        for event in events:
            try:
                payload = event.get("payload")
                payload = json.loads(payload) if isinstance(payload, str) else payload
                malformed = not isinstance(payload, dict)
            except (TypeError, ValueError):
                payload, malformed = {}, True
            if isinstance(payload, dict):
                # Missing business identifiers cannot be repaired by a parser upgrade.
                malformed = (any(not payload.get(key) for key in ("booking_id", "flight_id", "passenger_id"))
                             or payload.get("booking_id") != event.get("booking_id"))
                schema = payload.get("schema", payload.get("schema_version", 1))
                accepted = consumer.get("accepted_schema")
                if schema == 2 and accepted == 1:
                    patch("checkin", "accepted_schema", 2)
            if malformed and event.get("event_id"):
                quarantines.append(action("quarantine_event", event_id=event["event_id"]))

        for service, values in patches.items():
            repairs.append(action("patch_config", service=service, values=values))
        log_text = json.dumps(self.observed.get("get_logs") or {}, sort_keys=True).lower()
        if any(term in log_text for term in ("stale", "price_mismatch", "price mismatch", "quote mismatch")):
            repairs.append(action("invalidate_cache", service="pricing"))
        for row in pending:
            if row.get("booking_id"):
                arguments = {"booking_id": row["booking_id"]}
                if row.get("charge_count") == 1:
                    if self.recovery == "adopt" and row.get("charge_id"):
                        arguments["existing_charge_id"] = row["charge_id"]
                    elif row.get("idempotency_key"):
                        arguments["idempotency_key"] = row["idempotency_key"]
                repairs.append(action("reconcile_booking", **arguments))
        repairs.extend(quarantines)
        if events or pending:
            repairs.append(action("replay_events", limit=50))
        return repairs
