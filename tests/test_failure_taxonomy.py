"""The failure-cause labels are reproducible from the recorded evidence and their summary agrees with them."""
import json
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import failure_taxonomy as taxonomy  # noqa: E402

OUT = ROOT / "evidence" / "v0.5.0" / "hard" / "failure-analysis"


class FailureTaxonomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.labels, cls.summary, cls.readme = taxonomy.build()
        cls.stored = [json.loads(line) for line in (OUT / "labels.jsonl").read_text().splitlines()]
        cls.stored_summary = json.loads((OUT / "summary.json").read_text())

    def test_stored_outputs_equal_a_fresh_build(self):
        for name, text in taxonomy.serialise(self.labels, self.summary, self.readme).items():
            self.assertEqual(text, (OUT / name).read_text(), f"{name} is stale; rerun scripts/failure_taxonomy.py")

    def test_check_mode_reports_current(self):
        result = subprocess.run([sys.executable, "scripts/failure_taxonomy.py", "--check"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_failed_episode_has_exactly_one_primary(self):
        for agent in taxonomy.AGENTS:
            failed = Counter(record["run_id"] for record, _ in taxonomy.episodes(agent) if not record.get("success"))
            labelled = Counter(row["run_id"] for row in self.stored if row["agent"] == agent)
            self.assertEqual(failed, labelled, agent)
        for row in self.stored:
            self.assertIn(row["primary"], taxonomy.CAUSES)
            self.assertIsInstance(row["primary"], str)
            self.assertNotIn(row["primary"], row["secondary"])
            self.assertEqual(len(row["secondary"]), len(set(row["secondary"])))
            self.assertTrue(set(row["secondary"]) <= set(taxonomy.CAUSES) - {"stopped_unverified"})
            self.assertTrue(row["evidence"].startswith(row["primary"] + ": "))

    def test_summary_counts_match_labels(self):
        summary = self.stored_summary
        for agent, rows in [(a, [r for r in self.stored if r["agent"] == a]) for a in taxonomy.AGENTS] + [("all", self.stored)]:
            block = summary["all"] if agent == "all" else summary["agents"][agent]
            primary = Counter(r["primary"] for r in rows)
            secondary = Counter(c for r in rows for c in r["secondary"])
            self.assertEqual(block["failed"], len(rows), agent)
            self.assertEqual(block["primary"], {c: primary[c] for c in taxonomy.CAUSES}, agent)
            self.assertEqual(block["secondary"], {c: secondary[c] for c in taxonomy.CAUSES}, agent)
            self.assertEqual(sum(block["primary"].values()), len(rows), agent)
            self.assertEqual(block["groups"], {g: sum(taxonomy.CAUSES[r["primary"]][0] == g for r in rows) for g in taxonomy.GROUPS}, agent)
            self.assertEqual(block["harm_timing"]["integrity_primaries"], block["groups"]["integrity"], agent)
        for level, counts in summary["primary_by_level"].items():
            rows = [r for r in self.stored if str(r["level"]) == level]
            self.assertEqual(counts, {c: sum(r["primary"] == c for r in rows) for c in taxonomy.CAUSES}, level)
        for agent, row in summary["episodes"].items():
            self.assertEqual(row["failed"], row["total"] - row["succeeded"], agent)

    def test_harm_steps_lie_inside_the_episode(self):
        for row in self.stored:
            for cause, step in row["harm_steps"].items():
                self.assertTrue(1 <= step <= row["steps"], (row["agent"], row["run_id"], cause, step))
            integrity = [s for c, s in row["harm_steps"].items() if taxonomy.CAUSES[c][0] == "integrity"]
            self.assertEqual(row["first_integrity_harm_step"], min(integrity, default=None), row["run_id"])
            if integrity:
                self.assertEqual(taxonomy.CAUSES[row["primary"]][0], "integrity", row["run_id"])
            if row["violations"]:
                self.assertTrue(integrity, f"{row['run_id']} has violations but no integrity cause")

    def test_fare_replay_agrees_with_the_trusted_flag_and_violation(self):
        validation = self.summary["validation"]
        self.assertEqual(validation["fare_hold_broken_matches"], validation["episodes_with_receipt"])
        self.assertEqual(validation["accepted_request_changed_matches"], validation["episodes_with_receipt"])

    def test_manual_checks_name_failed_episodes_and_cover_every_agent(self):
        notes = json.loads((OUT / "manual_checks.json").read_text())["checks"]
        self.assertGreaterEqual(len(notes), 15)
        self.assertEqual({n["agent"] for n in notes}, set(taxonomy.AGENTS))
        index = {(r["agent"], r["run_id"]): r for r in self.stored}
        for note in notes:
            self.assertIn((note["agent"], note["run_id"]), index)
            self.assertIn(note["manual_primary"], taxonomy.CAUSES)
        agree = sum(index[(n["agent"], n["run_id"])]["primary"] == n["manual_primary"] for n in notes)
        self.assertEqual(self.stored_summary["manual_checks"]["agree"], agree)


if __name__ == "__main__":
    unittest.main()
