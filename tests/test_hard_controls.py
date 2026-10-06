"""Hard-tier controls: each shortcut passes at most 10% and fails for its designed reason."""
import json
import os
import unittest

from airline_recovery.live.oracle import SQL_A
from airline_recovery.live.policies import (AdoptElseReconcilePolicy, BlanketHardPolicy, BlanketPolicy, ReferencePolicy,
                                            WaitThenReconcilePolicy, load_policy, nop)
from tests.test_hard_oracle import HAVE_HARD_TIER, Driver, pending_row, run_hard, tools

CONTROL_SEEDS = list(range(1, int(os.environ.get("AIRLINE_HARD_CONTROL_SEEDS", "1")) + 1))
TRAP_CODES = ("cancelled_request_fulfilled", "duplicate_sale", "duplicate_charge", "unfunded_confirmation",
              "accepted_request_changed", "valid_event_discarded", "refund_missing")
# What each control is built to get wrong, as violation prefixes or trap outcomes.
DESIGNED = {
    "nop": {"incomplete"},
    "blanket": {"duplicate_charge", "cancelled_request_fulfilled", "duplicate_sale", "incomplete", "valid_event_discarded"},
    "blanket-hard": {"duplicate_charge", "cancelled_request_fulfilled", "duplicate_sale", "payment_restarted_with_inflight",
                     "fare_hold_broken", "incomplete", "valid_event_discarded"},
    "reference": {"duplicate_charge", "cancelled_request_fulfilled", "duplicate_sale", "incomplete", "accepted_request_changed"},
    "reference-adopt": {"cancelled_request_fulfilled", "duplicate_sale", "incomplete", "accepted_request_changed"},
    "source-aware": {"cancelled_request_fulfilled", "duplicate_sale", "duplicate_charge", "incomplete"},
    "adopt-else-reconcile": {"cancelled_request_fulfilled", "duplicate_sale", "duplicate_charge", "incomplete"},
    "wait-then-reconcile": {"cancelled_request_fulfilled", "duplicate_sale", "duplicate_charge", "incomplete"},
}


def reasons(score, facts):
    found = {code.split(":")[0] for code in score["details"]["violations"]}
    found |= {name for name, value in (score["details"].get("trap_outcomes") or {}).items() if value is True}
    if score["recovery"] < 1 or not facts["terminated"] or not score["verified"]:
        found.add("incomplete")
    return found


def controls_grid():
    from airline_recovery.live.environment import LiveAirlineEnv
    manifest = LiveAirlineEnv.task_manifest(tier="hard")
    for split in ("train", "eval", "test"):
        for task in manifest[split]:
            for seed in CONTROL_SEEDS:
                yield split, task["index"], seed


class ControlStructureTests(unittest.TestCase):
    """Fabricated observations: the controls take exactly the branch they are named for."""
    def test_registered(self):
        self.assertIsInstance(load_policy("blanket-hard"), BlanketHardPolicy)
        self.assertIsInstance(load_policy("adopt-else-reconcile"), AdoptElseReconcilePolicy)
        self.assertIsInstance(load_policy("wait-then-reconcile"), WaitThenReconcilePolicy)
        self.assertIsInstance(load_policy("blanket"), BlanketPolicy)
        with self.assertRaises(ValueError):
            load_policy("blanket-harder")

    def test_blanket_hard_restarts_everything_and_invalidates_unscoped(self):
        policy = BlanketHardPolicy()
        actions = [policy({"step": 0, "result": None})]
        for step in range(1, 24):
            if actions[-1]["tool"] == "finish":
                break
            data = [{"kind": "booking", "id": "b1"}, {"kind": "event", "id": "e1"}] if actions[-1]["tool"] == "query_sql" else {}
            actions.append(policy({"step": step, "result": {"tool": actions[-1]["tool"], "ok": True, "data": data}}))
        self.assertEqual([a["arguments"]["service"] for a in actions if a["tool"] == "restart_service"],
                         ["inventory", "pricing", "payment", "booking", "checkin"])
        self.assertIn({"tool": "invalidate_cache", "arguments": {"service": "pricing"}}, actions)
        self.assertIn({"tool": "reconcile_booking", "arguments": {"booking_id": "b1"}}, actions)
        self.assertIn({"tool": "quarantine_event", "arguments": {"event_id": "e1"}}, actions)
        self.assertNotIn("void_booking", tools(actions))
        self.assertEqual(tools(actions)[-3:], ["probe", "probe", "finish"])

    def rows(self):
        return [pending_row("cancelled", [("captured", 0)], cancel_events=1),
                pending_row("dup", [("submitted", 0)], client_reference="CR", confirmed_siblings=1),
                pending_row("plain"), pending_row("declined", [("declined", 0)])]

    def test_adopt_else_reconcile_never_voids_or_looks_up(self):
        driver = Driver({"get_config": {}, "A": self.rows(), "probe": {"healthy": False, "verification_windows": 0}},
                        summary={"pending_bookings": 4})
        actions = driver.run(AdoptElseReconcilePolicy(), limit=14)
        bookings = [a for a in actions if a["tool"] in ("reconcile_booking", "void_booking", "provider_lookup")]
        self.assertEqual(bookings[:4], [
            {"tool": "reconcile_booking", "arguments": {"booking_id": "cancelled", "existing_charge_id": "chg-cancelled-0"}},
            {"tool": "reconcile_booking", "arguments": {"booking_id": "dup", "existing_charge_id": "chg-dup-0"}},
            {"tool": "reconcile_booking", "arguments": {"booking_id": "plain"}},
            {"tool": "reconcile_booking", "arguments": {"booking_id": "declined", "existing_charge_id": "chg-declined-0"}},
        ])
        self.assertNotIn("void_booking", tools(actions))
        self.assertNotIn("provider_lookup", tools(actions))

    def test_wait_then_reconcile_waits_then_retries_plainly(self):
        driver = Driver({"get_config": {}, "A": self.rows(), "probe": {"healthy": False, "verification_windows": 0}},
                        summary={"pending_bookings": 4}, budget=30)
        actions = driver.run(WaitThenReconcilePolicy(wait_steps=12), limit=22)
        first = next(i for i, a in enumerate(actions) if a["tool"] == "reconcile_booking")
        self.assertGreaterEqual(first + 1, 12)
        self.assertEqual([a["arguments"] for a in actions if a["tool"] == "reconcile_booking"][:4],
                         [{"booking_id": b} for b in ("cancelled", "dup", "plain", "declined")])
        self.assertNotIn("void_booking", tools(actions))
        self.assertNotIn("provider_lookup", tools(actions))
        self.assertIn(SQL_A, [a["arguments"].get("query") for a in actions[:first]])


@unittest.skipIf(os.environ.get("AIRLINE_SKIP_HARD_GRIDS") == "1", "executed hard-tier grid runs in its own CI job")
@unittest.skipUnless(HAVE_HARD_TIER, "hard tier not integrated yet")
class ControlOutcomeTests(unittest.TestCase):
    def run_control(self, name):
        outcomes = []
        for split, index, seed in controls_grid():
            policy = load_policy(name)
            if callable(getattr(policy, "reset", None)):
                policy.reset()
            score, facts = run_hard(policy, split, index, seed)
            outcomes.append((f"{split}:{index}:{seed}", score["success"], reasons(score, facts)))
        return outcomes

    def assertControl(self, name):
        outcomes = self.run_control(name)
        passes = sum(success for _, success, _ in outcomes)
        self.assertLessEqual(passes / len(outcomes), 0.10, {"policy": name, "outcomes": outcomes})
        for task, success, found in outcomes:
            if not success:
                self.assertTrue(found & DESIGNED[name], {"policy": name, "task": task, "reasons": sorted(found)})
        failures = [found for _, success, found in outcomes if not success]
        self.assertTrue(any(found & (DESIGNED[name] - {"incomplete"}) for found in failures) or name == "nop",
                        {"policy": name, "failures": failures})

    def test_nop(self):
        self.assertControl("nop")

    def test_blanket(self):
        self.assertControl("blanket")

    def test_blanket_hard(self):
        self.assertControl("blanket-hard")

    def test_reference(self):
        self.assertControl("reference")

    def test_reference_adopt(self):
        self.assertControl("reference-adopt")

    def test_source_aware(self):
        self.assertControl("source-aware")

    def test_adopt_else_reconcile(self):
        self.assertControl("adopt-else-reconcile")

    def test_wait_then_reconcile(self):
        self.assertControl("wait-then-reconcile")


if __name__ == "__main__":
    unittest.main()
