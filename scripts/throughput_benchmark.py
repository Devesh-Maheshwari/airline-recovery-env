"""Measure how fast the hard tier runs on one machine.

    python scripts/throughput_benchmark.py [--concurrency 1,2,4,8] [--seeds 1,2] [--output DIR]

Runs the bundled oracle on every hard task slot for the given seeds, with N
episodes in flight at once (one process each, each owning its own five worker
processes), and reports episodes per hour, environment steps per second, reset
and step latency, and peak resident memory per episode (the episode process plus its workers, sampled). The oracle's own thinking
time is negligible, so this measures the environment, not a model. Results go to
OUTPUT/throughput.json and a markdown table on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _tree_rss_mb() -> float:
    """Resident memory of this process plus its direct children (the five workers)."""
    me = os.getpid()
    rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True, check=True).stdout
    kib = sum(int(rss) for pid, ppid, rss in (line.split() for line in rows.splitlines())
              if int(pid) == me or int(ppid) == me)
    return round(kib / 1024, 1)


def _episode(job: tuple[str, int, int]) -> dict:
    from airline_recovery.live.environment import LiveAirlineEnv
    from airline_recovery.live.policies import load_policy
    split, index, seed = job
    policy = load_policy("oracle")
    with LiveAirlineEnv(max_steps=None) as env:
        started = time.perf_counter()
        observation, _ = env.reset(seed=seed, options={"split": split, "index": index, "tier": "hard"})
        reset_seconds = time.perf_counter() - started
        steps, done, success, rss = [], False, False, []
        while not done:
            tick = time.perf_counter()
            observation, _, terminated, truncated, info = env.step(policy(observation))
            steps.append(time.perf_counter() - tick)
            done = terminated or truncated
            success = bool((info.get("score") or {}).get("success")) if done else False
            if len(steps) % 8 == 1 or done:
                rss.append(_tree_rss_mb())
    return {"split": split, "index": index, "seed": seed, "success": success, "reset_seconds": reset_seconds,
            "step_seconds": steps, "wall_seconds": time.perf_counter() - started, "peak_rss_mb": max(rss)}


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def measure(concurrency: int, jobs: list[tuple[str, int, int]]) -> dict:
    started = time.perf_counter()
    # max_tasks_per_child=1 gives every episode a fresh process, so peak memory is per episode.
    with ProcessPoolExecutor(max_workers=concurrency, max_tasks_per_child=1) as pool:
        results = list(pool.map(_episode, jobs))
    wall = time.perf_counter() - started
    steps = [s for r in results for s in r["step_seconds"]]
    return {"concurrency": concurrency, "episodes": len(results), "solved": sum(r["success"] for r in results),
            "wall_seconds": round(wall, 1), "episodes_per_hour": round(len(results) * 3600 / wall),
            "env_steps_per_second": round(len(steps) / wall, 1),
            "reset_seconds_p50": round(statistics.median(r["reset_seconds"] for r in results), 3),
            "step_seconds_p50": round(statistics.median(steps), 3), "step_seconds_p95": round(_percentile(steps, 0.95), 3),
            "peak_rss_mb_per_episode_max": max(r["peak_rss_mb"] for r in results)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--concurrency", default="1,2,4,8")
    parser.add_argument("--seeds", default="1,2")
    parser.add_argument("--output", default=str(ROOT / "evidence" / "v0.5.0" / "throughput"))
    args = parser.parse_args()
    from airline_recovery.live.evaluate import manifest_for
    manifest = manifest_for("hard")
    jobs = [(split, task["index"], int(seed)) for split in ("train", "eval", "test") for task in manifest[split]
            for seed in args.seeds.split(",")]
    machine = {"platform": platform.platform(), "python": platform.python_version(), "cpu_count": os.cpu_count(),
               "processor": platform.processor() or platform.machine()}
    rows = [measure(int(c), jobs) for c in args.concurrency.split(",")]
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "throughput.json").write_text(json.dumps({"machine": machine, "jobs": len(jobs), "results": rows}, indent=2) + "\n")
    print(f"{machine['processor']}, {machine['cpu_count']} cores, Python {machine['python']}; {len(jobs)} oracle episodes per row\n")
    print("| In flight | Episodes/hour | Env steps/s | Reset p50 (s) | Step p50 / p95 (s) | Peak RSS per episode (MB) | Solved |")
    print("|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(f"| {r['concurrency']} | {r['episodes_per_hour']} | {r['env_steps_per_second']} | {r['reset_seconds_p50']} | "
              f"{r['step_seconds_p50']} / {r['step_seconds_p95']} | {r['peak_rss_mb_per_episode_max']} | {r['solved']}/{r['episodes']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
