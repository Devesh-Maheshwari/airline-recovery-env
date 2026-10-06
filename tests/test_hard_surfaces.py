"""Tier plumbing across the CLI, the evaluator, the external runner, Harbor and OpenEnv.

Easy defaults must produce exactly what they produced before tiers existed. Tests
that need a hard episode or the oracle skip until those parts are importable.
"""
import contextlib
import hashlib
import hmac
import importlib.util
import inspect
import io
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
from unittest import mock

from airline_recovery.cli import main as cli_main
from airline_recovery.live import evaluate, external, harbor
from airline_recovery.live.environment import LiveAirlineEnv
from airline_recovery.live.evaluate import (MUTATIONS, PUBLIC_OBSERVATION_FIELDS, _public_observation, aggregate,
                                            manifest_for, reset_options, run_evaluation, run_id_for)
from airline_recovery.live.harbor import instruction, verify_receipt
from airline_recovery.live.policies import load_policy
from airline_recovery.live.scenarios import CASES, case_for, task_manifest

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "airline_recovery" / "live"
OPENENV_INSTALLED = importlib.util.find_spec("openenv") is not None


def _hard_manifest_available():
    try:
        manifest_for("hard")
    except ValueError:
        return False
    return True


def _hard_episode_available():
    if not _hard_manifest_available() or "tier" not in inspect.signature(case_for).parameters:
        return False
    try:
        LiveAirlineEnv(max_steps=None)
    except ValueError:
        return False
    return True


def _oracle_available():
    try:
        load_policy("oracle")
    except (ValueError, ImportError, AttributeError):
        return False
    return (LIVE / "oracle.py").is_file()


HARD_MANIFEST = _hard_manifest_available()
HARD_EPISODE = _hard_episode_available()
ORACLE = _oracle_available()


def run_cli(*arguments):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        cli_main(list(arguments))
    return output.getvalue()


class TierHelperTests(unittest.TestCase):
    def test_easy_defaults_are_unchanged(self):
        self.assertEqual(reset_options("eval", 2), {"split": "eval", "index": 2})
        self.assertEqual(reset_options("eval", 2, "hard"), {"split": "eval", "index": 2, "tier": "hard"})
        self.assertEqual(run_id_for("train", 1, 7), "train:1:7")
        self.assertEqual(run_id_for("train", 1, 7, "hard"), "hard:train:1:7")
        self.assertEqual(manifest_for(), task_manifest())
        with self.assertRaises(ValueError):
            reset_options("train", 0, "medium")

    def test_manifest_without_tier_support_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "not available"):
            manifest_for("hard", source=lambda: {})

    def test_public_fields_and_mutations_cover_the_hard_tools(self):
        self.assertLessEqual({"tier", "level"}, PUBLIC_OBSERVATION_FIELDS)
        self.assertIn("void_booking", MUTATIONS)
        self.assertNotIn("provider_lookup", MUTATIONS)
        public = _public_observation({"episode_id": "e", "tier": "hard", "level": 2, "metadata": {"score": 1}})
        self.assertEqual(public, {"episode_id": "e", "tier": "hard", "level": 2})

    def test_aggregate_by_level(self):
        rows = [{"split": "train", "task_index": 0, "success": True, "terminated": True, "truncated": False, "level": 1},
                {"split": "train", "task_index": 1, "success": False, "terminated": True, "truncated": False, "level": 1},
                {"split": "train", "task_index": 2, "success": True, "terminated": True, "truncated": False, "level": 3},
                {"split": "eval", "task_index": 0, "success": True, "terminated": True, "truncated": False}]
        self.assertEqual(aggregate(rows)["by_level"],
                         {"1": {"episodes": 2, "successes": 1, "pass_rate": 0.5},
                          "3": {"episodes": 1, "successes": 1, "pass_rate": 1.0}})
        self.assertEqual(aggregate(rows[3:])["by_level"], {})


class TieredRecordingEnv:
    instances = []
    resets = []
    budget = 26

    def __init__(self, max_steps=48):
        self.closed = False
        self.constructed_with = max_steps
        self.max_steps = max_steps if max_steps is not None else self.budget
        self.instances.append(self)

    def task_manifest(self, tier="easy"):
        return {name: [{"id": f"{tier}-{name}", "index": 0}] for name in ("train", "eval", "test")}

    def tools(self):
        return [{"name": "finish", "parameters": {"type": "object"}}]

    def reset(self, *, seed, options):
        self.resets.append((dict(options), seed))
        observation = {"episode_id": "public", "step": 0, "result": None}
        if options.get("tier") == "hard":
            observation.update(tier="hard", level=2)
        return observation, {"action_budget": self.max_steps}

    def step(self, action):
        score = {"success": True, "reward": 0.8, "cost": 0.1, "details": {"requests": 3, "level": 2}}
        return {"step": 1, "result": {"ok": True}}, 0.8, True, False, {"score": score}

    def close(self):
        self.closed = True


class EvaluatorTierTests(unittest.TestCase):
    def setUp(self):
        TieredRecordingEnv.instances = []
        TieredRecordingEnv.resets = []
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_easy_run_is_unchanged(self):
        summary = run_evaluation(policy="nop", split="train", seeds=[5], output=self.directory.name,
                                 env_factory=TieredRecordingEnv, progress=False)
        self.assertEqual(TieredRecordingEnv.resets, [({"split": "train", "index": 0}, 5)])
        self.assertEqual(summary["max_steps"], 48)
        self.assertEqual(summary["tier"], "easy")
        self.assertEqual(summary["matched_episode_keys"], ["train:0:5"])
        self.assertEqual(summary["all"]["by_level"], {})
        self.assertTrue(all(env.constructed_with == 48 for env in TieredRecordingEnv.instances))
        record = json.loads(Path(self.directory.name, "episodes.jsonl").read_text())
        self.assertEqual((record["tier"], record["level"], record["task_id"]), ("easy", None, "easy-train"))

    def test_hard_run_prefixes_ids_and_takes_the_budget_from_the_case(self):
        summary = run_evaluation(policy="nop", split="all", seeds=[5], output=self.directory.name,
                                 env_factory=TieredRecordingEnv, progress=False, tier="hard")
        self.assertEqual([options for options, _ in TieredRecordingEnv.resets],
                         [{"split": name, "index": 0, "tier": "hard"} for name in ("train", "eval", "test")])
        self.assertTrue(all(env.constructed_with is None for env in TieredRecordingEnv.instances))
        self.assertTrue(all(env.closed for env in TieredRecordingEnv.instances))
        self.assertIsNone(summary["max_steps"])
        self.assertEqual(summary["tier"], "hard")
        self.assertEqual(summary["matched_episode_keys"], ["hard:train:0:5", "hard:eval:0:5", "hard:test:0:5"])
        self.assertEqual(summary["all"]["by_level"], {"2": {"episodes": 3, "successes": 3, "pass_rate": 1.0}})
        record = json.loads(Path(self.directory.name, "episodes.jsonl").read_text().splitlines()[0])
        self.assertEqual((record["tier"], record["level"], record["task_id"]), ("hard", 2, "hard-train"))
        provenance = json.loads(Path(self.directory.name, "provenance.json").read_text())
        self.assertEqual((provenance["tier"], provenance["max_steps"]), ("hard", None))

    def test_explicit_max_steps_still_wins_on_the_hard_tier(self):
        run_evaluation(policy="nop", split="train", seeds=[1], output=self.directory.name,
                       env_factory=TieredRecordingEnv, progress=False, tier="hard", max_steps=12)
        self.assertTrue(all(env.constructed_with == 12 for env in TieredRecordingEnv.instances))
        with self.assertRaises(ValueError):
            run_evaluation(policy="nop", split="train", seeds=[1], output=self.directory.name, overwrite=True,
                           env_factory=TieredRecordingEnv, progress=False, tier="medium")

    def test_cli_forwards_tier(self):
        with mock.patch("airline_recovery.live.evaluate.run_evaluation", return_value={"all": {"errors": 0}}) as runner, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(evaluate.main(["--output", self.directory.name, "--tier", "hard"]), 0)
            self.assertEqual(runner.call_args.kwargs["tier"], "hard")
            self.assertIsNone(runner.call_args.kwargs["max_steps"])
            self.assertEqual(evaluate.main(["--output", self.directory.name]), 0)
            self.assertEqual(runner.call_args.kwargs["tier"], "easy")


class InstructionAndReceiptTests(unittest.TestCase):
    def test_easy_instruction_is_unchanged(self):
        text = instruction()
        self.assertIn("Budget: 48 actions.", text)
        self.assertIn("verify two healthy probe windows before finish", text)
        self.assertEqual(text, instruction(tier="easy"))
        self.assertEqual(instruction("python control.py", "episode.json"),
                         instruction("python control.py", "episode.json", tier="easy", budget=48))

    def test_hard_instruction_states_the_budget_and_drops_the_hints(self):
        text = instruction(tier="hard", budget=26)
        self.assertIn("Budget: 26 actions.", text)
        self.assertIn("python /app/control.py", text)
        self.assertIn("signed receipt", text)
        for hint in ("Preserve accepted bookings", "verify two healthy probe windows", "reconcile accepted transactions",
                     "another incident can arrive", "Inspect logs/config/SQL"):
            self.assertNotIn(hint, text)
        with self.assertRaises(ValueError):
            instruction(tier="hard")
        with self.assertRaises(ValueError):
            instruction(tier="medium", budget=10)

    def signed(self, body, key=b"k"):
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return {"receipt": {"body": body, "signature": hmac.new(key, payload, hashlib.sha256).hexdigest()}}

    def test_verify_receipt_checks_schema_and_tier(self):
        score = {"reward": 0.5, "success": False}
        easy = {"schema_version": 3, "task": {"split": "train", "index": 1}, "score": score}
        hard = {"schema_version": 4, "tier": "hard", "task": {"split": "train", "index": 1}, "score": score}
        self.assertEqual(verify_receipt(self.signed(easy), b"k", "train", 1)[0], 0.5)
        self.assertEqual(verify_receipt(self.signed({**easy, "tier": "easy"}), b"k", "train", 1)[0], 0.5)
        self.assertEqual(verify_receipt(self.signed(hard), b"k", "train", 1, tier="hard")[2]["tier"], "hard")
        for artifact, tier in ((self.signed(hard), "easy"), (self.signed(easy), "hard"),
                               (self.signed({**hard, "schema_version": 3}), "hard"),
                               (self.signed({**easy, "schema_version": 4}), "easy"),
                               (self.signed({**hard, "tier": "easy"}), "hard")):
            with self.subTest(tier=tier, body=artifact["receipt"]["body"]), self.assertRaisesRegex(ValueError, "another task"):
                verify_receipt(artifact, b"k", "train", 1, tier=tier)
        with self.assertRaises(ValueError):
            verify_receipt(self.signed(hard), b"k", "train", 1, tier="medium")

    def test_generated_grader_checks_tier_like_verify_receipt(self):
        self.assertIn('spec.get("tier","easy")', harbor.GRADE)
        self.assertIn('body.get("tier","easy")!=tier', harbor.GRADE)
        self.assertIn('{"easy":3,"hard":4}[tier]', harbor.GRADE)


class HarborBuildTests(unittest.TestCase):
    def test_easy_build_layout_is_unchanged_apart_from_the_tier_field(self):
        with tempfile.TemporaryDirectory() as directory:
            result = harbor.build(directory, 19)
            self.assertEqual(result, {"tasks": len(CASES), "output": directory})
            root = Path(directory)
            self.assertEqual({p.name for p in root.iterdir()}, {"train", "eval", "test", "manifest.json"})
            task = root / "train" / "airline-recovery-train-000"
            self.assertEqual(json.loads((task / "tests" / "airline-recovery-train-000.spec.json").read_text()),
                             {"split": "train", "index": 0, "tier": "easy", "seed": 19})
            self.assertEqual((task / "instruction.md").read_text(), instruction())
            self.assertIn('difficulty = "medium"', (task / "task.toml").read_text())
            self.assertIn('difficulty = "hard"', (root / "test" / "airline-recovery-test-000" / "task.toml").read_text())
            self.assertEqual({p.name for p in (task / "solution").iterdir()}, {"solve.sh", "solve.py", "policies.py", "oracle.py"})
            self.assertIn("from policies import ReferencePolicy", (task / "solution" / "solve.py").read_text())
            world = task / "environment" / "world-airline-recovery-train-000" / "airline_recovery" / "live"
            for name in harbor.CORE:
                self.assertTrue((world / name).is_file(), name)
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertTrue(all(entry["tier"] == "easy" for entry in manifest["tasks"]))
            # The CLI's easy call is unchanged: build(output, seed).
            with mock.patch("airline_recovery.live.harbor.build", return_value={}) as build:
                run_cli("build-harbor", "--output", directory)
                build.assert_called_once_with(directory, None)
                run_cli("build-harbor", "--output", directory, "--tier", "hard")
                self.assertEqual(build.call_args.args, (directory, None, "hard"))

    @unittest.skipUnless(HARD_MANIFEST and ORACLE, "the hard tier generator or the oracle is not importable")
    def test_hard_build_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = json.loads(run_cli("build-harbor", "--output", directory, "--tier", "hard", "--seed", "3"))
            hard = manifest_for("hard")
            count = sum(len(items) for items in hard.values())
            self.assertEqual(result["tasks"], count)
            root = Path(directory)
            self.assertEqual({p.name for p in root.iterdir()}, {"hard", "manifest.json"})
            for split, items in hard.items():
                for item in items:
                    task = root / "hard" / split / item["id"]
                    self.assertTrue(item["id"].startswith(f"airline-recovery-hard-{split}-"), item["id"])
                    case = case_for(split, item["index"], tier="hard")
                    spec = json.loads((task / "tests" / f"{item['id']}.spec.json").read_text())
                    self.assertEqual(spec, {"split": split, "index": item["index"], "tier": "hard", "seed": 3})
                    self.assertEqual(spec, json.loads((task / "environment" / f"world-{item['id']}" / "task_spec.json").read_text()))
                    toml = (task / "task.toml").read_text()
                    self.assertIn(f'difficulty = "hard-{case.level}"', toml)
                    self.assertIn(f'name = "airline-recovery/{item["id"]}"', toml)
                    text = (task / "instruction.md").read_text()
                    self.assertEqual(text, instruction(tier="hard", budget=case.budget))
                    self.assertTrue((task / "solution" / "oracle.py").is_file())
                    self.assertIn("from oracle import OraclePolicy", (task / "solution" / "solve.py").read_text())
                    world = task / "environment" / f"world-{item['id']}" / "airline_recovery" / "live"
                    for name in harbor.CORE + harbor.HARD_CORE:
                        self.assertTrue((world / name).is_file(), name)
                    self.assertEqual({p.name for p in (task / "environment").iterdir()},
                                     {"Dockerfile", "docker-compose.yaml", "control.py", "episode.json", f"world-{item['id']}"})
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertEqual({entry["tier"] for entry in manifest["tasks"]}, {"hard"})
            # A recognised hard output can be regenerated, and "all" writes both tiers.
            both = json.loads(run_cli("build-harbor", "--output", directory, "--tier", "all"))
            self.assertEqual(both["tasks"], count + len(CASES))
            self.assertEqual({entry["tier"] for entry in json.loads((root / "manifest.json").read_text())["tasks"]},
                             {"easy", "hard"})


class HarborGeneratedFileTests(unittest.TestCase):
    """Run or inspect what build-harbor writes, not only the file names."""
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name).resolve()
        cls.tier = "all" if HARD_MANIFEST and ORACLE else "easy"
        harbor.build(cls.root, 5, cls.tier)
        cls.tasks = sorted(toml.parent for toml in cls.root.rglob("task.toml"))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_each_tier_solution_runs_on_its_own(self):
        # solve.sh runs `python /solution/solve.py`: only the solution folder and control.py are importable.
        stub = ("import sys\n"
                "def call(action=None):\n"
                "    print('CONTROL CALL', action, file=sys.stderr)\n"
                "    return {'terminated': True, 'truncated': False, 'reward': 1.0, 'observation': {}}\n")
        samples = [self.root / "train" / "airline-recovery-train-000"]
        if self.tier == "all":
            samples.append(self.root / "hard" / "train" / "airline-recovery-hard-train-000")
        for task in samples:
            with self.subTest(task=task.name), tempfile.TemporaryDirectory() as sandbox:
                for source in (task / "solution").iterdir():
                    Path(sandbox, source.name).write_bytes(source.read_bytes())
                Path(sandbox, "control.py").write_text(stub)
                env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
                result = subprocess.run([sys.executable, "-E", "-s", "solve.py"], cwd=sandbox, env=env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("CONTROL CALL None", result.stderr)
                self.assertEqual(result.stdout.strip(), "1.0")

    def test_no_build_context_file_is_shared_between_tasks_with_different_content(self):
        # BuildKit reuses a cached context file when the context folder name, relative path, size and
        # mtime match; archives give every file one mtime, so path and size alone must identify content.
        # The agent image copies only these files from environment/; compose reads its file directly.
        agent_image = {"Dockerfile", "control.py", "episode.json"}
        self.assertIn("COPY control.py episode.json /app/", (self.tasks[0] / "environment" / "Dockerfile").read_text())
        seen = {}
        for task in self.tasks:
            environment = task / "environment"
            worlds = [p for p in environment.iterdir() if p.is_dir()]
            self.assertEqual([p.name for p in worlds], [f"world-{task.name}"])
            built = [(task / "tests", p) for p in (task / "tests").rglob("*")]
            built += [(worlds[0], p) for p in worlds[0].rglob("*")]
            built += [(environment, environment / name) for name in agent_image]
            for context, file in built:
                if file.is_file():
                    key = (context.name, file.relative_to(context).as_posix(), file.stat().st_size)
                    digest = hashlib.sha256(file.read_bytes()).hexdigest()
                    self.assertEqual(seen.setdefault(key, digest), digest, f"{task.name}: {key}")
        compose = (self.tasks[0] / "environment" / "docker-compose.yaml").read_text()
        self.assertIn(f"context: ./world-{self.tasks[0].name}\n", compose)

    def test_manifest_paths_are_relative_to_the_output(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(len(manifest["tasks"]), len(self.tasks))
        for entry in manifest["tasks"]:
            self.assertFalse(Path(entry["path"]).is_absolute(), entry["path"])
            self.assertNotIn(str(self.root), entry["path"])
            self.assertTrue((self.root / entry["path"] / "task.toml").is_file(), entry["path"])


class CliTierTests(unittest.TestCase):
    def test_list_default_is_byte_identical_to_the_easy_manifest(self):
        self.assertEqual(run_cli("list"), json.dumps(task_manifest(), sort_keys=True, indent=2) + "\n")
        self.assertEqual(run_cli("list"), run_cli("list", "--tier", "easy"))

    @unittest.skipUnless(HARD_MANIFEST, "the hard tier generator is not importable")
    def test_list_hard_and_all(self):
        hard = json.loads(run_cli("list", "--tier", "hard"))
        self.assertEqual(set(hard), {"train", "eval", "test"})
        for split, items in hard.items():
            self.assertEqual([task["id"] for task in items],
                             [f"airline-recovery-hard-{split}-{task['index']:03d}" for task in items])
            for task in items:
                self.assertTrue({"fault", "faults", "solution", "seed", "budget"}.isdisjoint(task), task)
        self.assertEqual(json.loads(run_cli("list", "--tier", "all")), {"easy": task_manifest(), "hard": hard})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            run_cli("demo", "--tier", "hard", "--index", "99", "--delay", "0")
        self.assertEqual(error.exception.code, 2)

    def test_demo_default_does_not_send_a_tier(self):
        with mock.patch("airline_recovery.live.environment.LiveAirlineEnv") as factory:
            factory.return_value.__enter__.return_value.reset.side_effect = RuntimeError("stop here")
            with self.assertRaisesRegex(RuntimeError, "stop here"):
                run_cli("demo", "--delay", "0")
            factory.assert_called_once_with()
            self.assertEqual(factory.return_value.__enter__.return_value.reset.call_args.kwargs["options"],
                             {"split": "train", "index": 0})

    @unittest.skipUnless(HARD_EPISODE and ORACLE, "a hard episode or the oracle is not available")
    def test_demo_hard_runs_the_oracle(self):
        process_env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        result = subprocess.run([sys.executable, "-m", "airline_recovery", "demo", "--tier", "hard", "--delay", "0",
                                 "--json", "--seed", "1"], env=process_env, text=True, capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(events[0]["task"], "airline-recovery-hard-train-000")
        self.assertEqual((events[0]["tier"], events[0]["policy"]), ("hard", "oracle"))
        self.assertIsInstance(events[0]["level"], int)
        self.assertEqual(events[-1]["event"], "outcome")
        self.assertEqual(events[-1]["score"]["details"]["tier"], "hard")


@unittest.skipUnless(HARD_EPISODE, "a hard episode is not available in this build")
class BridgeTierTests(unittest.TestCase):
    def test_hard_sidecar_serves_a_hard_episode_with_the_case_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory, "task_spec.json")
            spec.write_text(json.dumps({"split": "train", "index": 0, "tier": "hard", "seed": 1}))
            key = Path(directory, "receipt.key")
            key.write_text("0" * 64)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            bridge = subprocess.Popen([sys.executable, "-m", "airline_recovery.live.bridge", "--spec", str(spec),
                                       "--key", str(key), "--host", "127.0.0.1", "--port", str(port)],
                                      cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 60
                while True:
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/observation", timeout=30) as response:
                            transition = json.load(response)
                        break
                    except OSError:
                        if time.monotonic() > deadline or bridge.poll() is not None:
                            raise
                        time.sleep(0.1)
            finally:
                bridge.terminate()
                bridge.wait(timeout=10)
            case = case_for("train", 0, tier="hard", seed=1)
            observation = transition["observation"]
            self.assertEqual(observation["tier"], "hard")
            self.assertEqual(observation["episode_contract"]["max_actions"], case.budget)
            self.assertEqual(transition["info"]["action_budget"], case.budget)


@unittest.skipUnless(HARD_EPISODE and ORACLE, "a hard episode or the oracle is not available")
class ExternalRunnerTierTests(unittest.TestCase):
    SCRIPTED = f'''import json, subprocess, sys
sys.path.insert(0, {str(ROOT)!r})
from airline_recovery.live.policies import load_policy
def call(action=None):
    args = [sys.executable, "control.py"] + (["action", json.dumps(action)] if action else [])
    return json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)
policy, transition = load_policy("oracle"), call()
while not (transition["terminated"] or transition["truncated"]):
    transition = call(policy(transition["observation"]))
print(json.dumps({{"num_turns": 1, "total_cost_usd": 0.0, "usage": {{"input_tokens": 1, "output_tokens": 1}}}}))
'''

    def test_hard_episode_is_graded_from_a_schema_4_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "agent.py")
            script.write_text(self.SCRIPTED)
            fake = {"default_model": "scripted", "command": lambda model, text, turns: [sys.executable, str(script)]}
            with mock.patch.dict(external.AGENTS, {"claude-code": fake}):
                summary = external.run("claude-code", None, "train", [1], Path(tmp, "out"), timeout=900,
                                       turns=10, index=0, keep=True, tier="hard")
            self.assertEqual(summary["tier"], "hard")
            self.assertEqual(summary["unfinished_episodes"], 0)
            record = json.loads(Path(tmp, "out", "episodes.jsonl").read_text())
            case = case_for("train", 0, tier="hard", seed=1)
            self.assertEqual(record["run_id"], "hard:train:0:1")
            self.assertEqual((record["tier"], record["level"], record["budget"]), ("hard", case.level, case.budget))
            self.assertTrue(record["finished"], record.get("grade_error"))
            self.assertEqual(record["score"]["details"]["tier"], "hard")
            episode = Path(tmp, "out", "episodes", "hard-train-000-seed1")
            self.assertEqual(json.loads((episode / "world" / "task_spec.json").read_text())["tier"], "hard")
            self.assertEqual((episode / "world" / "instruction.md").read_text(),
                             instruction("python control.py", "episode.json", tier="hard", budget=case.budget))
            artifact = json.loads((episode / "agent" / "episode.json").read_text())
            self.assertEqual((artifact["receipt"]["body"]["schema_version"], artifact["receipt"]["body"]["tier"]), (4, "hard"))
            provenance = json.loads(Path(tmp, "out", "provenance.json").read_text())
            self.assertEqual(provenance["tier"], "hard")
            self.assertIn(f"Budget: {case.budget} actions.", provenance["instruction"])


@unittest.skipUnless(OPENENV_INSTALLED, "Install airline-recovery-env[openenv] to test the native integration")
class OpenEnvTierTests(unittest.TestCase):
    def test_models_accept_the_tier_and_the_new_tools(self):
        from pydantic import ValidationError
        from typing import get_args
        from airline_recovery.openenv_adapter.models import AirlineAction, AirlineObservation, ResetOptions, ToolName

        self.assertEqual(ResetOptions().tier, "easy")
        self.assertEqual(ResetOptions().core_options(), {"split": "train", "index": 0})
        self.assertEqual(ResetOptions(tier="hard", split="eval", index=1).core_options(),
                         {"split": "eval", "index": 1, "tier": "hard"})
        with self.assertRaises(ValidationError):
            ResetOptions(tier="medium")
        for tool in ("provider_lookup", "void_booking"):
            self.assertEqual(AirlineAction(tool=tool, arguments={}).tool, tool)
        observation = AirlineObservation(episode_id="e", step=0, tier="hard", level=3)
        self.assertEqual((observation.tier, observation.level), ("hard", 3))
        self.assertIsNone(AirlineObservation(episode_id="e", step=0).tier)
        declared = (ROOT / "openenv.yaml").read_text().split("declared_tools: [")[1].split("]")[0]
        self.assertEqual([name.strip() for name in declared.split(",")], list(get_args(ToolName)))

    def test_environment_defaults_keep_the_easy_budget(self):
        from airline_recovery.openenv_adapter.environment import AirlineEnvironment
        environment = AirlineEnvironment()
        try:
            self.assertEqual(environment._core.max_steps, 48)
            self.assertEqual(environment.list_splits(), ["train", "eval", "test"])
        finally:
            environment.close()

    @unittest.skipUnless(HARD_EPISODE, "a hard episode is not available in this build")
    def test_environment_reset_with_tier_hard(self):
        from airline_recovery.openenv_adapter.environment import AirlineEnvironment
        case = case_for("train", 0, tier="hard", seed=1)
        environment = AirlineEnvironment()
        try:
            observation = environment.reset(seed=1, split="train", index=0, tier="hard")
            self.assertEqual((observation.tier, observation.level), ("hard", case.level))
            self.assertEqual(observation.episode_contract["max_actions"], case.budget)
            self.assertEqual(observation.metadata["action_budget"], case.budget)
            easy = environment.reset(seed=1, options={"split": "train", "index": 1})
            self.assertIsNone(easy.tier)
            self.assertEqual(easy.episode_contract["max_actions"], 48)
        finally:
            environment.close()


if __name__ == "__main__":
    unittest.main()
