"""Per-action training feedback must reconcile with terminal episode accounting."""
import unittest

from airline_recovery.live.environment import LiveAirlineEnv


class LiveActionCostTests(unittest.TestCase):
    def test_failed_and_successful_tools_share_one_consistent_cost_ledger(self):
        actions = [
            {"tool": "patch_config", "arguments": {"service": "checkin", "values": {"consumer_enabled": "yes"}}},
            {"tool": "quarantine_event", "arguments": {"event_id": "missing-review-event"}},
            {"tool": "patch_config", "arguments": {"service": "checkin", "values": {"consumer_enabled": True}}},
            {"tool": "query_sql", "arguments": {"query": "DELETE FROM bookings"}},
            {"tool": "get_metrics", "arguments": {}},
            {"tool": "probe", "arguments": {}},
            {"tool": "probe", "arguments": {}},
            {"tool": "finish", "arguments": {}},
        ]
        with LiveAirlineEnv(max_steps=8) as environment:
            environment.reset(seed=19, options={"split": "train", "index": 1})
            costs, accepted = [], []
            for action in actions:
                observation, _, terminated, truncated, info = environment.step(action)
                costs.append(info["action_cost"])
                accepted.append(observation["result"]["ok"])
            self.assertEqual(accepted, [False, False, True, False, True, True, True, True])
            self.assertTrue(terminated)
            self.assertFalse(truncated)
            self.assertTrue(info["score"]["success"])
            self.assertEqual(info["score"]["details"]["mutations"], 1)
            self.assertAlmostEqual(sum(costs), info["score"]["cost"])
            self.assertAlmostEqual(info["score"]["cost"], 0.018)
            self.assertAlmostEqual(costs[0], 0.001)
            self.assertAlmostEqual(costs[1], 0.001)
            self.assertAlmostEqual(costs[2], 0.011)


if __name__ == "__main__":
    unittest.main()
