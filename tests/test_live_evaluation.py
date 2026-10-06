"""Evaluation trust boundaries, episode lifecycle and observation-only policies."""
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from airline_recovery.live.evaluate import aggregate, export_sft, main, run_evaluation, wilson_interval
from airline_recovery.live.policies import ReferencePolicy, load_policy


class RecordingEnv:
    instances = []
    resets = []

    def __init__(self, max_steps=48):
        self.closed = False
        self.max_steps = max_steps
        self.instances.append(self)

    def task_manifest(self):
        return {name: [{"id": f"opaque-{name}", "index": 0}] for name in ("train", "eval", "test")}

    def tools(self):
        return [{"name": "finish", "parameters": {"type": "object"}}]

    def reset(self, *, seed, options):
        self.resets.append((options["split"], options["index"], seed))
        return {"episode_id": "public", "step": 0, "result": None}, {"private_marker": "do-not-send-to-policy"}

    def step(self, action):
        assert action["tool"] in {"finish", "probe"}
        score = {"success": True, "reward": 0.9, "cost": 0.1, "details": {"requests": 12}}
        return {"step": 1, "result": {"ok": True}}, 0.9, True, False, {"score": score}

    def close(self):
        self.closed = True


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        RecordingEnv.instances = []
        RecordingEnv.resets = []
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_eval(self, **kwargs):
        return run_evaluation(policy="nop", split="all", seeds=[7, 11],
                              output=self.directory.name, env_factory=RecordingEnv,
                              progress=False, **kwargs)

    def test_matched_grid_terminal_flags_requests_and_cleanup(self):
        summary = self.run_eval()
        self.assertEqual(RecordingEnv.resets, [(split, 0, seed)
                         for split in ("train", "eval", "test") for seed in (7, 11)])
        self.assertTrue(all(env.closed for env in RecordingEnv.instances))
        self.assertEqual(summary["all"]["episodes"], 6)
        self.assertEqual(summary["all"]["terminated"], 6)
        self.assertEqual(summary["all"]["truncated"], 0)
        self.assertEqual(summary["all"]["mean_requests"], 12)
        self.assertEqual(summary["all"]["mean_tool_calls"], 1)
        self.assertGreater(summary["all"]["mean_wall_seconds"], 0)
        rows = [json.loads(line) for line in Path(self.directory.name, "trajectories.jsonl").read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows[:3]], ["reset", "step", "episode_end"])
        with self.assertRaises(FileExistsError):
            self.run_eval()

    def test_policy_only_receives_detached_observation(self):
        received = []
        def actor(observation):
            received.append(observation)
            self.assertNotIn("private_marker", observation)
            self.assertIn("available_tools", observation)
            observation["step"] = 1000
            return {"tool": "finish", "arguments": {}}
        with patch("airline_recovery.live.evaluate.load_policy", return_value=actor):
            self.run_eval()
        self.assertEqual(len(received), 6)
        first = json.loads(Path(self.directory.name, "trajectories.jsonl").read_text().splitlines()[0])
        self.assertEqual(first["observation"]["step"], 0)

    def test_policy_errors_remain_failed_and_close_all_workers(self):
        def broken(observation):
            raise RuntimeError("policy failed")
        with patch("airline_recovery.live.evaluate.load_policy", return_value=broken):
            summary = self.run_eval()
        self.assertEqual(summary["all"]["errors"], 6)
        self.assertEqual(summary["all"]["successes"], 0)
        self.assertEqual(summary["all"]["terminated"], 0)
        self.assertTrue(all(env.closed for env in RecordingEnv.instances))

    def test_environment_truncation_is_not_relabelled_as_termination(self):
        class TruncatingEnv(RecordingEnv):
            def step(self, action):
                return {"step": 1}, 0.2, False, True, {"score": {"success": False, "reward": 0.2}}
        summary = run_evaluation(policy="nop", split="train", seeds=[1], output=self.directory.name,
                                 env_factory=TruncatingEnv, progress=False)
        self.assertEqual(summary["all"]["truncated"], 1)
        self.assertEqual(summary["all"]["terminated"], 0)
        self.assertEqual(summary["all"]["errors"], 0)
        self.assertIsNone(summary["all"]["mean_requests"])

    def test_sft_export_excludes_eval_test_failures_and_grader_info(self):
        self.run_eval()
        source = Path(self.directory.name, "trajectories.jsonl")
        with source.open("a") as handle:
            for row in [
                {"policy": "nop", "run_id": "failed", "split": "train", "event": "reset", "observation": {"step": 0}},
                {"policy": "nop", "run_id": "failed", "split": "train", "event": "episode_end", "success": False},
            ]:
                handle.write(json.dumps(row) + "\n")
        destination = Path(self.directory.name, "sft.jsonl")
        result = export_sft(source, destination)
        self.assertEqual(result["successful_train_episodes_exported"], 2)
        content = destination.read_text()
        self.assertNotIn("private_marker", content)
        self.assertNotIn("details", content)
        self.assertNotIn('"split": "eval"', content)
        self.assertNotIn('"split": "test"', content)
        rows = [json.loads(line) for line in content.splitlines()]
        self.assertEqual([message["role"] for message in rows[0]["messages"]], ["system", "user", "assistant"])

    def test_wilson_interval_and_empty_results(self):
        low, high = wilson_interval(5, 10)
        self.assertAlmostEqual(low, 0.236593, places=5)
        self.assertAlmostEqual(high, 0.763407, places=5)
        self.assertIsNone(wilson_interval(0, 0))
        self.assertIsNone(aggregate([])["pass_rate"])
        self.assertLess(wilson_interval(0, 1)[1], 1)
        self.assertGreater(wilson_interval(1, 1)[0], 0)

    def test_plugin_contract(self):
        self.assertTrue(callable(load_policy("examples.custom_agent:agent")))
        with self.assertRaises(ValueError):
            load_policy("not-a-plugin")

    def test_invalid_grid_does_not_leave_provenance_blocking_corrected_run(self):
        with self.assertRaises(ValueError):
            self.run_eval(index=999)
        self.assertFalse(Path(self.directory.name, "provenance.json").exists())
        self.assertTrue(all(env.closed for env in RecordingEnv.instances))
        self.assertEqual(self.run_eval()["all"]["episodes"], 6)

    def test_seed_and_index_types_are_checked_before_starting_workers(self):
        for seeds, index in (([True], None), ([1.0], None), ([1], True)):
            with self.subTest(seeds=seeds, index=index), self.assertRaises(ValueError):
                run_evaluation(policy="nop", split="train", seeds=seeds, index=index,
                               output=self.directory.name, env_factory=RecordingEnv, progress=False)
        self.assertEqual(RecordingEnv.instances, [])


class NativeSFTExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name, "trajectories.jsonl")
        self.output = Path(self.directory.name, "tools.jsonl")

    def episode(self, key="train:0:1", split="train", success=True):
        tools = [{"name": "get_logs", "description": "Read public logs.",
                  "parameters": {"type": "object", "properties": {"service": {"type": "string"}}}},
                 {"name": "finish", "parameters": {"type": "object", "properties": {}}}]
        meta = {"run_id": key, "policy": "reference", "split": split}
        rows = [{**meta, "event": "reset", "step": 0,
                 "observation": {"episode_id": "public", "step": 0, "result": None, "available_tools": tools,
                                 "private_fault": "PRIVATE_FAULT_MARKER", "reward": "PRIVATE_REWARD_MARKER",
                                 "metadata": {"score": "PRIVATE_SCORE_MARKER"}},
                 "info": {"hidden": "PRIVATE_RESET_MARKER"}}]
        for number, (tool, arguments) in enumerate((("get_logs", {"service": "booking"}), ("finish", {})), start=1):
            rows.append({**meta, "event": "step", "step": number,
                         "action": {"tool": tool, "arguments": arguments},
                         "observation": {"step": number, "summary": {"pending_bookings": 0},
                                         "result": {"tool": tool, "ok": True, "data": {"public": "kept"}},
                                         "metadata": {"score": "PRIVATE_SCORE_MARKER"},
                                         "reward": "PRIVATE_REWARD_MARKER", "score": "PRIVATE_SCORE_MARKER"},
                         "reward": 1 if number == 2 else 0, "terminated": number == 2, "truncated": False,
                         "info": {"score": "PRIVATE_GRADER_MARKER"}})
        rows.append({**meta, "event": "episode_end", "success": success, "steps": 2,
                     "terminated": True, "truncated": False, "error": None,
                     "score": {"success": success, "details": "PRIVATE_GRADER_MARKER"}})
        return rows

    def write(self, rows):
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_native_export_has_schemas_ordered_calls_and_matched_responses(self):
        self.write(self.episode())
        self.assertEqual(export_sft(self.source, self.output, format="trl-tools")["successful_train_episodes_exported"], 1)
        record = json.loads(self.output.read_text())
        self.assertEqual(record["metadata"]["split"], "train")
        self.assertEqual([tool["function"]["name"] for tool in record["tools"]], ["get_logs", "finish"])
        self.assertTrue(all(tool["type"] == "function" for tool in record["tools"]))
        messages = record["messages"]
        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "tool", "assistant", "tool"])
        identifiers = []
        for call_message, response in zip(messages[2::2], messages[3::2]):
            call = call_message["tool_calls"][0]
            identifiers.append(call["id"])
            self.assertEqual(call["id"], response["tool_call_id"])
            self.assertEqual(call["function"]["name"], response["name"])
            self.assertIsInstance(call["function"]["arguments"], dict)
            self.assertEqual(json.loads(response["content"])["result"]["data"], {"public": "kept"})
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertNotIn("PRIVATE_", self.output.read_text())
        self.assertNotIn("available_tools", json.loads(messages[1]["content"]))

    def test_both_formats_filter_heldout_failed_incomplete_and_mixed_split_episodes(self):
        rows = self.episode()
        rows += self.episode("eval:0:1", split="eval")
        rows += self.episode("test:0:1", split="test")
        rows += self.episode("failed", success=False)
        rows += self.episode("incomplete")[:-1]
        truthy = self.episode("truthy")
        truthy[-1]["success"] = "true"
        rows += truthy
        mixed = self.episode("mixed")
        mixed[1]["split"] = "eval"
        rows += mixed
        errored = self.episode("errored")
        errored[-1]["error"] = "policy failure"
        rows += errored
        inconsistent_score = self.episode("inconsistent-score")
        inconsistent_score[-1]["score"]["success"] = False
        rows += inconsistent_score
        self.write(rows)
        for format in ("json-actions", "trl-tools"):
            with self.subTest(format=format):
                result = export_sft(self.source, self.output, format=format)
                self.assertEqual(result["successful_train_episodes_exported"], 1)
                self.assertEqual(json.loads(self.output.read_text())["metadata"]["run_id"], "train:0:1")
                self.assertNotIn("PRIVATE_", self.output.read_text())

    def test_success_at_environment_step_limit_is_still_exportable(self):
        rows = self.episode()
        rows[-2]["action"] = {"tool": "get_logs", "arguments": {"service": "booking"}}
        rows[-2]["observation"]["result"]["tool"] = "get_logs"
        for row in rows[-2:]:
            row["terminated"], row["truncated"] = False, True
        self.write(rows)
        for format in ("json-actions", "trl-tools"):
            with self.subTest(format=format):
                result = export_sft(self.source, self.output, format=format)
                self.assertEqual(result["successful_train_episodes_exported"], 1)

    def test_default_remains_json_actions(self):
        self.write(self.episode())
        export_sft(self.source, self.output)
        record = json.loads(self.output.read_text())
        self.assertNotIn("tools", record)
        self.assertEqual([message["role"] for message in record["messages"]], ["system", "user", "assistant", "user", "assistant"])
        self.assertEqual(json.loads(record["messages"][2]["content"]), {"tool": "get_logs", "arguments": {"service": "booking"}})

    def test_corrupt_chronology_and_undeclared_tools_preserve_existing_output(self):
        for change in ("step", "tool", "schemas", "response", "terminal"):
            with self.subTest(change=change):
                rows = self.episode()
                if change == "step":
                    rows[2]["step"] = 3
                elif change == "tool":
                    rows[1]["action"]["tool"] = "not_declared"
                elif change == "schemas":
                    rows[0]["observation"].pop("available_tools")
                elif change == "response":
                    rows[1]["observation"]["result"]["tool"] = "finish"
                else:
                    rows[2]["terminated"] = False
                self.write(rows)
                self.output.write_text("preserve me")
                with self.assertRaises(ValueError):
                    export_sft(self.source, self.output, format="trl-tools")
                self.assertEqual(self.output.read_text(), "preserve me")

    def test_source_cannot_be_overwritten_and_format_is_validated(self):
        self.write(self.episode())
        before = self.source.read_text()
        with self.assertRaises(ValueError):
            export_sft(self.source, self.source)
        self.assertEqual(self.source.read_text(), before)
        with self.assertRaises(ValueError):
            export_sft(self.source, self.output, format="unknown")

    def test_cli_forwards_native_format_choice(self):
        with patch("airline_recovery.live.evaluate.run_evaluation", return_value={"all": {"errors": 0}}), \
                patch("airline_recovery.live.evaluate.export_sft", return_value={}) as exporter, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--output", self.directory.name, "--export-sft", str(self.output),
                                   "--sft-format", "trl-tools"]), 0)
        exporter.assert_called_once_with(self.source, str(self.output), format="trl-tools")


class ReferencePolicyTests(unittest.TestCase):
    def observe(self, policy, responses):
        selected = policy({"step": 0, "result": None})
        actions = [selected]
        for step in range(1, 40):
            tool = selected["tool"]
            if tool == "finish":
                break
            key = tool
            if tool == "query_sql":
                key = "pending" if "FROM bookings" in selected["arguments"]["query"] else "outbox"
            selected = policy({"step": step, "result": {"tool": tool, "ok": True, "data": responses.get(key, {})}})
            actions.append(selected)
        return actions

    def test_derives_timeout_from_observed_latency_and_reconciles_pending(self):
        actions = self.observe(ReferencePolicy(max_rounds=1), {
            "get_config": {"booking": {"payment_timeout_ms": 50}, "payment": {"provider_latency_ms": 370}},
            "pending": [{"booking_id": "observed-booking"}],
        })
        patch_action = next(row for row in actions if row["tool"] == "patch_config")
        self.assertGreater(patch_action["arguments"]["values"]["payment_timeout_ms"], 370)
        reconcile = next(row for row in actions if row["tool"] == "reconcile_booking")
        self.assertEqual(reconcile["arguments"]["booking_id"], "observed-booking")
        self.assertEqual(actions[-1]["tool"], "finish")

    def test_valid_new_schema_is_upgraded_not_quarantined(self):
        payload = {"schema_version": 2, "booking_id": "B1", "flight_id": "F100", "passenger_id": "P1"}
        actions = self.observe(ReferencePolicy(max_rounds=1), {
            "get_config": {"checkin": {"accepted_schema": 1, "consumer_enabled": True}},
            "outbox": [{"event_id": "good", "booking_id": "B1", "payload": json.dumps(payload)},
                       {"event_id": "bad", "booking_id": "B1", "payload": "{broken"}],
        })
        self.assertEqual([row["arguments"]["event_id"] for row in actions if row["tool"] == "quarantine_event"], ["bad"])
        self.assertIn({"tool": "patch_config", "arguments": {"service": "checkin", "values": {"accepted_schema": 2}}}, actions)

    def test_stopped_service_uses_public_metrics(self):
        actions = self.observe(ReferencePolicy(max_rounds=1), {
            "get_metrics": {"services": {"pricing": {"process_running": False}}},
        })
        self.assertIn({"tool": "restart_service", "arguments": {"service": "pricing"}}, actions)

    def test_verification_requires_two_public_healthy_windows(self):
        policy = ReferencePolicy(max_rounds=1)
        selected = policy({"step": 0})
        step = 0
        while selected["tool"] != "probe":
            step += 1
            selected = policy({"step": step, "result": {"tool": selected["tool"], "ok": True, "data": {}}})
        selected = policy({"step": step + 1, "result": {"tool": "probe", "ok": True,
                                                       "data": {"healthy": True, "verification_windows": 1}}})
        self.assertEqual(selected["tool"], "probe")
        selected = policy({"step": step + 2, "result": {"tool": "probe", "ok": True,
                                                       "data": {"healthy": True, "verification_windows": 2}}})
        self.assertEqual(selected["tool"], "finish")


if __name__ == "__main__":
    unittest.main()
