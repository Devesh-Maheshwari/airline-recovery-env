"""The public commands run the live benchmark and present readable output."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airline_recovery.cli import _summarize_arguments, main
from airline_recovery.live.scenarios import CASES


class CliRoutingTests(unittest.TestCase):
    def run_cli(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(list(arguments))
        return output.getvalue()

    def test_list_defaults_to_current_public_task_manifest(self):
        result = json.loads(self.run_cli("list"))
        self.assertEqual(set(result), {"train", "eval", "test"})
        tasks = [task for split in result.values() for task in split]
        self.assertEqual(len(tasks), len(CASES))
        self.assertTrue(all(task["id"].startswith("airline-recovery-") for task in tasks))
        self.assertTrue(all("faults" not in task for task in tasks))

    def test_build_harbor_generates_live_task_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = json.loads(self.run_cli("build-harbor", "--output", directory, "--seed", "19"))
            self.assertEqual(result["tasks"], len(CASES))
            tasks = list(Path(directory).glob("*/*/task.toml"))
            self.assertEqual(len(tasks), len(CASES))
            for task in tasks:
                world = task.parent / "environment" / f"world-{task.parent.name}"
                self.assertTrue((world / "airline_recovery" / "live" / "worker.py").is_file())
                self.assertIn('environment_mode = "separate"', task.read_text())
            manifest = json.loads((Path(directory) / "manifest.json").read_text())
            self.assertTrue(all(task["seed"] == 19 for task in manifest["tasks"]))
            # Regeneration of a recognized live output is still supported.
            self.assertEqual(json.loads(self.run_cli("build-harbor", "--output", directory))["tasks"], len(CASES))

    def test_builder_default_destination_is_live_tasks(self):
        with patch("airline_recovery.cli._validate_live_destination"), patch("airline_recovery.live.harbor.build", return_value={}) as live:
            self.run_cli("build-harbor")
            live.assert_called_once_with("live-tasks", None)

    def test_builder_preserves_unrecognized_output(self):
        for manifest in ({"version": "0", "tasks": []}, {"tasks": [{"path": "a"}]}, None, []):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                marker = root / "do-not-overwrite.txt"
                marker.write_text("original")
                if manifest is not None:
                    (root / "manifest.json").write_text(json.dumps(manifest))
                before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    self.run_cli("build-harbor", "--output", directory)
                self.assertEqual(error.exception.code, 2)
                after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                self.assertEqual(after, before)

    def run_demo(self, *arguments):
        with tempfile.TemporaryDirectory() as directory:
            process_env = os.environ.copy()
            process_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + process_env.get("PYTHONPATH", "")
            process_env.pop("COLUMNS", None)
            result = subprocess.run([sys.executable, "-m", "airline_recovery", "demo", "--delay", "0", *arguments], cwd=directory, env=process_env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            # The demo leaves nothing behind in the working directory.
            self.assertEqual(list(Path(directory).iterdir()), [])
            return result.stdout

    def test_demo_uses_live_arguments(self):
        with patch("airline_recovery.live.environment.LiveAirlineEnv", side_effect=RuntimeError("live route")):
            with self.assertRaisesRegex(RuntimeError, "live route"):
                self.run_cli("demo", "--split", "test", "--index", "1", "--delay", "0")

    def test_demo_rejects_unknown_task_and_delay(self):
        for arguments in (("--index", "99"), ("--delay", "11")):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.run_cli("demo", *arguments)
            self.assertEqual(error.exception.code, 2)

    def test_default_demo_is_readable_and_completes_live_recovery(self):
        lines = self.run_demo().splitlines()
        self.assertIn("Task: airline-recovery-train-000   Seed: 42", lines[1])
        steps = [line.split() for line in lines if line[:4].strip().isdigit()]
        self.assertEqual([int(step[0]) for step in steps], list(range(1, len(steps) + 1)))
        self.assertEqual(steps[-1][1], "finish")
        self.assertTrue(all(step[-4] == "ok" for step in steps))
        self.assertEqual((steps[-1][-3], steps[-1][-1]), ("0", "2/2"))
        self.assertLessEqual(max(map(len, lines)), 120)
        outcome = lines.index("Outcome: SOLVED")
        self.assertEqual(lines[outcome + 1:], ["  reward: 1.0", f"  steps:  {len(steps)}"])

    def test_demo_json_emits_one_object_per_line(self):
        events = [json.loads(line) for line in self.run_demo("--json").splitlines()]
        self.assertEqual([event["event"] for event in events], ["start"] + ["step"] * (len(events) - 2) + ["outcome"])
        self.assertEqual(len(events[0]["workers"]), 5)
        self.assertEqual({key: events[0][key] for key in ("task", "split", "index", "seed")},
                         {"task": "airline-recovery-train-000", "split": "train", "index": 0, "seed": 42})
        self.assertTrue(all({"step", "action", "ok", "summary"} <= set(event) for event in events[1:-1]))
        self.assertTrue(events[-1]["solved"])
        self.assertTrue(events[-1]["score"]["success"], events[-1]["score"])
        self.assertEqual(events[-1]["score"]["details"]["steps"], len(events) - 2)

    def test_argument_summary_is_bounded(self):
        self.assertEqual(_summarize_arguments({}), "-")
        self.assertEqual(_summarize_arguments({"service": "booking", "values": {"payment_timeout_ms": 971}}),
                         'service=booking, values={"payment_timeout_ms": 971}')
        summary = _summarize_arguments({"query": "SELECT *\n  FROM bookings " + "x" * 600})
        self.assertEqual(len(summary), 60)
        self.assertTrue(summary.startswith("query=SELECT * FROM bookings") and summary.endswith("..."))

    def test_removed_commands_are_rejected(self):
        for command in ("progress", "live-demo", "generate", "init", "status", "action", "solve"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.run_cli(command)
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
