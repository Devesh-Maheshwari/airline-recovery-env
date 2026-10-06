"""The hard-tier oracle: import boundary, decision table on fabricated observations,
and executed success over levels x slots x seeds (AIRLINE_HARD_SEEDS, default 4)."""
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from airline_recovery.live import oracle
from airline_recovery.live.oracle import SQL_A, SQL_B, SQL_C, SQL_S, OraclePolicy

ROOT = Path(__file__).resolve().parents[1]
HAVE_HARD_TIER = (importlib.util.find_spec("airline_recovery.live.hardcases") is not None
                  and hasattr(importlib.import_module("airline_recovery.live.environment"), "TOOLS_HARD"))
SEEDS = list(range(1, int(os.environ.get("AIRLINE_HARD_SEEDS", "4")) + 1))
FORBIDDEN = ("environment", "scenarios", "hardcases", "hard_injector", "verification", "runtime", "store", "worker")


def hard_grid():
    from airline_recovery.live.environment import LiveAirlineEnv
    manifest = LiveAirlineEnv.task_manifest(tier="hard")
    for split in ("train", "eval", "test"):
        for task in manifest[split]:
            for seed in SEEDS:
                yield split, task["index"], seed


def run_hard(policy, split, index, seed):
    """Drive a policy through one hard episode; returns the score and public facts."""
    from airline_recovery.live.environment import LiveAirlineEnv
    lookups = steps = 0
    with LiveAirlineEnv(max_steps=None) as env:
        observation, _ = env.reset(seed=seed, options={"split": split, "index": index, "tier": "hard"})
        contract = observation["episode_contract"]
        while True:
            selected = policy(observation)
            observation, _, terminated, truncated, info = env.step(selected)
            steps += 1
            lookups += selected["tool"] == "provider_lookup" and observation["result"]["ok"]
            if terminated or truncated:
                return info["score"], {"budget": contract["max_actions"], "quota": contract["provider_lookup_quota"],
                                       "steps": steps, "lookups": lookups, "terminated": terminated}


class Driver:
    """Answers oracle actions from canned responses and tracks the step counter."""
    def __init__(self, responses, budget=30, summary=None, alerts=(), tier="hard"):
        self.responses, self.budget, self.tier = responses, budget, tier
        self.summary = {"pending_bookings": 0, "outbox_pending": 0, "remaining_actions": budget, **(summary or {})}
        self.alerts = list(alerts)
        self.step = 0
        self.actions = []

    def answer(self, selected):
        tool, arguments = selected["tool"], selected["arguments"]
        key = tool
        if tool == "query_sql":
            key = {SQL_A: "A", SQL_B: "B", SQL_C: "C", SQL_S: "S"}.get(arguments["query"], "legacy")
        response = self.responses.get(key, {"ok": True, "data": {}})
        if callable(response):
            response = response(arguments, self)
        return {"tool": tool, **({"ok": True, "data": response} if "ok" not in response else response)}

    def run(self, policy, limit=60):
        contract = {"max_actions": self.budget, "verification_eligible_from_step": 6}
        if self.tier == "hard":
            contract["provider_lookup_quota"] = 3
        observation = {"step": 0, "result": None, "summary": dict(self.summary), "alerts": self.alerts, "tier": self.tier,
                       "episode_contract": contract}
        for _ in range(limit):
            selected = policy(observation)
            self.actions.append(selected)
            self.step += 1
            self.summary["remaining_actions"] = self.budget - self.step
            if selected["tool"] == "finish":
                break
            observation = {"step": self.step, "result": self.answer(selected), "summary": dict(self.summary), "alerts": self.alerts}
        return self.actions


def tools(actions):
    return [a["tool"] for a in actions]


def pending_row(booking_id, charges=(), has_hold=1, **fields):
    row = {"booking_id": booking_id, "request_id": "req-" + booking_id, "flight_id": "F100", "passenger_id": "P", "amount_cents": 20000,
           "client_reference": None, "has_hold": has_hold, "cancel_events": 0, "confirmed_siblings": 0, "older_pending_siblings": 0,
           "charges": json.dumps([{"charge_id": f"chg-{booking_id}-{i}", "idempotency_key": f"booking:{booking_id}", "state": state,
                                   "amount_cents": 20000, "created_step": created} for i, (state, created) in enumerate(charges)])}
    row.update(fields)
    return row


class ImportBoundaryTests(unittest.TestCase):
    def test_oracle_imports_nothing_private(self):
        source = (ROOT / "airline_recovery" / "live" / "oracle.py").read_text(encoding="utf-8")
        for name in FORBIDDEN:
            self.assertNotIn(f"from .{name}", source)
            self.assertNotIn(f"from airline_recovery.live.{name}", source)
            self.assertNotIn(f"import {name}", source)
        script = ("import sys; import airline_recovery.live.oracle; "
                  "print(json.dumps(sorted(m for m in sys.modules if m.startswith('airline_recovery'))))")
        loaded = json.loads(subprocess.run([sys.executable, "-c", "import json; " + script], cwd=ROOT, check=True,
                                           capture_output=True, text=True).stdout)
        self.assertEqual(loaded, ["airline_recovery", "airline_recovery.live", "airline_recovery.live.oracle"])

    def test_policy_contract(self):
        policy = OraclePolicy()
        self.assertTrue(callable(policy))
        policy.reset()
        from airline_recovery.live.policies import load_policy
        self.assertIsInstance(load_policy("oracle"), OraclePolicy)


class DecisionTableTests(unittest.TestCase):
    def config(self, **overrides):
        base = {"inventory": {"enforce_capacity": True}, "pricing": {"cache_enabled": True},
                "payment": {"provider_latency_ms": 80, "idempotency_enabled": True, "lookup_quota": 3, "idempotency_window_steps": 1000},
                "booking": {"payment_timeout_ms": 200, "payment_idempotency_enabled": True, "payment_key_version": 1, "validate_price": True},
                "checkin": {"consumer_enabled": True, "batch_size": 10, "accepted_schema": 1, "auto_pause_after_attempts": 0}}
        for service, values in overrides.items():
            base[service].update(values)
        return base

    def decide(self, rows, **kwargs):
        driver = Driver({"get_config": self.config(**kwargs.pop("config", {})), "A": rows,
                         "probe": {"healthy": False, "verification_windows": 0}},
                        summary={"pending_bookings": len(rows)})
        driver.run(OraclePolicy(), limit=12)
        return [a for a in driver.actions if a["tool"] in ("reconcile_booking", "void_booking", "provider_lookup")]

    def test_bookings_are_read_no_earlier_than_verification_eligibility(self):
        driver = Driver({"get_config": self.config(), "A": []}, summary={"pending_bookings": 1})
        driver.run(OraclePolicy(), limit=8)
        reads = [i + 1 for i, a in enumerate(driver.actions) if a["tool"] == "query_sql" and a["arguments"]["query"] == SQL_A]
        self.assertEqual(reads[:1], [6])
        self.assertEqual(tools(driver.actions)[:6], ["get_config", "probe", "probe", "probe", "probe", "query_sql"])

    def test_cancel_event_and_duplicate_siblings_are_voided(self):
        actions = self.decide([pending_row("c", [("captured", 0)], cancel_events=1),
                               pending_row("d", [("captured", 0)], client_reference="CR", confirmed_siblings=1),
                               pending_row("e", [("submitted", 0)], client_reference="CR2", older_pending_siblings=1),
                               pending_row("f", [("captured", 0)], client_reference="CR2")])
        self.assertEqual(actions[:3], [{"tool": "void_booking", "arguments": {"booking_id": b}} for b in "cde"])
        self.assertEqual(actions[3], {"tool": "reconcile_booking", "arguments": {"booking_id": "f", "existing_charge_id": "chg-f-0"}})

    def test_captured_adopts_declined_retries_key_and_no_charge_is_plain(self):
        actions = self.decide([pending_row("a", [("captured", 0)]), pending_row("b", [("declined", 0)]),
                               pending_row("c"), pending_row("d", has_hold=0)])
        self.assertEqual(actions, [
            {"tool": "reconcile_booking", "arguments": {"booking_id": "a", "existing_charge_id": "chg-a-0"}},
            {"tool": "reconcile_booking", "arguments": {"booking_id": "b", "idempotency_key": "booking:b"}},
            {"tool": "reconcile_booking", "arguments": {"booking_id": "c"}},
            {"tool": "reconcile_booking", "arguments": {"booking_id": "d"}},
        ])

    def test_unknown_charge_with_live_key_is_re_presented_and_expired_key_is_looked_up(self):
        actions = self.decide([pending_row("live", [("submitted", 0)]), pending_row("old", [("submitted", -30)]),
                               pending_row("lost", [("lost", -30)], has_hold=0)],
                              config={"payment": {"idempotency_window_steps": 10}})
        self.assertEqual(actions[0], {"tool": "reconcile_booking", "arguments": {"booking_id": "live", "idempotency_key": "booking:live"}})
        self.assertEqual(actions[1], {"tool": "provider_lookup", "arguments": {"idempotency_key": "booking:old"}})
        self.assertEqual(actions[2], {"tool": "provider_lookup", "arguments": {"idempotency_key": "booking:lost"}})

    def test_lookup_results_drive_adopt_retry_or_wait(self):
        truths = {"booking:cap": "captured", "booking:dec": "declined", "booking:pen": "pending"}
        driver = Driver({"get_config": self.config(payment={"idempotency_window_steps": 5}),
                         "A": [pending_row(b, [("submitted", -40)]) for b in ("cap", "dec", "pen")],
                         "provider_lookup": lambda args, d: {"idempotency_key": args["idempotency_key"], "state": truths[args["idempotency_key"]],
                                                             "booking_id": "x", "amount_cents": 20000}},
                        summary={"pending_bookings": 3, "provider_lookups_remaining": 3})
        driver.run(OraclePolicy(), limit=14)
        acted = [a for a in driver.actions if a["tool"] in ("reconcile_booking", "provider_lookup")]
        self.assertEqual([a["tool"] for a in acted[:5]], ["provider_lookup", "reconcile_booking", "provider_lookup", "reconcile_booking", "provider_lookup"])
        self.assertEqual(acted[1]["arguments"], {"booking_id": "cap", "existing_charge_id": "chg-cap-0"})
        self.assertEqual(acted[3]["arguments"], {"booking_id": "dec", "idempotency_key": "booking:dec"})
        self.assertNotIn("pen", [a["arguments"].get("booking_id") for a in acted])
        self.assertLessEqual(sum(a["tool"] == "provider_lookup" for a in driver.actions), 3)
        # A pending outcome is waited for with re-reads, never re-looked-up immediately.
        after = driver.actions[driver.actions.index(acted[4]) + 1:]
        self.assertIn({"tool": "query_sql", "arguments": {"query": SQL_A}}, after[:2])

    def test_sold_out_retry_is_voided(self):
        driver = Driver({"get_config": self.config(), "A": [pending_row("s", has_hold=0)],
                         "reconcile_booking": {"ok": False, "data": None, "error": "Booking recovery HTTP 409: inventory: flight sold out: capacity exhausted"}},
                        summary={"pending_bookings": 1})
        driver.run(OraclePolicy(), limit=10)
        index = tools(driver.actions).index("reconcile_booking")
        self.assertEqual(driver.actions[index + 1], {"tool": "void_booking", "arguments": {"booking_id": "s"}})

    def test_settings_rules(self):
        payload = {"schema_version": 2, "booking_id": "B1", "flight_id": "F100", "passenger_id": "P1"}
        events = [{"event_id": "good", "booking_id": "B1", "payload": json.dumps(payload), "attempts": 3, "valid_json": 1,
                   "flight_id": "F100", "passenger_id": "P1", "booking_status": "confirmed"},
                  {"event_id": "decoy", "booking_id": "B1", "payload": json.dumps({**payload, "schema_version": "2"}), "attempts": 0, "valid_json": 1,
                   "flight_id": "F100", "passenger_id": "P1", "booking_status": "confirmed"},
                  {"event_id": "foreign", "booking_id": "B9", "payload": json.dumps(payload), "attempts": 0, "valid_json": 1,
                   "flight_id": None, "passenger_id": None, "booking_status": None}]
        cache = [{"kind": "flight", "id": "F100", "amount_cents": 22000, "version": 2, "until_step": None, "value": None},
                 {"kind": "flight", "id": "F200", "amount_cents": 27000, "version": 2, "until_step": None, "value": None},
                 {"kind": "cache", "id": "quote:F100", "amount_cents": None, "version": None, "until_step": None,
                  "value": json.dumps({"flight_id": "F100", "amount_cents": 20000, "version": 1})},
                 {"kind": "cache", "id": "quote:F200", "amount_cents": None, "version": None, "until_step": None,
                  "value": json.dumps({"flight_id": "F200", "amount_cents": 25000, "version": 1})},
                 {"kind": "hold", "id": "F200", "amount_cents": 25000, "version": None, "until_step": 40, "value": None}]
        driver = Driver({"get_config": self.config(booking={"payment_timeout_ms": 50, "payment_idempotency_enabled": False},
                                                   payment={"provider_latency_ms": 400},
                                                   checkin={"consumer_enabled": False, "batch_size": 1}),
                         "get_metrics": {"services": {"pricing": {"running": False, "health": "healthy"},
                                                      "payment": {"running": True, "health": "degraded"}}},
                         "B": events, "C": cache, "A": [], "probe": {"healthy": False, "verification_windows": 0}},
                        summary={"pending_bookings": 0, "outbox_pending": 3},
                        alerts=[{"service": "pricing", "severity": "critical", "message": "HTTP worker unavailable"},
                                {"service": "booking", "severity": "critical", "message": "Customer transaction probes are failing"},
                                {"service": "payment", "severity": "critical", "message": "Payment worker health degraded: restart recommended"}])
        actions = driver.run(OraclePolicy(), limit=14)
        self.assertEqual(actions[0], {"tool": "get_config", "arguments": {}})
        self.assertEqual(actions[1], {"tool": "patch_config", "arguments": {"service": "booking", "values": {"payment_timeout_ms": 601, "payment_idempotency_enabled": True}}})
        self.assertIn({"tool": "restart_service", "arguments": {"service": "pricing"}}, actions)
        self.assertNotIn({"tool": "restart_service", "arguments": {"service": "payment"}}, actions)
        quarantined = [a["arguments"]["event_id"] for a in actions if a["tool"] == "quarantine_event"]
        self.assertEqual(sorted(quarantined), ["decoy", "foreign"])
        checkin = next(a for a in actions if a["tool"] == "patch_config" and a["arguments"]["service"] == "checkin")
        self.assertEqual(checkin["arguments"]["values"], {"accepted_schema": 2, "consumer_enabled": True})
        self.assertLess(max(i for i, a in enumerate(actions) if a["tool"] == "quarantine_event"), actions.index(checkin))
        self.assertEqual([a for a in actions if a["tool"] == "invalidate_cache"],
                         [{"tool": "invalidate_cache", "arguments": {"service": "pricing", "flight_id": "F100"}}])
        self.assertNotIn("get_logs", tools(actions))
        self.assertNotIn("replay_events", tools(actions))

    def test_finish_waits_for_the_settlement_horizon_in_the_hard_tier(self):
        pending = {"ok": False, "data": None, "error": "Booking recovery HTTP 504: payment: capture outcome pending at provider"}
        calls = []
        def rows(arguments, driver):
            calls.append(driver.step)
            # The provider's outcome is pending on the first read; traffic later confirms the booking.
            driver.summary["pending_bookings"] = 0 if calls[1:] else 1
            return [pending_row("p", [("submitted", 0)])] if len(calls) == 1 else []
        driver = Driver({"get_config": self.config(), "A": rows, "reconcile_booking": pending,
                         "probe": {"healthy": True, "verification_windows": 2}, "S": [{"submitted": 1}]},
                        budget=30, summary={"pending_bookings": 1})
        policy = OraclePolicy()
        actions = driver.run(policy, limit=40)
        self.assertIn("p", policy.provider_pending)
        self.assertEqual(actions[-1]["tool"], "finish")
        self.assertGreaterEqual(len(actions), 22)
        self.assertLessEqual(len(actions), 24)
        self.assertTrue(2 <= sum(a["tool"] == "query_sql" and a["arguments"]["query"] == SQL_S for a in actions) <= 6)
        # Without any outcome the provider reported pending, two counted probes suffice.
        quick = Driver({"get_config": self.config(), "A": [], "probe": {"healthy": True, "verification_windows": 2}}, budget=30)
        self.assertEqual(tools(quick.run(OraclePolicy(), limit=40))[-2:], ["probe", "finish"])
        easy = Driver({"get_config": self.config(), "A": [], "probe": {"healthy": True, "verification_windows": 2}}, budget=48, tier="easy")
        easy.summary.pop("remaining_actions")
        actions = easy.run(OraclePolicy(), limit=40)
        self.assertEqual(tools(actions)[-2:], ["probe", "finish"])
        self.assertNotIn(SQL_S, [a["arguments"].get("query") for a in actions])

    def test_legacy_store_falls_back_to_easy_queries(self):
        rejected = {"ok": False, "data": None, "error": "Read-only query rejected: no such table: customer_events"}
        driver = Driver({"get_config": self.config(), "A": rejected, "C": rejected, "legacy": [],
                         "probe": {"healthy": True, "verification_windows": 2}},
                        summary={"pending_bookings": 1}, alerts=[{"service": "booking", "severity": "critical", "message": "Customer transaction probes are failing"}],
                        tier="easy", budget=48)
        actions = driver.run(OraclePolicy(), limit=20)
        queries = [a["arguments"]["query"] for a in actions if a["tool"] == "query_sql"]
        self.assertIn(oracle.SQL_A_LEGACY, queries)
        self.assertIn(oracle.SQL_C_LEGACY, queries)
        self.assertEqual(queries.index(SQL_A) + 1, queries.index(oracle.SQL_A_LEGACY))


@unittest.skipIf(os.environ.get("AIRLINE_SKIP_HARD_GRIDS") == "1", "executed hard-tier grid runs in its own CI job")
@unittest.skipUnless(HAVE_HARD_TIER, "hard tier not integrated yet")
class OracleHardTierTests(unittest.TestCase):
    def test_oracle_solves_every_generated_instance(self):
        failures = []
        for split, index, seed in hard_grid():
            score, facts = run_hard(OraclePolicy(), split, index, seed)
            with self.subTest(task=f"{split}:{index}:{seed}"):
                detail = {"success": score["success"], "violations": score["details"]["violations"], "recovery": score["recovery"],
                          "steps": facts["steps"], "budget": facts["budget"], "lookups": facts["lookups"], "quota": facts["quota"]}
                self.assertEqual(score["details"]["violations"], [], detail)
                self.assertTrue(score["success"], detail)
                self.assertTrue(facts["terminated"], detail)
                self.assertLessEqual(facts["steps"], facts["budget"], detail)
                self.assertLessEqual(facts["lookups"], facts["quota"], detail)
                self.assertEqual(score["details"]["safety_check"]["violations"], [], detail)
                if not score["success"]:
                    failures.append(detail)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
