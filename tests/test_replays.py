"""The replay viewer's data matches the recorded evidence, and the page serves it."""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "airline_recovery" / "openenv_adapter" / "replays.json"


class ReplayDataTests(unittest.TestCase):
    def test_shipped_data_is_rebuilt_from_evidence(self):
        built = subprocess.run([sys.executable, "-c", "import json,sys; sys.path.insert(0,'scripts'); import build_replays;"
                                "print(json.dumps(build_replays.build(), separators=(',',':'), sort_keys=True))"],
                               cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(json.loads(built), json.loads(DATA.read_text()))

    def test_counts_match_the_recorded_runs(self):
        data = json.loads(DATA.read_text())
        by_agent = {}
        for episode in data["episodes"]:
            by_agent.setdefault(episode["agent"], []).append(episode)
            self.assertTrue(episode["steps"], episode["agent"] + episode["task"])
        for agent, episodes in by_agent.items():
            records = (ROOT / "evidence/v0.5.0/hard" / ("oracle" if agent == "oracle" else f"agents/{agent}")
                       / "episodes.jsonl").read_text().splitlines()
            self.assertEqual(len(episodes), len(records))
            self.assertEqual(sum(e["success"] for e in episodes), sum(json.loads(r)["success"] for r in records))


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"), "needs the openenv extra")
class ReplayRouteTests(unittest.TestCase):
    def test_page_and_data_are_served(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from airline_recovery.openenv_adapter.replays import register
        app = FastAPI()
        register(app)
        client = TestClient(app)
        page = client.get("/replays")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Play it yourself", page.text)
        data = client.get("/replays/data.json").json()
        self.assertEqual(len(data["episodes"]), len(json.loads(DATA.read_text())["episodes"]))


if __name__ == "__main__":
    unittest.main()
