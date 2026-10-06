"""The external-agent runner grades a CLI agent only through the sidecar's signed receipt."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from airline_recovery.live import external

ROOT = Path(__file__).resolve().parents[1]
# A stand-in "agent CLI": drives the reference policy through control.py like a coding agent would.
SCRIPTED = f'''import json, subprocess, sys
sys.path.insert(0, {str(ROOT)!r})
from airline_recovery.live.policies import ReferencePolicy
def call(action=None):
    args = [sys.executable, "control.py"] + (["action", json.dumps(action)] if action else [])
    return json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)
policy, transition = ReferencePolicy(), call()
while not (transition["terminated"] or transition["truncated"]):
    transition = call(policy(transition["observation"]))
print(json.dumps({{"num_turns": 1, "total_cost_usd": 0.0, "usage": {{"input_tokens": 1, "output_tokens": 1}}}}))
'''


class ExternalRunnerTests(unittest.TestCase):
    def test_scripted_cli_agent_is_graded_from_the_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "agent.py")
            script.write_text(SCRIPTED)
            fake = {"default_model": "scripted",
                    "command": lambda model, text, turns: [sys.executable, str(script)]}
            with mock.patch.dict(external.AGENTS, {"claude-code": fake}):
                summary = external.run("claude-code", None, "train", [1], Path(tmp, "out"), timeout=600,
                                       turns=10, index=1, keep=True)
            self.assertEqual(summary["all"]["successes"], 1)
            self.assertEqual(summary["unfinished_episodes"], 0)
            record = json.loads(Path(tmp, "out", "episodes.jsonl").read_text())
            self.assertTrue(record["finished"] and record["success"])
            self.assertEqual(record["reward"], 1.0)
            self.assertEqual(record["agent_actions"], record["steps"])
            self.assertEqual(record["usage"]["cost_usd"], 0.0)
            agent_dir = Path(tmp, "out", "episodes", "train-001-seed1", "agent")
            self.assertEqual({p.name for p in agent_dir.iterdir()}, {"control.py", "episode.json", "agent.stdout", "agent.stderr"})
            provenance = json.loads(Path(tmp, "out", "provenance.json").read_text())
            self.assertEqual(provenance["trust_boundary"], "external-process")

    def test_agent_that_never_finishes_scores_zero_without_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "agent.py")
            script.write_text("import subprocess,sys\nsubprocess.run([sys.executable,'control.py','action','{\"tool\":\"get_metrics\",\"arguments\":{}}'],check=True)\n")
            fake = {"default_model": "scripted", "command": lambda model, text, turns: [sys.executable, str(script)]}
            with mock.patch.dict(external.AGENTS, {"codex": fake}):
                summary = external.run("codex", None, "train", [2], Path(tmp, "out"), timeout=600, turns=10, index=1)
            record = json.loads(Path(tmp, "out", "episodes.jsonl").read_text())
            self.assertFalse(record["finished"])
            self.assertEqual(record["reward"], 0.0)
            self.assertIn("No signed receipt", record["grade_error"])
            self.assertEqual(summary["unfinished_episodes"], 1)

    def test_repeated_seeds_are_rejected_before_any_episode_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = {"default_model": "scripted", "command": lambda model, text, turns: [sys.executable, "-c", "pass"]}
            with mock.patch.dict(external.AGENTS, {"codex": fake}):
                for seeds in ([1, 1], []):
                    with self.subTest(seeds=seeds), self.assertRaisesRegex(ValueError, "unique integer seeds"):
                        external.run("codex", None, "train", seeds, Path(tmp, "out"), timeout=60, turns=1, index=0)
            self.assertFalse(Path(tmp, "out").exists())


class ExternalTimeoutTests(unittest.TestCase):
    def test_timed_out_agent_and_its_children_are_stopped(self):
        import os, time
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp, "child.pid")
            script = Path(tmp, "agent.py")
            # The "agent" starts a helper that would outlive a plain kill of the parent.
            script.write_text(f"import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
                              f"open({str(pidfile)!r},'w').write(str(p.pid))\ntime.sleep(120)\n")
            fake = {"default_model": "scripted", "command": lambda model, text, turns: [sys.executable, str(script)]}
            with mock.patch.dict(external.AGENTS, {"codex": fake}):
                external.run("codex", None, "train", [1], Path(tmp, "out"), timeout=5, turns=1, index=1)
            record = json.loads(Path(tmp, "out", "episodes.jsonl").read_text())
            self.assertIn("timed out", record["error"])
            child = int(pidfile.read_text())
            time.sleep(0.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)


if __name__ == "__main__":
    unittest.main()
