"""Observation-only oracle for the hard tier; it also solves the easy tier.

The policy decides from public tool results alone: deployed settings, process
metrics and read-only SQL over public tables. It imports nothing from the
environment, scenario catalogue, case generator, verifier or runtime, and a
test asserts that import graph. It is a scripted solver, not a trained model.
"""
from __future__ import annotations

from collections import defaultdict, deque
import json
from typing import Any

Action = dict[str, Any]

# Row predicates the oracle acts on, as read-only SQL over public tables. The
# ``LEGACY`` variants omit columns and tables that predate the hard tier so the
# same policy runs against a store without them.
SQL_A = (
    "SELECT b.booking_id, b.request_id, b.flight_id, b.passenger_id, b.amount_cents, b.client_reference, "
    "(h.booking_id IS NOT NULL) AS has_hold, "
    "(SELECT COUNT(*) FROM customer_events e WHERE e.request_id = b.request_id AND e.kind = 'cancel') AS cancel_events, "
    "(SELECT COUNT(*) FROM bookings s WHERE b.client_reference IS NOT NULL AND s.client_reference = b.client_reference "
    "AND s.booking_id <> b.booking_id AND s.status = 'confirmed') AS confirmed_siblings, "
    "(SELECT COUNT(*) FROM bookings s WHERE b.client_reference IS NOT NULL AND s.client_reference = b.client_reference "
    "AND s.booking_id <> b.booking_id AND s.status = 'pending' AND s.rowid < b.rowid) AS older_pending_siblings, "
    "(SELECT json_group_array(json_object('charge_id', c.charge_id, 'idempotency_key', c.idempotency_key, "
    "'state', c.state, 'amount_cents', c.amount_cents, 'created_step', c.created_step)) "
    "FROM (SELECT * FROM charges WHERE booking_id = b.booking_id ORDER BY rowid) c) AS charges "
    "FROM bookings b LEFT JOIN holds h ON h.booking_id = b.booking_id "
    "WHERE b.status = 'pending' ORDER BY b.rowid LIMIT 200"
)
SQL_A_LEGACY = (
    "SELECT b.booking_id, b.request_id, b.flight_id, b.passenger_id, b.amount_cents, NULL AS client_reference, "
    "(h.booking_id IS NOT NULL) AS has_hold, 0 AS cancel_events, 0 AS confirmed_siblings, 0 AS older_pending_siblings, "
    "(SELECT json_group_array(json_object('charge_id', c.charge_id, 'idempotency_key', c.idempotency_key, "
    "'state', 'captured', 'amount_cents', c.amount_cents, 'created_step', 0)) "
    "FROM (SELECT * FROM charges WHERE booking_id = b.booking_id ORDER BY rowid) c) AS charges "
    "FROM bookings b LEFT JOIN holds h ON h.booking_id = b.booking_id "
    "WHERE b.status = 'pending' ORDER BY b.rowid LIMIT 200"
)
SQL_B = (
    "SELECT o.event_id, o.booking_id, o.payload, o.attempts, json_valid(o.payload) AS valid_json, "
    "b.flight_id, b.passenger_id, b.status AS booking_status "
    "FROM outbox o LEFT JOIN bookings b ON b.booking_id = o.booking_id "
    "WHERE o.status = 'pending' ORDER BY o.event_id LIMIT 200"
)
SQL_C = (
    "SELECT 'flight' AS kind, flight_id AS id, price_cents AS amount_cents, version AS version, NULL AS until_step, NULL AS value FROM flights "
    "UNION ALL SELECT 'cache', key, NULL, NULL, NULL, value FROM cache WHERE key LIKE 'quote:%' "
    "UNION ALL SELECT 'hold', flight_id, price_cents, NULL, until_step, NULL FROM fare_holds "
    "UNION ALL SELECT 'conflict', service, COUNT(*), NULL, NULL, message FROM request_logs WHERE status = 409 GROUP BY service, message "
    "LIMIT 400"
)
SQL_C_LEGACY = (
    "SELECT 'flight' AS kind, flight_id AS id, price_cents AS amount_cents, version AS version, NULL AS until_step, NULL AS value FROM flights "
    "UNION ALL SELECT 'cache', key, NULL, NULL, NULL, value FROM cache WHERE key LIKE 'quote:%' "
    "UNION ALL SELECT 'conflict', service, COUNT(*), NULL, NULL, message FROM request_logs WHERE status = 409 GROUP BY service, message "
    "LIMIT 400"
)
SQL_S = "SELECT COUNT(*) AS submitted FROM charges WHERE state = 'submitted'"
QUERIES = {"A": SQL_A, "B": SQL_B, "C": SQL_C, "S": SQL_S}
SETTLEMENT_SLACK = 8
LEGACY = {SQL_A: SQL_A_LEGACY, SQL_C: SQL_C_LEGACY}
MAX_ATTEMPTS = 3
RELOOKUP_AFTER_STEPS = 4


def action(tool: str, **arguments: Any) -> Action:
    return {"tool": tool, "arguments": arguments}


def _unwrap(result: Any) -> Any:
    while isinstance(result, dict):
        if "data" in result and "tool" in result:
            result = result["data"]
        elif "result" in result:
            result = result["result"]
        else:
            break
    return result


def _rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        for key in ("rows", "data", "result"):
            if key in result:
                return _rows(result[key])
    return []


def event_is_valid(event: dict[str, Any]) -> tuple[bool, int | None]:
    """The public validity predicate: parseable object, integer schema 1 or 2,
    and business identity equal to the booking it names. Returns the schema."""
    try:
        payload = json.loads(event.get("payload")) if isinstance(event.get("payload"), str) else event.get("payload")
    except (TypeError, ValueError):
        return False, None
    if not isinstance(payload, dict):
        return False, None
    schema = payload.get("schema_version")
    valid = (type(schema) is int and schema in (1, 2)
             and event.get("booking_status") is not None
             and payload.get("booking_id") == event.get("booking_id")
             and payload.get("flight_id") == event.get("flight_id")
             and payload.get("passenger_id") == event.get("passenger_id"))
    return valid, (schema if valid else None)


class OraclePolicy:
    """Decision-table solver driven by public measurements and SQL.

    Round 1 reads settings and makes them safe, then reads only what the public
    summary and structural alerts implicate (metrics for a worker down, the
    pending outbox for a backlog, the fare picture for failing traffic). Pending
    bookings are read no earlier than the step at which verification becomes
    eligible (late faults land before it) and each is resolved by the evidence
    in its rows. An uncertain capture under a live key is re-presented to the
    provider through the booking path; an expired key is looked up within the
    quota; a pending outcome is waited for. Two counted healthy probes end it
    once no provider outcome can still be scheduled.
    """

    def __init__(self, max_rounds: int = 3):
        self.max_rounds = max_rounds
        self.reset()

    def reset(self) -> None:
        self.queue: deque[Action] = deque()
        self.last: Action | None = None
        self.observed: dict[str, Any] = {}
        self.read_step: dict[str, int] = {}
        self.step = 0
        self.horizon = 6
        self.quota: int | None = None
        self.quota_exhausted = False
        self.lookups_used = 0
        self.truth: dict[str, tuple[str, int]] = {}
        self.lookup_targets: dict[str, dict[str, Any]] = {}
        self.deferred: dict[str, str] = {}
        self.void_retry_after: dict[str, int] = {}
        self.failed: dict[str, list[tuple[str, dict[str, Any], str]]] = defaultdict(list)
        self.scoped_invalidation = True
        self.voids_supported = True
        self.rounds = 0
        self.started = False
        self.summary: dict[str, Any] = {}
        self.alerts: list[dict[str, Any]] = []
        self.restarted: dict[str, int] = defaultdict(int)
        self.planned_lookups = 0
        self.hard = False
        self.budget: int | None = None
        self.settled_at: int | None = None
        self.provider_pending: set[str] = set()
        self.queue.append(action("get_config"))

    # -- observation plumbing -------------------------------------------------

    def __call__(self, observation: dict[str, Any]) -> Action:
        if observation.get("step") == 0 and self.started:
            self.reset()
        self.started = True
        self.step = observation.get("step", 0) or 0
        self.summary = observation.get("summary") if isinstance(observation.get("summary"), dict) else {}
        self.alerts = observation.get("alerts") if isinstance(observation.get("alerts"), list) else []
        contract = observation.get("episode_contract")
        if observation.get("tier") == "hard":
            self.hard = True
        if isinstance(contract, dict):
            if isinstance(contract.get("max_actions"), int):
                self.budget = contract["max_actions"]
            if "provider_lookup_quota" in contract:
                self.hard = True
            if isinstance(contract.get("verification_eligible_from_step"), int):
                self.horizon = contract["verification_eligible_from_step"]
            if isinstance(contract.get("provider_lookup_quota"), int):
                self.quota = contract["provider_lookup_quota"]
        if self.last is not None:
            self._absorb(observation.get("result"))
        if not self.queue:
            self._plan()
        self.last = self.queue.popleft()
        return self.last

    def _absorb(self, result: Any) -> None:
        tool, arguments = self.last["tool"], self.last["arguments"]
        ok = not isinstance(result, dict) or result.get("ok", True)
        error = str(result.get("error", "")) if isinstance(result, dict) else ""
        data = _unwrap(result)
        if tool == "query_sql":
            query = arguments["query"]
            if not ok and query in LEGACY:
                self.queue.appendleft(action("query_sql", query=LEGACY[query]))
                return
            for name, text in QUERIES.items():
                if query in (text, LEGACY.get(text)):
                    self.observed[name] = _rows(data) if ok else []
                    self.read_step[name] = self.step
            if query == SQL_S:
                rows = self.observed.get("S") or [{"submitted": 0}]
                self.settled_at = self.step if not rows[0].get("submitted") else None
        elif tool in ("get_config", "get_metrics", "probe"):
            self.observed[tool] = data if ok else {}
            if tool == "get_config":
                self.observed.pop("configs", None)
            if tool != "probe":
                self.read_step[tool] = self.step
        elif tool == "provider_lookup":
            self._absorb_lookup(arguments["idempotency_key"], ok, error, data)
        elif tool == "reconcile_booking":
            self._absorb_reconcile(arguments, ok, error)
        elif tool == "void_booking":
            booking_id = arguments["booking_id"]
            if ok:
                self.deferred.pop(booking_id, None)
                self.provider_pending.discard(booking_id)
            elif "unresolved" in error or "pending" in error:
                self.deferred[booking_id] = "provider-pending"
                self.provider_pending.add(booking_id)
                self.void_retry_after[booking_id] = self.step + RELOOKUP_AFTER_STEPS
            elif "Unknown tool" in error:
                self.voids_supported = False
            else:
                self.failed[booking_id].append((tool, arguments, error))
        elif tool == "invalidate_cache" and not ok and "flight_id" in arguments:
            self.scoped_invalidation = False
            self.queue.appendleft(action("invalidate_cache", service=arguments["service"]))

    def _absorb_lookup(self, key: str, ok: bool, error: str, data: Any) -> None:
        target = self.lookup_targets.get(key, {})
        booking_id = target.get("booking_id")
        if ok:
            self.lookups_used += 1
            state = str(data.get("state")) if isinstance(data, dict) else "pending"
        elif "quota" in error.lower():
            self.quota_exhausted = True
            if booking_id:
                self.deferred[booking_id] = "quota"
            return
        else:
            # Unknown to the provider: the capture never reached it.
            state = "declined"
        self.truth[key] = (state, self.step)
        if booking_id and state == "pending":
            self.provider_pending.add(booking_id)
        if booking_id:
            follow_up = self._resolve(target, state)
            if follow_up is not None:
                self.queue.appendleft(follow_up)

    def _absorb_reconcile(self, arguments: dict[str, Any], ok: bool, error: str) -> None:
        booking_id = arguments["booking_id"]
        if ok:
            self.deferred.pop(booking_id, None)
            self.provider_pending.discard(booking_id)
            return
        if "sold out" in error:
            if self.voids_supported:
                self.queue.appendleft(action("void_booking", booking_id=booking_id))
        elif "504" in error or "pending" in error:
            # "pending at provider": settlement will decide. Any other lost
            # acknowledgement on a live key means the capture exists but the
            # provider's answer did not reach the caller.
            self.deferred[booking_id] = "provider-pending"
            if "pending" in error:
                self.provider_pending.add(booking_id)
            if arguments.get("idempotency_key"):
                truth = "pending" if "pending" in error else "ack-lost"
                self.truth[arguments["idempotency_key"]] = (truth, self.step)
        else:
            self.failed[booking_id].append(("reconcile_booking", arguments, error))

    # -- planning -------------------------------------------------------------

    def _messages(self) -> str:
        return " ".join(str(alert.get("message", "")) for alert in self.alerts if isinstance(alert, dict)).lower()

    def _due(self, name: str, signal: bool) -> bool:
        """A read is due when its public signal is present and it was never
        taken, or was taken before late faults could land and the signal is
        still there once they have."""
        if not signal:
            return False
        taken = self.read_step.get(name)
        return taken is None or (taken < self.horizon <= self.step + 1)

    def _due_reads(self) -> list[Action]:
        messages = self._messages()
        reads: list[Action] = []
        if self._due("get_metrics", "http worker unavailable" in messages):
            reads.append(action("get_metrics"))
        if self._due("B", bool(self.summary.get("outbox_pending"))):
            reads.append(action("query_sql", query=SQL_B))
        if self._due("C", "probes are failing" in messages):
            reads.append(action("query_sql", query=SQL_C))
        if any(read["arguments"].get("query") == SQL_B for read in reads) and self.read_step.get("get_config", -1) < self.horizon <= self.step + 1:
            # Consumer settings can change behind the agent's back (breaker, late schema fault).
            reads.insert(0, action("get_config"))
        return reads

    def _plan(self) -> None:
        remaining = self.summary.get("remaining_actions")
        if isinstance(remaining, int) and remaining <= 1:
            self.queue.append(action("finish"))
            return
        if self.last is not None and self.last["tool"] == "probe" and "probe" in self.observed:
            probe = self.observed.get("probe") or {}
            if isinstance(probe, dict) and probe.get("healthy"):
                if probe.get("verification_windows", 0) < 2:
                    self.queue.append(action("probe"))
                elif self._horizon_clear():
                    self.queue.append(action("finish"))
                elif self.read_step.get("S", -1) < self.step - 2:
                    self.queue.append(action("query_sql", query=SQL_S))
                else:
                    self.queue.append(action("probe"))
                return
            if not self.deferred:
                self.rounds += 1
            if self.rounds > self.max_rounds:
                self.queue.append(action("finish"))
                return
            self._invalidate_reads()
        if self.last is not None and self.last["tool"] == "query_sql" and self.last["arguments"]["query"] == SQL_S:
            self.queue.append(action("finish") if self._horizon_clear() else action("probe"))
            return
        if "get_config" not in self.read_step:
            self.queue.append(action("get_config"))
            return
        repairs = self._service_repairs()
        reads = self._due_reads()
        if reads:
            self.queue.extend(repairs + reads)
            return
        pending = self.summary.get("pending_bookings")
        need_bookings = pending is None or pending > 0 or bool(self.deferred)
        fresh = "A" in self.observed and self.read_step.get("A", -1) >= self.horizon
        if need_bookings and not fresh:
            self.queue.extend(repairs)
            # Late incidents land before verification eligibility; bookings are
            # resolved only from rows read at or after that step.
            while self.step + len(self.queue) + 1 < self.horizon:
                self.queue.append(action("probe"))
            self.queue.append(action("query_sql", query=SQL_A))
            return
        bookings = self._booking_repairs(len(repairs)) if fresh else []
        self.observed.pop("A", None)
        self.queue.extend(repairs)
        self.queue.extend(bookings)
        if self.deferred:
            # Waiting on the provider is cheapest as a direct re-read: a probe
            # cannot count while a booking is still pending.
            self.queue.append(action("query_sql", query=SQL_A))
        else:
            self.queue.append(action("probe"))

    def _horizon_clear(self) -> bool:
        """Finishing while the provider still holds an outcome as pending fails.
        The only pending outcomes the agent can know about are the ones it was
        told about; once their bookings resolved, or no charge is submitted any
        more, or the budget says nothing can settle later, the horizon is clear."""
        if not self.hard or not self.provider_pending:
            return True
        if self.budget is not None and self.step >= self.budget - SETTLEMENT_SLACK:
            return True
        return self.settled_at is not None

    def _invalidate_reads(self) -> None:
        """After an unhealthy probe, forget the reads the public signals implicate
        so they are taken again; with no signal, forget everything."""
        messages = self._messages()
        implicated = []
        if self.summary.get("outbox_pending"):
            implicated += ["get_config", "B"]
        if "http worker unavailable" in messages:
            implicated.append("get_metrics")
        if "probes are failing" in messages:
            implicated.append("C")
        if not implicated and not (self.summary.get("pending_bookings") or self.deferred):
            implicated = ["get_config", "get_metrics", "B", "C"]
        for name in implicated:
            self.read_step.pop(name, None)
            self.observed.pop(name, None)

    def _configs(self) -> dict[str, dict[str, Any]]:
        """Deployed settings as last read, updated optimistically by planned patches."""
        if "configs" not in self.observed:
            value = self.observed.get("get_config") or {}
            if isinstance(value, dict):
                value = value.get("configs", value.get("config", value))
            self.observed["configs"] = ({k: dict(v) for k, v in value.items() if isinstance(v, dict)}
                                        if isinstance(value, dict) else {})
        return self.observed["configs"]

    def _service_repairs(self) -> list[Action]:
        repairs: list[Action] = []
        configs = self._configs()
        patches: dict[str, dict[str, Any]] = {}

        def patch(service: str, key: str, value: Any) -> None:
            patches.setdefault(service, {})[key] = value
            configs.setdefault(service, {})[key] = value

        booking, payment = configs.get("booking", {}), configs.get("payment", {})
        timeout, latency = booking.get("payment_timeout_ms"), payment.get("provider_latency_ms")
        if isinstance(timeout, (int, float)) and isinstance(latency, (int, float)) and timeout <= latency:
            patch("booking", "payment_timeout_ms", min(10000, int(latency * 1.5) + 1))
        for service, key in (("booking", "payment_idempotency_enabled"), ("booking", "validate_price"),
                             ("payment", "idempotency_enabled"), ("inventory", "enforce_capacity")):
            if configs.get(service, {}).get(key) is False:
                patch(service, key, True)
        for service, values in patches.items():
            repairs.append(action("patch_config", service=service, values=values))
        patches = {}

        metrics = self.observed.get("get_metrics") or {}
        services = metrics.get("services", metrics) if isinstance(metrics, dict) else {}
        if isinstance(services, dict):
            for name, measurement in sorted(services.items()):
                if isinstance(measurement, dict) and measurement.get("running") is False and self.restarted[name] < 2:
                    repairs.append(action("restart_service", service=name))
                    self.restarted[name] += 1
                    measurement["running"] = True

        read_b = "B" in self.observed
        events = self.observed.get("B") or []
        consumer = configs.get("checkin", {})
        schemas, kept = [], []
        for event in events:
            valid, schema = event_is_valid(event)
            if valid:
                schemas.append(schema)
                kept.append(event)
            elif event.get("event_id"):
                repairs.append(action("quarantine_event", event_id=event["event_id"]))
        if read_b:
            self.observed["B"] = kept
        accepted = consumer.get("accepted_schema")
        if schemas and isinstance(accepted, int) and accepted < max(schemas):
            patch("checkin", "accepted_schema", max(schemas))
        batch_size = consumer.get("batch_size")
        if kept and isinstance(batch_size, int) and batch_size < len(kept):
            patch("checkin", "batch_size", min(50, len(kept)))
        if consumer.get("consumer_enabled") is False and (read_b or not self.summary.get("outbox_pending")):
            # The consumer comes back only after malformed heads are out of its
            # way; a circuit breaker would otherwise pause it again.
            patch("checkin", "consumer_enabled", True)
        for service, values in patches.items():
            repairs.append(action("patch_config", service=service, values=values))
        repairs.extend(self._cache_repairs())
        return repairs

    def _cache_repairs(self) -> list[Action]:
        rows = self.observed.get("C") or []
        flights = {r["id"]: r for r in rows if r.get("kind") == "flight"}
        holds = {r["id"]: r for r in rows if r.get("kind") == "hold"
                 and isinstance(r.get("until_step"), int) and r["until_step"] >= self.step + 1}
        repairs, remaining = [], []
        for row in rows:
            if row.get("kind") != "cache":
                remaining.append(row)
                continue
            flight_id = str(row.get("id", ""))[len("quote:"):]
            try:
                quote = json.loads(row.get("value") or "{}")
            except (TypeError, ValueError):
                quote = {}
            flight, hold = flights.get(flight_id), holds.get(flight_id)
            if hold is not None:
                stale = quote.get("amount_cents") != hold.get("amount_cents")
            elif flight is not None:
                stale = quote.get("amount_cents") != flight.get("amount_cents") or quote.get("version") != flight.get("version")
            else:
                stale = False
            if stale and flight_id:
                arguments = {"service": "pricing"}
                if self.scoped_invalidation:
                    arguments["flight_id"] = flight_id
                repairs.append(action("invalidate_cache", **arguments))
            else:
                remaining.append(row)
        self.observed["C"] = remaining
        return repairs

    # -- per-booking decision table -------------------------------------------

    def _lookups_remaining(self) -> int:
        """Quota left after the lookups already planned in this batch."""
        if self.quota_exhausted:
            return 0
        remaining = self.summary.get("provider_lookups_remaining")
        if not isinstance(remaining, int):
            quota = self.quota
            if quota is None:
                quota = self._configs().get("payment", {}).get("lookup_quota")
            remaining = (quota - self.lookups_used) if isinstance(quota, int) else 1
        return remaining - self.planned_lookups

    def _allowed(self, booking_id: str, candidate: Action) -> bool:
        failures = self.failed.get(booking_id, [])
        if len(failures) >= MAX_ATTEMPTS:
            return False
        for tool, arguments, error in failures:
            if tool == candidate["tool"] and arguments == candidate["arguments"] and "503" not in error:
                return False
        return True

    @staticmethod
    def _charges(row: dict[str, Any]) -> list[dict[str, Any]]:
        raw = row.get("charges")
        try:
            charges = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            charges = []
        return [c for c in charges if isinstance(c, dict)] if isinstance(charges, list) else []

    def _key_alive(self, charge: dict[str, Any], at_step: int) -> bool:
        """A key inside the provider's idempotency window still returns the
        original capture; a forgotten key makes a retry a new charge."""
        created = charge.get("created_step")
        if not isinstance(created, int):
            return True
        window = self._configs().get("payment", {}).get("idempotency_window_steps")
        window = window if isinstance(window, int) else 1000
        return at_step - created < window

    def _resolve(self, target: dict[str, Any], truth: str) -> Action | None:
        """Finish a booking whose provider outcome is known."""
        booking_id, key = target["booking_id"], target.get("idempotency_key")
        if truth == "captured":
            if target.get("has_hold") and target.get("charge_id"):
                return action("reconcile_booking", booking_id=booking_id, existing_charge_id=target["charge_id"])
            if key and target.get("alive"):
                return action("reconcile_booking", booking_id=booking_id, idempotency_key=key)
            return None
        if truth == "declined" and key:
            return action("reconcile_booking", booking_id=booking_id, idempotency_key=key)
        self.deferred[booking_id] = "provider-pending"
        return None

    def _decide(self, row: dict[str, Any], at_step: int) -> Action | None:
        booking_id = row.get("booking_id")
        if not booking_id:
            return None
        charges = self._charges(row)
        captured = [c for c in charges if (c.get("state") or "captured") == "captured"]
        declined = [c for c in charges if c.get("state") == "declined"]
        unknown = [c for c in charges if c.get("state") in ("submitted", "lost")]
        if row.get("cancel_events") or row.get("confirmed_siblings") or row.get("older_pending_siblings"):
            if not self.voids_supported:
                return None
            if self.step < self.void_retry_after.get(booking_id, 0):
                self.deferred[booking_id] = "provider-pending"
                return None
            return action("void_booking", booking_id=booking_id)
        if captured:
            if len(captured) > 1:
                return None
            charge = captured[0]
            return self._resolve({"booking_id": booking_id, "has_hold": bool(row.get("has_hold")), "charge_id": charge.get("charge_id"),
                                  "idempotency_key": charge.get("idempotency_key"), "alive": self._key_alive(charge, at_step)}, "captured")
        if unknown:
            charge = unknown[-1]
            key = charge.get("idempotency_key")
            target = {"booking_id": booking_id, "has_hold": bool(row.get("has_hold")), "charge_id": charge.get("charge_id"),
                      "idempotency_key": key, "alive": self._key_alive(charge, at_step)}
            known = self.truth.get(key)
            if known is not None and known[0] == "ack-lost":
                adopt = action("reconcile_booking", booking_id=booking_id, existing_charge_id=charge.get("charge_id"))
                if target["has_hold"] and charge.get("charge_id") and self._allowed(booking_id, adopt):
                    return adopt
                if key and self._lookups_remaining() > 0:
                    self.lookup_targets[key] = target
                    return action("provider_lookup", idempotency_key=key)
                self.deferred[booking_id] = "quota"
                return None
            if known is not None and known[0] != "pending":
                return self._resolve(target, known[0])
            if known is not None and self.step - known[1] < RELOOKUP_AFTER_STEPS:
                self.deferred[booking_id] = "provider-pending"
                return None
            if target["alive"] and key:
                # Re-presenting a live key makes the provider answer through the
                # booking path: captured confirms, declined captures afresh,
                # pending is refused without a write.
                return action("reconcile_booking", booking_id=booking_id, idempotency_key=key)
            if key and self._lookups_remaining() > 0:
                self.lookup_targets[key] = target
                return action("provider_lookup", idempotency_key=key)
            self.deferred[booking_id] = "quota"
            return None
        if declined:
            return action("reconcile_booking", booking_id=booking_id, idempotency_key=declined[-1].get("idempotency_key"))
        return action("reconcile_booking", booking_id=booking_id)

    def _booking_repairs(self, offset: int = 0) -> list[Action]:
        """Decide every pending row afresh; ``deferred`` is rebuilt from these rows
        so a booking that left the pending set stops being waited for."""
        repairs: list[Action] = []
        self.deferred = {}
        self.planned_lookups = 0
        for row in self.observed.get("A") or []:
            candidate = self._decide(row, self.step + offset + len(repairs) + 2)
            if candidate is not None and self._allowed(row["booking_id"], candidate):
                repairs.append(candidate)
                self.planned_lookups += candidate["tool"] == "provider_lookup"
        return repairs
