"""Executed migration episodes distinguish availability from payment integrity."""
import unittest

from airline_recovery.live.environment import LiveAirlineEnv
from airline_recovery.live.policies import BlanketPolicy, ReferencePolicy
from airline_recovery.live.store import configuration_contracts


def act(env, tool, **arguments):
    return env.step({"tool":tool, "arguments":arguments})


class PaymentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.env = LiveAirlineEnv()
        self.addCleanup(self.env.close)
        # Seed 7 interrupts three bookings: one charged under each key version, one never charged.
        self.observation, _ = self.env.reset(seed=7, options={"split":"train", "index":4})

    def rows(self):
        return self.env.stack.query("SELECT b.booking_id,b.amount_cents,c.charge_id,c.idempotency_key "
            "FROM bookings b LEFT JOIN charges c ON c.booking_id=b.booking_id WHERE b.status='pending' ORDER BY b.booking_id")

    def finish_with(self, policy):
        observation = self.observation
        while not self.env.done:
            observation, reward, _, _, info = self.env.step(policy(observation))
        return reward, info["score"]

    def test_mixed_states_are_executed_and_initially_valid(self):
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["charge_id"] is not None for row in rows), 2)
        self.assertEqual(len({row["amount_cents"] for row in rows}), 1)
        self.assertEqual({row["idempotency_key"].split(':')[0] for row in rows if row["charge_id"]}, {"booking", "booking-v2"})
        self.assertTrue(self.env._score()["integrity"])
        logs = self.env.stack.logs("booking", limit=100)
        self.assertGreaterEqual(sum(row["status"] == 504 for row in logs), 2)
        self.assertGreaterEqual(sum(row["status"] == 503 for row in logs), 1)

    def test_blanket_repair_creates_duplicate_payment_even_with_healthy_traffic(self):
        reward, score = self.finish_with(BlanketPolicy())
        self.assertEqual(reward, 0)
        self.assertEqual(score["availability"], 1)
        self.assertFalse(score["integrity"])
        self.assertEqual(score["details"]["business_impact"]["duplicate_charge_bookings"], 1)
        self.assertGreater(score["details"]["business_impact"]["excess_capture_cents"], 0)
        self.assertTrue(any(v.startswith("duplicate_charge:") for v in score["details"]["violations"]))

    def test_both_evidence_based_recoveries_receive_full_credit(self):
        for recovery in ("retry", "adopt"):
            with self.subTest(recovery=recovery):
                self.observation, _ = self.env.reset(seed=19, options={"split":"train", "index":4})
                reward, score = self.finish_with(ReferencePolicy(recovery=recovery))
                self.assertEqual(reward, 1)
                self.assertTrue(score["success"])
                self.assertEqual(score["details"]["business_impact"]["excess_capture_cents"], 0)

    def test_foreign_charge_rejects_without_changing_target_or_integrity(self):
        charged = [row for row in self.rows() if row["charge_id"]]
        first, other = charged
        before = self.env.stack.query(f"SELECT * FROM bookings WHERE booking_id='{first['booking_id']}'")
        obs, *_ = act(self.env, "reconcile_booking", booking_id=first["booking_id"], existing_charge_id=other["charge_id"])
        self.assertFalse(obs["result"]["ok"])
        self.assertIn("HTTP 409", obs["result"]["error"])
        self.assertEqual(before, self.env.stack.query(f"SELECT * FROM bookings WHERE booking_id='{first['booking_id']}'"))
        self.assertTrue(self.env._score()["integrity"])

    def test_adoption_is_idempotent_and_preserves_accepted_fare(self):
        row = next(row for row in self.rows() if row["charge_id"])
        self.env.stack.admin_execute("UPDATE flights SET price_cents=price_cents+1000,version=version+1")
        for _ in range(2):
            result = self.env.stack.reconcile(row["booking_id"], existing_charge_id=row["charge_id"])
            self.assertEqual(result["status"], 200)
        charges = self.env.stack.query(f"SELECT * FROM charges WHERE booking_id='{row['booking_id']}'")
        events = self.env.stack.query(f"SELECT * FROM outbox WHERE booking_id='{row['booking_id']}'")
        self.assertEqual(len(charges), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(charges[0]["amount_cents"], row["amount_cents"])

    def test_mutually_exclusive_options_reject_before_capture(self):
        row = next(row for row in self.rows() if row["charge_id"])
        obs, *_ = act(self.env, "reconcile_booking", booking_id=row["booking_id"],
                     idempotency_key=row["idempotency_key"], existing_charge_id=row["charge_id"])
        self.assertFalse(obs["result"]["ok"])
        self.assertTrue(self.env._score()["integrity"])
        self.assertEqual(len(self.rows()), 3)

    def test_public_contract_discloses_version_and_recovery_options(self):
        schema = configuration_contracts()["booking"]["patch_schema"]["properties"]["payment_key_version"]
        self.assertEqual((schema["minimum"], schema["maximum"]), (1, 2))
        tool = next(tool for tool in self.observation["available_tools"] if tool["name"] == "reconcile_booking")
        self.assertIn("existing_charge_id", tool["parameters"]["properties"])
        self.assertIn("idempotency_key", tool["parameters"]["properties"])
        self.assertIn("mutually exclusive", tool["description"])
        for field in tool["parameters"]["properties"].values():
            self.assertEqual(field["minLength"], 1)


if __name__ == "__main__":
    unittest.main()
