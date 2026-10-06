"""The Harbor verifier trusts only the sidecar's signed receipt of the one real episode."""
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from airline_recovery.live import harbor
from airline_recovery.live.policies import ReferencePolicy

ROOT = Path(__file__).resolve().parents[1]


def call(url, action=None):
    request = urllib.request.Request(url + ("/observation" if action is None else "/step"),
                                     data=None if action is None else json.dumps(action).encode(),
                                     headers={"Content-Type":"application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        return json.load(response)


class HarborReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        harbor.build(cls.root / "tasks")
        cls.task = cls.root / "tasks" / "train" / "airline-recovery-train-004"
        cls.other = cls.root / "tasks" / "train" / "airline-recovery-train-001"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        world = cls.task / "environment" / f"world-{cls.task.name}"
        cls.bridge = subprocess.Popen(
            [sys.executable, "-m", "airline_recovery.live.bridge", "--spec", str(world / "task_spec.json"),
             "--key", str(world / "receipt.key"), "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env={**os.environ, "PYTHONPATH":str(ROOT)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                transition = call(cls.url)
                break
            except OSError:
                if time.monotonic() > deadline or cls.bridge.poll() is not None:
                    raise
                time.sleep(0.1)
        # The oracle: an observation-driven policy against the sidecar's only episode.
        policy, cls.actions = ReferencePolicy(), []
        while not (transition["terminated"] or transition["truncated"]):
            cls.actions.append(policy(transition["observation"]))
            transition = call(cls.url, cls.actions[-1])
        cls.final = transition

    @classmethod
    def tearDownClass(cls):
        cls.bridge.terminate()
        cls.bridge.wait(timeout=10)
        cls.temporary.cleanup()

    def grade(self, artifact, task=None):
        """Run the generated grade.py with its container paths mapped into a sandbox."""
        task = task or self.task
        with tempfile.TemporaryDirectory() as sandbox:
            app, logs = Path(sandbox, "app"), Path(sandbox, "logs")
            app.mkdir()
            (app / "episode.json").write_text(artifact if isinstance(artifact, str) else json.dumps(artifact))
            source = (task / "tests" / "grade.py").read_text()
            for container, local in (("/tests/", f"{task / 'tests'}/"), ("/app/episode.json", str(app / "episode.json")),
                                     ("/logs/verifier", str(logs))):
                source = source.replace(container, local)
            subprocess.run([sys.executable, "-I", "-c", source], check=True, timeout=30)
            return json.loads((logs / "reward.json").read_text()), json.loads((logs / "details.json").read_text())

    def artifact(self):
        return {"schema_version":3, "actions":copy.deepcopy(self.actions), "receipt":copy.deepcopy(self.final["receipt"])}

    def test_oracle_episode_receipt_earns_full_reward(self):
        self.assertTrue(self.final["info"]["score"]["success"])
        reward, details = self.grade(self.artifact())
        self.assertEqual(reward, {"reward":1.0, "success":1.0})
        self.assertEqual(details["steps"], len(self.actions))

    def test_action_list_without_a_receipt_scores_zero(self):
        # v0.3 replayed this list in a fresh world; a constant answer key scored 1.0.
        reward, details = self.grade({"schema_version":3, "actions":self.actions})
        self.assertEqual(reward, {"reward":0.0, "success":0.0})
        self.assertIn("No signed receipt", details["error"])

    def test_edited_or_forged_receipts_score_zero(self):
        edited = self.artifact()
        edited["receipt"]["body"]["steps"] = 1
        forged = self.artifact()
        forged["receipt"]["signature"] = "0" * 64
        claimed = {"schema_version":3, "actions":[], "receipt":{"body":{"schema_version":3,
                   "task":{"split":"train","index":4}, "score":{"reward":1.0,"success":True}}, "signature":""}}
        for name, artifact in (("edited", edited), ("forged", forged), ("claimed", claimed),
                               ("not-json", "{"), ("empty", {})):
            with self.subTest(name):
                reward, _ = self.grade(artifact)
                self.assertEqual(reward, {"reward":0.0, "success":0.0})

    def test_receipt_from_another_task_is_rejected(self):
        # Each task has its own key, so a receipt cannot be carried between tasks.
        reward, details = self.grade(self.artifact(), task=self.other)
        self.assertEqual(reward, {"reward":0.0, "success":0.0})
        self.assertIn("does not verify", details["error"])

    def test_finished_episode_cannot_be_continued_or_restarted(self):
        request = urllib.request.Request(self.url + "/step", data=json.dumps({"tool":"probe","arguments":{}}).encode())
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(call(self.url)["receipt"], self.final["receipt"])

    def test_agent_image_holds_no_key_world_code_or_solution(self):
        files = {p.name for p in (self.task / "environment").iterdir()}
        self.assertEqual(files, {"Dockerfile", "docker-compose.yaml", "control.py", "episode.json", f"world-{self.task.name}"})
        dockerfile = (self.task / "environment" / "Dockerfile").read_text()
        self.assertIn("COPY control.py episode.json /app/", dockerfile)
        self.assertNotIn("world", dockerfile)
        self.assertNotIn("seed", json.loads((self.task / "tests" / f"{self.task.name}.spec.json").read_text()))
        self.assertNotEqual((self.task / "tests" / f"{self.task.name}.receipt.key").read_text(),
                            (self.other / "tests" / f"{self.other.name}.receipt.key").read_text())


class BridgeEdgeTests(unittest.TestCase):
    def test_non_finite_action_is_rejected_unstepped_and_sigterm_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, key, scratch = root / "task_spec.json", root / "receipt.key", root / "tmp"
            spec.write_text(json.dumps({"split":"train", "index":1, "tier":"easy", "seed":3}))
            key.write_text("k" * 64)
            scratch.mkdir()
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            bridge = subprocess.Popen(
                [sys.executable, "-m", "airline_recovery.live.bridge", "--spec", str(spec), "--key", str(key),
                 "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env={**os.environ, "PYTHONPATH":str(ROOT), "TMPDIR":str(scratch)},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: bridge.poll() is None and bridge.kill())
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while True:
                try:
                    call(url)
                    break
                except OSError:
                    if time.monotonic() > deadline or bridge.poll() is not None:
                        raise
                    time.sleep(0.1)
            self.assertEqual(len(list(scratch.glob("airline-recovery-*"))), 1)
            # A raw client can send 1e999, which parses as inf and could never be signed into a receipt.
            raw = urllib.request.Request(url + "/step", data=b'{"tool":"get_metrics","arguments":{"service":1e999}}')
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(raw, timeout=30)
            self.assertEqual(raised.exception.code, 400)
            self.assertEqual(call(url)["observation"]["step"], 0)
            final = call(url, {"tool":"finish", "arguments":{}})
            self.assertTrue(final["terminated"])
            self.assertIn("receipt", final)
            self.assertEqual(final["receipt"]["body"]["steps"], 1)
            bridge.terminate()
            self.assertEqual(bridge.wait(timeout=20), 0)
            self.assertEqual(list(scratch.glob("airline-recovery-*")), [])


if __name__ == "__main__":
    unittest.main()
