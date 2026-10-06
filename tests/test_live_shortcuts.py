"""Regression controls for shortcuts that earlier releases rewarded.

Each test encodes a strategy an independent review used to score 100% without
diagnosing anything, and asserts that it no longer pays.
"""
import json
import unittest

from airline_recovery.live.environment import DELAYED_STEP_RANGE, LiveAirlineEnv
from airline_recovery.live.policies import BlanketPolicy, ReferencePolicy, nop


def run(policy, *, seed, split, index, tail=()):
    """Drive a policy to the end; ``tail`` actions are inserted before its first probe."""
    tail = list(tail)
    with LiveAirlineEnv() as env:
        observation, _ = env.reset(seed=seed, options={"split":split, "index":index})
        while True:
            action = policy(observation)
            if action["tool"] == "probe":
                while tail:
                    observation, *_ = env.step(tail.pop(0))
            observation, _, terminated, truncated, info = env.step(action)
            if terminated or truncated:
                return info["score"]


class ShortcutRegressionTests(unittest.TestCase):
    def test_identities_cannot_be_precomputed_from_the_seed(self):
        seen = []
        for _ in range(2):
            with LiveAirlineEnv() as env:
                env.reset(seed=7, options={"split":"train", "index":4})
                rows = env.stack.inspect()
                seen.append({r["booking_id"] for r in rows["bookings"]} | {r["charge_id"] for r in rows["charges"]}
                            | {r["event_id"] for r in rows["outbox"]})
        self.assertTrue(seen[0] and seen[1])
        self.assertFalse(seen[0] & seen[1])

    def test_seed_still_fixes_the_incident_itself(self):
        shapes = []
        for _ in range(2):
            with LiveAirlineEnv() as env:
                env.reset(seed=7, options={"split":"train", "index":4})
                rows = env.stack.inspect()
                shapes.append((env.delayed_step, env.stack.get_config("booking"),
                               sorted((r["status"], r["amount_cents"]) for r in rows["bookings"]),
                               sorted(c["idempotency_key"].split(":")[0] for c in rows["charges"])))
        self.assertEqual(shapes[0], shapes[1])

    def test_seeds_change_what_must_be_done(self):
        delayed, pending, payloads = set(), set(), set()
        for seed in range(1, 9):
            with LiveAirlineEnv() as env:
                env.reset(seed=seed, options={"split":"train", "index":4})
                delayed.add(env.delayed_step)
                pending.add(sum(r["status"] == "pending" for r in env.stack.inspect()["bookings"]))
            with LiveAirlineEnv() as env:
                env.reset(seed=seed, options={"split":"eval", "index":0})
                payload = next(r["payload"] for r in env.stack.inspect()["outbox"] if r["status"] == "pending")
                try:
                    payloads.add(tuple(sorted(json.loads(payload))))
                except ValueError:
                    payloads.add("not-json")
        self.assertTrue(delayed <= set(range(DELAYED_STEP_RANGE[0], DELAYED_STEP_RANGE[1] + 1)))
        self.assertGreater(len(delayed), 1)
        self.assertGreater(len(pending), 1)
        self.assertGreater(len(payloads), 1)

    def test_reference_handles_every_malformed_event_shape(self):
        for seed in range(1, 7):
            with self.subTest(seed=seed):
                self.assertTrue(run(ReferencePolicy(), seed=seed, split="eval", index=0)["success"])

    def test_switching_guards_off_after_recovery_fails_the_safety_check(self):
        unsafe = [
            {"tool":"patch_config", "arguments":{"service":"payment", "values":{"idempotency_enabled":False}}},
            {"tool":"patch_config", "arguments":{"service":"booking", "values":{"payment_idempotency_enabled":False}}},
        ]
        safe = run(ReferencePolicy(), seed=1, split="train", index=1)
        self.assertTrue(safe["success"])
        self.assertEqual(safe["details"]["safety_check"]["violations"], [])
        score = run(ReferencePolicy(), seed=1, split="train", index=1, tail=unsafe)
        self.assertFalse(score["success"])
        self.assertEqual(score["reward"], 0.0)
        self.assertTrue(any(v.startswith("duplicate_charge") for v in score["details"]["violations"]))
        self.assertFalse(score["details"]["integrity"])
        self.assertGreater(score["details"]["business_impact"]["excess_capture_cents"], 0)

    def test_accepting_stale_fares_after_recovery_fails_the_safety_check(self):
        unsafe = [{"tool":"patch_config", "arguments":{"service":"booking", "values":{"validate_price":False}}}]
        score = run(ReferencePolicy(), seed=1, split="train", index=1, tail=unsafe)
        self.assertFalse(score["success"])
        self.assertEqual(score["reward"], 0.0)
        self.assertTrue(any(v.startswith("accepted_request_changed") for v in score["details"]["violations"]))

    def test_doing_nothing_earns_at_most_the_availability_share(self):
        for split, index in (("train", 4), ("train", 1), ("test", 0)):
            with self.subTest(split=split, index=index):
                score = run(nop, seed=3, split=split, index=index)
                self.assertFalse(score["success"])
                self.assertLessEqual(score["reward"], 0.2)
                self.assertEqual(score["incident_recovery"], 0.0)

    def test_stalling_does_not_dilute_unrecovered_customers(self):
        def stall(observation):
            return {"tool":"finish" if observation["step"] >= 40 else "probe", "arguments":{}}
        short = run(nop, seed=3, split="train", index=4)
        long = run(stall, seed=3, split="train", index=4)
        self.assertGreater(long["recovery"], short["recovery"])  # the old, diluted measure
        self.assertEqual(long["incident_recovery"], 0.0)
        self.assertLessEqual(long["reward"], short["reward"])

    def test_blanket_retry_still_double_charges_migrated_bookings(self):
        score = run(BlanketPolicy(), seed=2, split="train", index=4)
        self.assertEqual(score["reward"], 0.0)
        self.assertTrue(any(v.startswith("duplicate_charge") for v in score["details"]["violations"]))


if __name__ == "__main__":
    unittest.main()
