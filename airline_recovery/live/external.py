r"""Evaluate coding-agent CLIs (Claude Code, Codex) against the live environment.

Each episode runs the world as the Harbor sidecar does (``bridge.py`` in its own
process with a private signing key) and gives the agent a scratch directory that
holds only ``control.py`` and ``episode.json``. The agent is graded solely from the
signed receipt, exactly like the Harbor verifier. The agent runs with whatever
account the CLI is logged into; nothing here reads or stores credentials.

    python -m airline_recovery.live.external --agent claude-code --model claude-haiku-4-5 \
        --split all --seeds 1 --output runs/claude-haiku

Isolation is weaker than Harbor's: the agent process runs on this machine. Claude
Code is restricted to the control script, Codex runs in its workspace sandbox
with network access, and neither is told where the environment's source lives.
Treat results as model baselines on the published tasks, not as adversarial
robustness evidence.

Each episode folder keeps ``trace.jsonl``: the world's reset observation and, for
every action the agent sent, the full reply (or the rejection), written by the
world process itself. ``--budget-scale`` and ``--explicit-instructions`` exist for
ablations (does a larger action budget, or the integrity rules stated in plain
words, change the outcome?); results run with them are not comparable to the
published baselines and are labelled as such in every record.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .evaluate import TIERS, _jsonl, _source_hashes, aggregate, check_tier, manifest_for, repeated_attempts, run_id_for, wilson_interval
from .harbor import CONTROL, case_for_tier, instruction, verify_receipt

AGENTS = {
    "claude-code": {
        "default_model": "claude-sonnet-5-5",
        "command": lambda model, task_text, turns: [
            "claude", "-p", task_text, "--model", model,
            "--tools", "Bash", "--allowedTools", "Bash(python control.py:*)", "Bash(python3 control.py:*)",
            "--permission-mode", "default", "--no-session-persistence", "--setting-sources", "",
            "--output-format", "json", "--max-turns", str(turns)],
    },
    "codex": {
        "default_model": "gpt-6-astra",
        "command": lambda model, task_text, turns: [
            "codex", "exec", "--model", model, "--sandbox", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true", "-c", 'approval_policy="never"',
            "--skip-git-repo-check", "--ephemeral", "--json", task_text],
    },
}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _usage(agent: str, stdout: str) -> dict:
    """Best-effort token/cost extraction from the CLI's machine-readable output."""
    try:
        if agent == "claude-code":
            summary = json.loads(stdout.strip().splitlines()[-1])
            usage = summary.get("usage", {})
            return {"cost_usd": summary.get("total_cost_usd"), "turns": summary.get("num_turns"),
                    "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                    "stop_reason": summary.get("stop_reason"), "terminal_reason": summary.get("terminal_reason")}
        if agent == "codex":
            for line in reversed(stdout.strip().splitlines()):
                event = json.loads(line)
                if event.get("type") in {"turn.completed", "usage"} or "usage" in event:
                    usage = event.get("usage", event)
                    return {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                            "cached_input_tokens": usage.get("cached_input_tokens")}
    except (ValueError, IndexError, AttributeError):
        pass
    return {}


def _sidecar_observation(url: str) -> dict:
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + "/observation", timeout=30) as response:
        return json.load(response)


def _scaled_budget(split: str, index: int, tier: str, scale: float) -> int | None:
    if scale == 1.0:
        return None
    if tier == "easy":
        raise ValueError("--budget-scale is for the hard tier")
    if not 1.0 < scale <= 4.0:
        raise ValueError("budget scale must be in (1, 4]")
    return min(256, math.ceil(case_for_tier(split, index, tier).budget * scale))


def run_episode(agent: str, model: str, split: str, index: int, seed: int, workdir: Path, *,
                timeout: float, turns: int, tier: str = "easy", budget_scale: float = 1.0,
                explicit: bool = False) -> dict:
    check_tier(tier)
    key = secrets.token_hex(32)
    port = _free_port()
    world = workdir / "world"
    agent_dir = workdir / "agent"
    world.mkdir(parents=True)
    agent_dir.mkdir()
    spec = {"split": split, "index": index, "seed": seed, "tier": tier}
    scaled = _scaled_budget(split, index, tier, budget_scale)
    if scaled is not None:
        spec["max_steps"] = scaled
    (world / "task_spec.json").write_text(json.dumps(spec))
    (world / "receipt.key").write_text(key)
    (agent_dir / "control.py").write_text(CONTROL)
    (agent_dir / "episode.json").write_text(json.dumps({"schema_version": 3, "actions": []}))
    package_root = str(Path(__file__).resolve().parents[2])
    bridge_log = (world / "bridge.log").open("wb")
    bridge = subprocess.Popen(
        [sys.executable, "-m", "airline_recovery.live.bridge", "--spec", str(world / "task_spec.json"),
         "--key", str(world / "receipt.key"), "--host", "127.0.0.1", "--port", str(port),
         "--trace", str(workdir / "trace.jsonl")],
        env={**os.environ, "PYTHONPATH": package_root}, stdout=subprocess.DEVNULL, stderr=bridge_log)
    url = f"http://127.0.0.1:{port}"
    record = {"agent": agent, "model": model, "tier": tier, "level": None, "split": split, "task_index": index,
              "seed": seed, "success": False, "reward": 0.0, "steps": None, "finished": False, "error": None,
              "budget_scale": budget_scale, "instruction_variant": "explicit" if explicit else "standard"}
    started = time.perf_counter()
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if bridge.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("world sidecar did not start; see world/bridge.log")
                time.sleep(0.1)
        budget = None
        if tier != "easy":
            # The hard budget is the case's own; the sidecar's reset observation is the authority.
            initial = _sidecar_observation(url)["observation"]
            budget = initial["episode_contract"]["max_actions"]
            record["level"] = initial.get("level")
            record["budget"] = budget
        task_text = instruction("python control.py", "episode.json", tier=tier, budget=budget, explicit=explicit)
        (world / "instruction.md").write_text(task_text)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "OPENAI_"))} | {
            "AIRLINE_RECOVERY_WORLD": url}
        command = AGENTS[agent]["command"](model, task_text, turns)
        # Own process group, so a timeout also stops the CLI's helper processes.
        process = subprocess.Popen(command, cwd=agent_dir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            (agent_dir / "agent.stdout").write_text(stdout)
            (agent_dir / "agent.stderr").write_text(stderr)
            record["agent_exit_code"] = process.returncode
            record["usage"] = _usage(agent, stdout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, _ = process.communicate()
            record["error"] = f"agent timed out after {timeout:.0f}s"
            (agent_dir / "agent.stdout").write_text(stdout or "")
        artifact = json.loads((agent_dir / "episode.json").read_text())
        record["agent_actions"] = len(artifact.get("actions", []))
        if "receipt" not in artifact:
            # Recover a receipt whose final reply was lost; the world is still up.
            current = _sidecar_observation(url)
            if "receipt" in current:
                artifact["receipt"] = current["receipt"]
        try:
            reward, success, body = verify_receipt(artifact, key.encode(), split, index, tier)
            record.update(reward=reward, success=success, finished=True, steps=body["steps"],
                          terminated=body["terminated"], truncated=body["truncated"], score=body["score"])
            # Top-level fields the shared aggregate reads.
            record.update(integrity=body["score"].get("integrity"),
                          incident_recovery=body["score"].get("incident_recovery"),
                          excess_capture_cents=((body["score"].get("details") or {}).get("business_impact") or {}).get("excess_capture_cents"))
        except ValueError as unverified:
            record["grade_error"] = str(unverified)
    except Exception as exc:  # the episode record must survive any failure
        record["error"] = record["error"] or f"{type(exc).__name__}: {exc}"
    finally:
        bridge.terminate()
        try:
            bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.kill()
        bridge_log.close()
    record["wall_seconds"] = time.perf_counter() - started
    return record


def run(agent: str, model: str | None, split: str, seeds: list[int], output: Path, *,
        timeout: float, turns: int, index: int | None = None, keep: bool = False, tier: str = "easy",
        budget_scale: float = 1.0, explicit: bool = False, trials: int = 1) -> dict:
    if agent not in AGENTS:
        raise ValueError(f"agent must be one of {', '.join(AGENTS)}")
    check_tier(tier)
    if tier == "easy" and (budget_scale != 1.0 or explicit):
        raise ValueError("--budget-scale and --explicit-instructions are for the hard tier")
    if budget_scale != 1.0 and not 1.0 < budget_scale <= 4.0:
        raise ValueError("budget scale must be in (1, 4]")
    if type(trials) is not int or not 1 <= trials <= 64:
        raise ValueError("trials must be an integer in 1..64")
    if not seeds or len(set(seeds)) != len(seeds):
        # As in evaluate.py: a repeated seed reuses an episode folder and would abort the run partway.
        raise ValueError("provide one or more unique integer seeds")
    if shutil.which(AGENTS[agent]["command"](model or "", "", 1)[0]) is None:
        raise RuntimeError(f"{agent} CLI is not installed or not on PATH")
    model = model or AGENTS[agent]["default_model"]
    output.mkdir(parents=True, exist_ok=True)
    if (output / "episodes.jsonl").exists():
        raise FileExistsError(f"results already exist in {output}")
    splits = ["train", "eval", "test"] if split == "all" else [split]
    manifest = manifest_for(tier)
    # Repeated attempts of one instance (same task and seed) measure pass@k and pass^k.
    grid = [(name, task["index"], seed, trial) for name in splits for task in manifest[name]
            if index is None or task["index"] == index for seed in seeds for trial in range(1, trials + 1)]
    provenance = {"started_utc": datetime.now(timezone.utc).isoformat(), "agent": agent, "model": model,
                  "command": AGENTS[agent]["command"](model, "<instruction>", turns), "seeds": seeds, "tier": tier,
                  "trust_boundary": "external-process", "source_sha256": _source_hashes(),
                  "budget_scale": budget_scale, "instruction_variant": "explicit" if explicit else "standard",
                  "trials": trials}
    if tier == "easy":
        provenance["instruction"] = instruction("python control.py", "episode.json")
    elif grid:
        # Hard budgets differ by level; the text of the first task stands in for the template.
        provenance["instruction"] = instruction("python control.py", "episode.json", tier=tier,
                                                budget=_scaled_budget(grid[0][0], grid[0][1], tier, budget_scale)
                                                or case_for_tier(grid[0][0], grid[0][1], tier).budget,
                                                explicit=explicit)
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    records = []
    with (output / "episodes.jsonl").open("w") as handle:
        for name, task_index, seed, trial in grid:
            suffix = f"-t{trial}" if trials > 1 else ""
            run_dir = output / "episodes" / f"{'' if tier == 'easy' else tier + '-'}{name}-{task_index:03d}-seed{seed}{suffix}"
            run_dir.mkdir(parents=True, exist_ok=True)
            record = run_episode(agent, model, name, task_index, seed, run_dir, timeout=timeout, turns=turns, tier=tier,
                                 budget_scale=budget_scale, explicit=explicit)
            record["run_id"] = run_id_for(name, task_index, seed, tier) + (f":t{trial}" if trials > 1 else "")
            record["trial"] = trial
            records.append(record)
            _jsonl(handle, record)
            if not keep:
                shutil.rmtree(run_dir / "world", ignore_errors=True)
            print(f"[{len(records)}/{len(grid)}] {record['run_id']} {'PASS' if record['success'] else 'FAIL'} "
                  f"reward={record['reward']:.3f} steps={record['steps']} finished={record['finished']} "
                  f"seconds={record['wall_seconds']:.0f} {record.get('error') or ''}", flush=True)
    for row in records:
        row.setdefault("terminated", False)
        row.setdefault("truncated", False)
    summary = {"schema_version": 1, "agent": agent, "model": model, "seeds": seeds, "tier": tier,
               "trust_boundary": "external-process", "budget_scale": budget_scale,
               "instruction_variant": "explicit" if explicit else "standard",
               "all": aggregate(records),
               "by_split": {name: aggregate([r for r in records if r["split"] == name]) for name in splits},
               "by_task": {f"{n}:{i}": aggregate([r for r in records if r["split"] == n and r["task_index"] == i])
                           for n, i in sorted({(r["split"], r["task_index"]) for r in records})},
               "unfinished_episodes": sum(not r["finished"] for r in records),
               "repeated_attempts": repeated_attempts(records),
               "total_cost_usd": sum((r.get("usage") or {}).get("cost_usd") or 0 for r in records) or None}
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", choices=sorted(AGENTS), required=True)
    parser.add_argument("--model", help="model name for the CLI (default depends on the agent)")
    parser.add_argument("--split", default="train", choices=("train", "eval", "test", "all"))
    parser.add_argument("--tier", default="easy", choices=TIERS, help="task tier (default: easy)")
    parser.add_argument("--index", type=int)
    parser.add_argument("--seeds", default="1")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per episode (default 1800)")
    parser.add_argument("--max-turns", type=int, default=150, help="agent turn cap where the CLI supports one")
    parser.add_argument("--keep-world", action="store_true", help="keep each episode's sidecar key, log and task text")
    parser.add_argument("--budget-scale", type=float, default=1.0,
                        help="ablation: multiply each hard case's action budget (1 < scale <= 4)")
    parser.add_argument("--explicit-instructions", action="store_true",
                        help="ablation: append the integrity rules in plain words to the hard task text")
    parser.add_argument("--trials", type=int, default=1,
                        help="attempts per task and seed, for pass@k and pass^k (default 1)")
    args = parser.parse_args(argv)
    seeds = [int(value) for value in args.seeds.split(",")]
    summary = run(args.agent, args.model, args.split, seeds, Path(args.output), timeout=args.timeout,
                  turns=args.max_turns, index=args.index, keep=args.keep_world, tier=args.tier,
                  budget_scale=args.budget_scale, explicit=args.explicit_instructions, trials=args.trials)
    print(json.dumps(summary["all"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
