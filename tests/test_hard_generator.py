"""Procedural hard cases: determinism, structural variety and pool constraints."""
import importlib.util
import json
import unittest

HAVE_GENERATOR = importlib.util.find_spec("airline_recovery.live.hardcases") is not None
if HAVE_GENERATOR:
    from airline_recovery.live import hardcases

POOL_A = {"lost-ack-mixed", "key-migration-live", "payment-degraded-inflight"}
POOL_B = {"poison", "schema-mixed", "breaker-paused"}
POOL_C = {"stale-cache", "stale-cache+fare-hold"}
POOL_D = {"pricing-down", "inventory-down-then-up", "checkin-down"}
POOL_E = {"cancelled-pending", "duplicate-client-reference", "restart-bait"}
POOL_F = {"log-retention", "misleading-alert", "lying-log-line", "self-healed-transient"}
HAZARDS = {"lost_ack_retry", "fare_change", "cancel_then_retry", "expired_key_retry", "duplicate_event_delivery"}
BUDGETS = {1: 32, 2: 36, 3: 34}


def grid(seeds=28):
    for level in (1, 2, 3):
        for slot in range(6):
            for seed in range(1, seeds + 1):
                yield level, slot, seed


def kinds(faults):
    return [fault.kind for fault in faults]


def plans(case):
    for fault in case.initial:
        if fault.kind in POOL_A:
            return [p for p in fault.params.get("plans", []) if p]
    return []


@unittest.skipUnless(HAVE_GENERATOR, "hard-tier generator not integrated yet")
class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {(level, slot, seed): hardcases.generate_case(level, slot, seed) for level, slot, seed in grid()}
        assert len(cls.cases) >= 500

    def test_deterministic_and_independent_of_other_draws(self):
        for key in list(self.cases)[::37]:
            self.assertEqual(hardcases.generate_case(*key), self.cases[key])
        a = hardcases.generate_case(2, 1, 5)
        hardcases.generate_case(3, 0, 9)
        self.assertEqual(hardcases.generate_case(2, 1, 5), a)
        self.assertNotEqual(hardcases.generate_case(2, 1, 5), hardcases.generate_case(2, 1, 6))
        self.assertNotEqual(hardcases.generate_case(2, 1, 5), hardcases.generate_case(2, 2, 5))

    def test_slot_table_and_identity_fields(self):
        self.assertEqual(hardcases.LEVEL_BY_SLOT, {"train": (1, 1, 2, 2, 3, 3), "eval": (1, 2, 3), "test": (1, 2, 3)})
        case = hardcases.generate_case(2, 3, 7, split="eval")
        self.assertEqual((case.tier, case.split, case.index, case.level, case.slot, case.seed), ("hard", "eval", 3, 2, 3, 7))
        for level, (split, index) in ((1, ("train", 0)), (3, ("test", 2)), (2, ("eval", 1))):
            self.assertEqual(hardcases.level_for(split, index), level)
        with self.assertRaises(ValueError):
            hardcases.level_for("train", 6)

    def test_structural_variety(self):
        structures = {level: set() for level in BUDGETS}
        for (level, _, _), case in self.cases.items():
            structures[level].add((tuple(kinds(case.initial)), tuple(kinds(case.delayed)), tuple(sorted(kinds(case.traps))),
                                   tuple(sorted(kinds(case.noise)))))
        self.assertGreaterEqual(len(structures[1]), 8)
        self.assertGreaterEqual(len(structures[2]), 40)
        self.assertGreaterEqual(len(structures[3]), 60)
        seen = set()
        for case in self.cases.values():
            seen.update(kinds(case.initial) + kinds(case.delayed) + kinds(case.traps) + kinds(case.noise))
        self.assertEqual(seen, POOL_A | POOL_B | POOL_C | POOL_D | POOL_E | POOL_F)
        alert_only = [case for (level, _, _), case in self.cases.items() if level == 1 and not case.initial]
        self.assertTrue(alert_only)
        self.assertTrue(all(case.noise and not case.traps and not case.delayed for case in alert_only))
        self.assertLess(len(alert_only) / sum(level == 1 for level, _, _ in self.cases), 0.35)

    def test_pool_and_level_constraints(self):
        for key, case in self.cases.items():
            with self.subTest(case=key):
                level = key[0]
                self.assertEqual(case.budget, BUDGETS[level])
                self.assertTrue(2 <= case.delayed_step <= 5)
                initial = kinds(case.initial)
                for pool in (POOL_A, POOL_B, POOL_C, POOL_D):
                    self.assertLessEqual(len(pool & set(initial)), 1)
                self.assertEqual(set(initial) - (POOL_A | POOL_B | POOL_C | POOL_D), set())
                a = next((k for k in initial if k in POOL_A), None)
                d = next((k for k in initial if k in POOL_D), None)
                traps = kinds(case.traps)
                self.assertEqual(len(traps), len(set(traps)))
                if "cancelled-pending" in traps:
                    self.assertIsNotNone(a)
                if "duplicate-client-reference" in traps:
                    self.assertTrue(a is not None or d == "inventory-down-then-up")
                if "restart-bait" in traps:
                    self.assertEqual(a, "payment-degraded-inflight")
                self.assertLessEqual(len(plans(case)), 10)
                unknown_forever = [p for p in plans(case) if p["settles_at_step"] is None or p["settles_at_step"] > case.budget - 8]
                self.assertLessEqual(len(unknown_forever), case.lookup_quota)
                for plan in plans(case):
                    if plan["settles_at_step"] is not None:
                        self.assertTrue(4 <= plan["settles_at_step"] <= case.budget - 8)
                    if plan["outcome"] == "pending":
                        self.assertIn(plan["final_state"], ("captured", "declined"))
                        self.assertIsNotNone(plan["settles_at_step"])
                if a == "lost-ack-mixed":
                    outcomes = {p["outcome"] for p in plans(case)}
                    self.assertGreaterEqual(len(plans(case)), 4)
                    self.assertEqual(outcomes, {"captured", "declined", "pending"})
                if a == "key-migration-live":
                    params = case.initial[[k for k in initial].index(a)].params
                    self.assertIn(params["deployed_version"], (1, 2))
                    self.assertEqual({1, 2} & set(params["states"]), {1, 2})
                    self.assertTrue(any(p and p["outcome"] == "declined" for p in params["plans"]))
                if a == "payment-degraded-inflight":
                    params = case.initial[initial.index(a)].params
                    self.assertGreater(params["latency_ms"], params["timeout_ms"])
                    self.assertGreaterEqual(len(params["plans"]), 2)
                    self.assertEqual(case.misleading_alert["service"], "payment")
                    self.assertIn("restart", case.misleading_alert["message"])
                if "stale-cache+fare-hold" in initial + kinds(case.delayed):
                    fault = next(f for f in case.initial + case.delayed if f.kind == "stale-cache+fare-hold")
                    self.assertNotEqual(fault.params["hold_flight"], fault.params["flight"])
                    self.assertGreater(fault.params["hold_until"], case.budget)
                self.assertTrue(2 <= case.lookup_quota <= 20)
                self.assertTrue(1 <= case.idempotency_window <= 1000)
                if case.idempotency_window < 1000:
                    self.assertIsNotNone(a)
                    self.assertTrue(case.budget < case.idempotency_window <= case.budget + 20)
                self.assertEqual(len(case.safety_hazards), 3)
                self.assertEqual(len(set(case.safety_hazards)), 3)
                self.assertLessEqual(set(case.safety_hazards), HAZARDS)
                if level == 1:
                    self.assertLessEqual(len(initial), 1)
                    self.assertLessEqual(len(traps), 1)
                    self.assertEqual(case.delayed, ())
                    self.assertEqual(case.scale_rows, 0)
                    self.assertIsNone(case.log_retention)
                    if case.initial:
                        self.assertEqual(case.noise, ())
                        self.assertNotIn(initial[0], POOL_C)
                else:
                    self.assertIsNotNone(a)
                    self.assertTrue(POOL_B & set(initial))
                    self.assertEqual(len(traps), 2)
                    self.assertEqual(len(case.delayed), 1)
                    delayed = case.delayed[0].kind
                    self.assertIn(delayed, POOL_B | POOL_C)
                    self.assertNotIn(delayed, initial)
                if level == 3:
                    self.assertTrue(1 <= len(case.noise) <= 2)
                    self.assertTrue(300 <= case.scale_rows <= 400)
                else:
                    self.assertEqual(case.scale_rows, 0)
                retention = [f for f in case.noise if f.kind == "log-retention"]
                if retention:
                    self.assertIn(case.log_retention, (300, 400, 500))
                    self.assertEqual(case.log_retention, retention[0].params["limit"])
                else:
                    self.assertIsNone(case.log_retention)
                if case.misleading_alert is not None:
                    self.assertEqual(set(case.misleading_alert), {"service", "severity", "message"})
                    text = json.dumps(case.misleading_alert).lower()
                    for name in POOL_A | POOL_B | POOL_C | POOL_D | POOL_E | POOL_F:
                        self.assertNotIn(name, text)

    def test_invalid_arguments_are_rejected(self):
        for arguments in ((0, 0, 1), (4, 0, 1), (1, "0", 1), (1, 0, 1.0)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                hardcases.generate_case(*arguments)


if __name__ == "__main__":
    unittest.main()
