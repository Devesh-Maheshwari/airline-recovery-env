"""Regressions from the concurrent fairness and implementation reviews."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from airline_recovery.live.environment import LiveAirlineEnv
from airline_recovery.live.evaluate import export_sft, run_evaluation
from airline_recovery.live.store import configuration_contracts


def act(env, tool, **arguments):
    return env.step({"tool": tool, "arguments": arguments})


class ReviewContractTests(unittest.TestCase):
    def test_public_probe_boundary_matches_executed_success(self):
        with LiveAirlineEnv() as env:
            initial, _ = env.reset(seed=11, options={"split": "train", "index": 1})
            contract = initial["episode_contract"]
            self.assertEqual(contract["verification_eligible_from_step"], 6)
            self.assertEqual(contract["required_healthy_probes"], 2)
            act(env, "patch_config", service="checkin", values={"consumer_enabled": True})
            for _ in range(3):
                act(env, "get_metrics")
            for expected_step, expected_count in ((5, 0), (6, 1), (7, 2)):
                observation, *_ = act(env, "probe")
                self.assertEqual(observation["step"], expected_step)
                self.assertTrue(observation["result"]["data"]["healthy"])
                self.assertEqual(observation["summary"]["verification_windows"], expected_count)
            self.assertTrue(act(env, "finish")[4]["score"]["success"])

    def test_configuration_discovery_exposes_limits_not_baselines_and_is_detached(self):
        with LiveAirlineEnv() as env:
            initial, _ = env.reset(seed=11, options={"split": "train", "index": 1})
            contracts = initial["configuration_contracts"]
            for service, contract in contracts.items():
                self.assertEqual(set(contract["observed_fields"]), set(env.stack.get_config(service)))
                for field in contract["observed_fields"].values():
                    self.assertTrue({"default", "example", "baseline", "reference"}.isdisjoint(field))
            booking = contracts["booking"]["patch_schema"]["properties"]["payment_timeout_ms"]
            observation, *_ = act(env, "patch_config", service="booking",
                                 values={"payment_timeout_ms": booking["maximum"] + 1})
            self.assertFalse(observation["result"]["ok"])
            payment = contracts["payment"]
            self.assertTrue(payment["observed_fields"]["provider_latency_ms"]["readOnly"])
            self.assertNotIn("provider_latency_ms", payment["patch_schema"]["properties"])
            booking["maximum"] = -1
            self.assertEqual(configuration_contracts()["booking"]["patch_schema"]["properties"]["payment_timeout_ms"]["maximum"], 10000)

    def test_evaluator_retains_malformed_tool_feedback_and_allows_recovery(self):
        for invalid_tool in ([], {}):
            with self.subTest(tool=invalid_tool), tempfile.TemporaryDirectory() as directory:
                actions = iter([
                    {"tool": invalid_tool, "arguments": {}},
                    {"tool": "patch_config", "arguments": {"service": "checkin", "values": {"consumer_enabled": True}}},
                    *[{"tool": "probe", "arguments": {}} for _ in range(5)],
                    {"tool": "finish", "arguments": {}},
                ])
                with patch("airline_recovery.live.evaluate.load_policy", return_value=lambda observation: next(actions)):
                    summary = run_evaluation(policy="review", split="train", index=1, seeds=[11],
                                             output=directory, progress=False)
                self.assertEqual(summary["all"]["successes"], 1)
                self.assertEqual(summary["all"]["errors"], 0)
                rows = [json.loads(line) for line in (Path(directory) / "trajectories.jsonl").read_text().splitlines()]
                self.assertFalse(rows[1]["observation"]["result"]["ok"])
                self.assertEqual(rows[1]["action"]["tool"], invalid_tool)
                self.assertEqual(rows[-1]["steps"], 8)

    def test_both_sft_formats_preserve_public_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_evaluation(policy="reference", split="train", index=1, seeds=[11],
                           output=root, progress=False)
            initial = json.loads((root / "trajectories.jsonl").read_text().splitlines()[0])["observation"]
            for format in ("json-actions", "trl-tools"):
                target = root / (format + ".jsonl")
                export_sft(root / "trajectories.jsonl", target, format=format)
                record = json.loads(target.read_text())
                observed = json.loads(record["messages"][1]["content"])
                for key in ("episode_contract", "configuration_contracts"):
                    self.assertEqual(observed[key], initial[key])
                self.assertNotIn("score", observed)
                self.assertNotIn("metadata", observed)


if __name__ == "__main__":
    unittest.main()
