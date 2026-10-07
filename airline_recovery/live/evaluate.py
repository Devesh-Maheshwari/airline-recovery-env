"""Reproducible local agent evaluation over real HTTP service episodes."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time
from typing import Any, Callable

from .policies import load_policy

MUTATIONS = {"patch_config", "restart_service", "replay_events", "quarantine_event",
             "invalidate_cache", "reconcile_booking", "void_booking"}
SFT_FORMATS = ("json-actions", "trl-tools")
TIERS = ("easy", "hard")
EASY_MAX_STEPS = 48
PUBLIC_OBSERVATION_FIELDS = {"episode_id", "step", "alerts", "summary", "result", "available_tools", "mission",
                             "configuration_contracts", "episode_contract", "tier", "level"}


def check_tier(tier: str) -> str:
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    return tier


def reset_options(split: str, index: int, tier: str = "easy") -> dict[str, Any]:
    """Reset options for one task. The easy tier sends exactly what it sent before tiers existed."""
    options: dict[str, Any] = {"split": split, "index": index}
    if check_tier(tier) != "easy":
        options["tier"] = tier
    return options


def manifest_for(tier: str = "easy", source: Callable[..., Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Public task manifest of one tier; ``source`` defaults to the scenario catalogue."""
    if source is None:
        from .scenarios import task_manifest
        source = task_manifest
    if check_tier(tier) == "easy":
        return source()
    try:
        return source(tier=tier)
    except TypeError as exc:
        raise ValueError("the hard tier is not available in this build") from exc


def run_id_for(split: str, index: int, seed: int, tier: str = "easy") -> str:
    key = f"{split}:{index}:{seed}"
    return key if tier == "easy" else f"{tier}:{key}"


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> list[float] | None:
    """Two-sided Wilson 95% interval; empty samples have no interval."""
    if not trials:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def repeated_attempts(episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """pass@k and pass^k over instances attempted several times (same task and seed).

    Unbiased estimators per instance with n attempts and c successes, averaged over
    instances: pass@k = 1 - C(n-c, k) / C(n, k) (solved at least once in k tries) and
    pass^k = C(c, k) / C(n, k) (solved on all k tries).
    """
    from math import comb
    outcomes: dict[tuple, list[bool]] = {}
    for row in episodes:
        outcomes.setdefault((row["split"], row["task_index"], row["seed"]), []).append(bool(row["success"]))
    counts = {len(values) for values in outcomes.values()}
    if not outcomes or counts == {1}:
        return None
    if len(counts) != 1:
        raise ValueError("every instance needs the same number of attempts")
    n = counts.pop()
    def table(groups: list[list[bool]]) -> dict[str, dict[str, float]]:
        return {"pass@k": {str(k): statistics.mean(1 - comb(n - sum(g), k) / comb(n, k) for g in groups) for k in range(1, n + 1)},
                "pass^k": {str(k): statistics.mean(comb(sum(g), k) / comb(n, k) for g in groups) for k in range(1, n + 1)}}
    level_of = {(r["split"], r["task_index"], r["seed"]): r.get("level") for r in episodes}
    by_level = {}
    for level in sorted({lv for lv in level_of.values() if isinstance(lv, int)}):
        groups = [g for key, g in outcomes.items() if level_of[key] == level]
        by_level[str(level)] = {"instances": len(groups), **table(groups)}
    return {"instances": len(outcomes), "attempts_per_instance": n, **table(list(outcomes.values())), "by_level": by_level}


def aggregate(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    successes = sum(bool(row["success"]) for row in episodes)
    def mean(key: str) -> float | None:
        values = [row[key] for row in episodes if isinstance(row.get(key), (int, float))]
        return statistics.mean(values) if values else None
    # Seeds of one task are related samples, so also count tasks solved on every seed.
    tasks: dict[tuple[str, int], list[bool]] = {}
    levels: dict[int, list[bool]] = {}
    for row in episodes:
        tasks.setdefault((row["split"], row["task_index"]), []).append(bool(row["success"]))
        if isinstance(row.get("level"), int):
            levels.setdefault(row["level"], []).append(bool(row["success"]))
    return {
        # Hard episodes carry the generator level; easy episodes have none, so this stays empty.
        "by_level": {str(level): {"episodes": len(outcomes), "successes": sum(outcomes),
                                  "pass_rate": sum(outcomes) / len(outcomes)}
                     for level, outcomes in sorted(levels.items())},
        "episodes": len(episodes), "successes": successes,
        "pass_rate": successes / len(episodes) if episodes else None,
        "pass_rate_95pct_wilson": wilson_interval(successes, len(episodes)),
        "tasks": len(tasks), "tasks_solved_on_every_seed": sum(all(outcomes) for outcomes in tasks.values()),
        "integrity_violation_episodes": sum(row.get("integrity") is False for row in episodes),
        "excess_capture_cents": sum(row.get("excess_capture_cents") or 0 for row in episodes),
        "mean_incident_recovery": mean("incident_recovery"),
        "mean_reward": mean("reward"), "mean_steps": mean("steps"),
        "mean_tool_calls": mean("tool_calls"), "mean_cost": mean("cost"),
        "mean_requests": mean("requests"), "mean_wall_seconds": mean("wall_seconds"),
        "total_wall_seconds": sum(row.get("wall_seconds", 0) for row in episodes),
        "terminated": sum(bool(row["terminated"]) for row in episodes),
        "truncated": sum(bool(row["truncated"]) for row in episodes),
        "errors": sum(row.get("error") is not None for row in episodes),
    }


def _jsonl(handle: Any, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    handle.flush()


def _source_hashes() -> dict[str, str]:
    directory = Path(__file__).resolve().parent
    return {f"airline_recovery/live/{path.name}": hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.glob("*.py"))}


def run_evaluation(*, policy: str, split: str, seeds: list[int], output: str | Path,
                   max_steps: int | None = None, index: int | None = None,
                   env_factory: Callable[..., Any] | None = None,
                   overwrite: bool = False, progress: bool = True, tier: str = "easy") -> dict[str, Any]:
    """Run the exact same (split, index, seed) grid for each policy.

    Plugin exceptions are retained as failed episodes. Every environment is closed
    in a finally block. The only object supplied to the policy is a detached copy
    of the public observation, including public tool schemas on reset.
    ``max_steps=None`` means 48 on the easy tier and the case's own budget on the hard tier.
    """
    if split not in {"train", "eval", "test", "all"}:
        raise ValueError("split must be train, eval, test, or all")
    check_tier(tier)
    if not seeds or any(type(seed) is not int for seed in seeds) or len(seeds) != len(set(seeds)):
        raise ValueError("provide one or more unique integer seeds")
    if max_steps is None and tier == "easy":
        max_steps = EASY_MAX_STEPS
    if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
        raise ValueError("max_steps must be positive")
    if index is not None and (type(index) is not int or index < 0):
        raise ValueError("index must be non-negative")
    if env_factory is None:
        from .environment import LiveAirlineEnv
        env_factory = LiveAirlineEnv
    actor = load_policy(policy)
    directory = Path(output)
    paths = [directory / name for name in ("trajectories.jsonl", "episodes.jsonl", "summary.json", "provenance.json")]
    if not overwrite and any(path.exists() for path in paths):
        raise FileExistsError(f"results already exist in {directory}; use another --output or --overwrite")
    directory.mkdir(parents=True, exist_ok=True)
    source_hashes = _source_hashes()
    provenance = {"started_utc": datetime.now(timezone.utc).isoformat(),
                  "python": sys.version, "platform": platform.platform(),
                  "source_sha256": source_hashes, "policy": policy,
                  # The policy shares this interpreter with the grader; results are
                  # self-reported, not tamper-evident. Use OpenEnv or Harbor to isolate.
                  "trust_boundary": "in-process",
                  "split": split, "index": index, "seeds": seeds, "max_steps": max_steps, "tier": tier}
    policy_module = sys.modules.get(getattr(actor, "__module__", type(actor).__module__))
    policy_file = getattr(policy_module, "__file__", None)
    if policy_file and Path(policy_file).is_file():
        provenance["policy_source_sha256"] = hashlib.sha256(Path(policy_file).read_bytes()).hexdigest()
    discovery = env_factory(max_steps=max_steps)
    try:
        manifest = manifest_for(tier, discovery.task_manifest)
    finally:
        discovery.close()
    splits = ["train", "eval", "test"] if split == "all" else [split]
    grid: list[tuple[str, int, dict[str, Any], int]] = []
    for current_split in splits:
        tasks = manifest[current_split]
        if index is not None and index >= len(tasks):
            raise ValueError(f"index {index} outside {current_split} task range 0..{len(tasks) - 1}")
        for task_index, task in enumerate(tasks):
            if index is None or index == task_index:
                for seed in seeds:
                    grid.append((current_split, task_index, task, seed))
    paths[3].write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    records: list[dict[str, Any]] = []
    started = time.perf_counter()
    with paths[0].open("w", encoding="utf-8") as trajectories, paths[1].open("w", encoding="utf-8") as results:
        for split_name, task_index, task, seed in grid:
            key = run_id_for(split_name, task_index, seed, tier)
            metadata = {"run_id": key, "policy": policy, "split": split_name,
                        "task_index": task_index, "task_id": task.get("id", f"{split_name}/{task_index}"),
                        "seed": seed, "tier": tier, "level": None}
            episode_started = time.perf_counter()
            env = None
            steps = tool_calls = mutations = 0
            terminated = truncated = False
            accumulated_reward = 0.0
            score: dict[str, Any] = {}
            error = None
            try:
                reset_policy = getattr(actor, "reset", None)
                if callable(reset_policy):
                    reset_policy()
                env = env_factory(max_steps=max_steps)
                observation, info = env.reset(seed=seed, options=reset_options(split_name, task_index, tier))
                # The same discovery interface is available to user policies.
                if "available_tools" not in observation:
                    observation = {**observation, "available_tools": env.tools()}
                if isinstance(observation.get("level"), int):
                    metadata["level"] = observation["level"]
                # A hard episode's budget comes from its case; the runner's safety cap follows it.
                step_cap = max_steps if max_steps is not None else getattr(env, "max_steps", None) or 256
                _jsonl(trajectories, {**metadata, "event": "reset", "step": 0,
                                     "observation": observation, "info": info})
                while not (terminated or truncated) and steps < step_cap:
                    decision_started = time.perf_counter()
                    selected = actor(copy.deepcopy(observation))
                    decision_seconds = time.perf_counter() - decision_started
                    if not isinstance(selected, dict):
                        raise TypeError("policy must return a JSON action object")
                    json.dumps(selected, allow_nan=False)  # fail clearly for non-JSON plugins
                    observation, reward, terminated, truncated, info = env.step(selected)
                    steps += 1
                    tool = selected.get("tool")
                    tool_calls += tool != "finish"
                    mutations += isinstance(tool, str) and tool in MUTATIONS
                    accumulated_reward += float(reward)
                    score = info.get("score", score)
                    _jsonl(trajectories, {**metadata, "event": "step", "step": steps,
                        "action": selected, "observation": observation, "reward": reward,
                        "terminated": bool(terminated), "truncated": bool(truncated),
                        "decision_seconds": decision_seconds, "info": info})
                if not (terminated or truncated):
                    # A nonconforming environment reaching the runner's safety cap
                    # is incomplete; never invent terminal success or a grader score.
                    truncated = True
                    error = "runner_step_limit_without_environment_terminal"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _jsonl(trajectories, {**metadata, "event": "error", "step": steps, "error": error})
            finally:
                if env is not None:
                    try:
                        env.close()
                    except Exception as exc:
                        error = error or f"cleanup {type(exc).__name__}: {exc}"
            details = score.get("details", {})
            record = {**metadata, "success": bool(score.get("success", False)) and error is None,
                "reward": score.get("reward", accumulated_reward), "score": score,
                "steps": steps, "tool_calls": tool_calls, "mutations": mutations,
                "requests": details.get("requests") if isinstance(details, dict) else None,
                "integrity": score.get("integrity"), "incident_recovery": score.get("incident_recovery"),
                "excess_capture_cents": (details.get("business_impact") or {}).get("excess_capture_cents")
                                        if isinstance(details, dict) else None,
                "cost": score.get("cost"), "wall_seconds": time.perf_counter() - episode_started,
                "terminated": bool(terminated), "truncated": bool(truncated), "error": error}
            records.append(record)
            _jsonl(results, record)
            _jsonl(trajectories, {**record, "event": "episode_end"})
            if progress:
                outcome = "PASS" if record["success"] else "FAIL"
                print(f"[{len(records)}/{len(grid)}] {key} {outcome} steps={steps} "
                      f"reward={record['reward']:.3f} seconds={record['wall_seconds']:.2f}", flush=True)
    summary = {
        "schema_version": 2, "policy": policy, "seeds": seeds, "max_steps": max_steps, "tier": tier,
        "trust_boundary": "in-process",
        "all": aggregate(records),
        "by_split": {name: aggregate([row for row in records if row["split"] == name]) for name in splits},
        "by_task": {f"{name}:{i}": aggregate([row for row in records if row["split"] == name and row["task_index"] == i])
                    for name, i in sorted({(row["split"], row["task_index"]) for row in records})},
        "wall_seconds": time.perf_counter() - started,
        "interpretation": "Scripted policies are baselines, not training. Wilson intervals describe this run's "
                          "episode sample; repeated seeds and related tasks are not independent production incidents, "
                          "so read tasks_solved_on_every_seed alongside pass_rate. The policy ran in the grader's "
                          "process: these numbers are self-reported.",
        "matched_episode_keys": [row["run_id"] for row in records],
    }
    provenance["completed_utc"] = datetime.now(timezone.utc).isoformat()
    provenance["source_stable_through_completion"] = source_hashes == _source_hashes()
    summary["source_stable_through_completion"] = provenance["source_stable_through_completion"]
    paths[3].write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths[2].write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def _public_observation(observation: dict[str, Any], *, include_tools: bool = True) -> dict[str, Any]:
    """Export only the public observation contract, never OpenEnv metadata/score."""
    return {key: copy.deepcopy(value) for key, value in observation.items()
            if key in PUBLIC_OBSERVATION_FIELDS and (include_tools or key != "available_tools")}


def _successful_train_episode(rows: list[dict[str, Any]]) -> bool:
    final = rows[-1]
    score = final.get("score", {})
    return (
        final.get("event") == "episode_end"
        and final.get("success") is True
        and final.get("error") is None
        and (final.get("terminated") is True or final.get("truncated") is True)
        and all(row.get("split") == "train" and row.get("event") != "error" for row in rows)
        and isinstance(score, dict) and score.get("success", True) is True
    )


def _episode_messages(rows: list[dict[str, Any]], format: str) -> dict[str, Any]:
    """Validate chronology before creating either supported conversation format."""
    final, initial = rows[-1], rows[0]
    steps = rows[1:-1]
    if initial.get("event") != "reset" or initial.get("step") != 0 or not steps:
        raise ValueError(f"successful train episode {final['run_id']} lacks a reset or actions")
    for number, row in enumerate(steps, start=1):
        if row.get("event") != "step" or row.get("step") != number:
            raise ValueError(f"successful train episode {final['run_id']} has non-chronological steps")
        action = row.get("action")
        if (not isinstance(action, dict) or set(action) != {"tool", "arguments"}
                or not isinstance(action["tool"], str) or not isinstance(action["arguments"], dict)
                or not isinstance(row.get("observation"), dict)):
            raise ValueError(f"successful train episode {final['run_id']} has an invalid tool interaction")
        result = row["observation"].get("result")
        if isinstance(result, dict) and "tool" in result and result["tool"] != action["tool"]:
            raise ValueError(f"successful train episode {final['run_id']} has a mismatched tool response")
        if number < len(steps) and (row.get("terminated") or row.get("truncated")):
            raise ValueError(f"successful train episode {final['run_id']} continues after completion")
    if final.get("steps") != len(steps) or not isinstance(initial.get("observation"), dict):
        raise ValueError(f"successful train episode {final['run_id']} has inconsistent episode metadata")
    if any(steps[-1].get(flag, False) != final.get(flag, False) for flag in ("terminated", "truncated")):
        raise ValueError(f"successful train episode {final['run_id']} has inconsistent terminal flags")
    messages = [{"role": "system", "content": "Recover the airline services using the available tools. "
                 "Preserve booking, payment and check-in integrity. " +
                 ("Return one JSON tool action per turn." if format == "json-actions" else
                  "Call one tool at a time, inspect its response, and finish after verifying recovery.")}]
    record = {"messages": messages,
              "metadata": {"split": "train", "run_id": final["run_id"], "policy": final["policy"]}}
    previous_observation = _public_observation(initial["observation"])
    if format == "json-actions":
        for row in steps:
            messages.extend([
                {"role": "user", "content": json.dumps(previous_observation, sort_keys=True)},
                {"role": "assistant", "content": json.dumps(row["action"], sort_keys=True)},
            ])
            previous_observation = _public_observation(row["observation"])
        return record

    schemas = initial["observation"].get("available_tools")
    if not isinstance(schemas, list) or not schemas:
        raise ValueError(f"successful train episode {final['run_id']} has no tool schemas")
    tools, names = [], set()
    for schema in schemas:
        if (not isinstance(schema, dict) or not isinstance(schema.get("name"), str)
                or not isinstance(schema.get("parameters"), dict) or schema["name"] in names):
            raise ValueError(f"successful train episode {final['run_id']} has invalid tool schemas")
        names.add(schema["name"])
        function = {"name": schema["name"], "parameters": copy.deepcopy(schema["parameters"])}
        if isinstance(schema.get("description"), str):
            function["description"] = schema["description"]
        tools.append({"type": "function", "function": function})
    record["tools"] = tools
    messages.append({"role": "user", "content": json.dumps(
        _public_observation(initial["observation"], include_tools=False), sort_keys=True)})
    for number, row in enumerate(steps, start=1):
        name = row["action"]["tool"]
        if name not in names:
            raise ValueError(f"successful train episode {final['run_id']} calls an undeclared tool: {name}")
        call_id = f"call_{number:04d}"
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": copy.deepcopy(row["action"]["arguments"])},
            }]},
            {"role": "tool", "name": name, "tool_call_id": call_id,
             "content": json.dumps(_public_observation(row["observation"], include_tools=False), sort_keys=True)},
        ])
    return record


def export_sft(trajectory_path: str | Path, output_path: str | Path, *,
               format: str = "json-actions") -> dict[str, int]:
    """Export complete successful train episodes with public observations only.

    The backward-compatible default teaches JSON actions in assistant content.
    ``trl-tools`` uses HF tool schemas, assistant tool_calls and linked tool
    responses. No tokenizer, model, trainer or provider is loaded by this function.
    """
    if format not in SFT_FORMATS:
        raise ValueError(f"format must be one of {', '.join(SFT_FORMATS)}")
    source_path, target = Path(trajectory_path), Path(output_path)
    if source_path.resolve() == target.resolve() or (target.exists() and source_path.samefile(target)):
        raise ValueError("SFT output must not overwrite its source trajectories")
    episodes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    with source_path.open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            episodes.setdefault((row["policy"], row["run_id"]), []).append(row)
    # Validate all eligible episodes before touching an existing output artifact.
    records = [_episode_messages(rows, format) for rows in episodes.values() if _successful_train_episode(rows)]
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as destination:
        for record in records:
            _jsonl(destination, record)
    return {"episodes_read": len(episodes), "successful_train_episodes_exported": len(records)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="reference",
                        help="nop, blanket, blanket-hard, adopt-else-reconcile, wait-then-reconcile, source-aware, reference, reference-adopt, oracle, or module:function")
    parser.add_argument("--split", default="train", choices=("train", "eval", "test", "all"))
    parser.add_argument("--tier", default="easy", choices=TIERS, help="task tier (default: easy)")
    parser.add_argument("--seeds", default="1,2,3", help="comma-separated integers; matched across policies")
    parser.add_argument("--index", type=int, help="evaluate one public task index per selected split")
    parser.add_argument("--max-steps", type=int, default=None,
                        help="action budget per episode (default: 48 on the easy tier, the case's budget on the hard tier)")
    parser.add_argument("--output", default="runs/eval")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--export-sft", metavar="JSONL", help="export successful train demonstrations after this run")
    parser.add_argument("--sft-format", choices=SFT_FORMATS, default="json-actions",
                        help="SFT serialization: JSON-action chat (default) or native HF/TRL tool calling")
    args = parser.parse_args(argv)
    try:
        seeds = [int(value.strip()) for value in args.seeds.split(",")]
        summary = run_evaluation(policy=args.policy, split=args.split, seeds=seeds, output=args.output,
                                 index=args.index, max_steps=args.max_steps, overwrite=args.overwrite,
                                 progress=not args.quiet, tier=args.tier)
    except (ValueError, TypeError, FileExistsError, ImportError, AttributeError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.export_sft:
        print(json.dumps(export_sft(Path(args.output) / "trajectories.jsonl", args.export_sft, format=args.sft_format)))
    return 1 if summary["all"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
