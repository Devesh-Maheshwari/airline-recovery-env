"""Executed, reproducible comparison of transaction recovery strategies."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from airline_recovery import __version__
from .environment import LiveAirlineEnv
from .evaluate import _source_hashes
from .policies import load_policy


def _cohort_shape(rows: list[dict]) -> list[tuple]:
    """Identity-free description of a pending cohort; entity IDs are unique to each episode."""
    return sorted((row["amount_cents"], row["charge_id"] is not None, (row["idempotency_key"] or "").partition(":")[0])
                  for row in rows)


def _comparison_table(runs: list[dict]) -> str:
    """Aligned plain-text summary of the saved runs; amounts are synthetic cents shown as dollars."""
    rows = [("strategy", "outcome", "reward", "steps", "duplicate-charged bookings", "excess capture (synthetic $)")]
    for run in runs:
        score = run["score"]
        impact = score["details"]["business_impact"]
        rows.append((run["policy"], "SOLVED" if score["success"] else "NOT SOLVED", f"{score['reward']:.1f}",
                     str(score["details"]["steps"]), str(impact["duplicate_charge_bookings"]),
                     f"${impact['excess_capture_cents'] / 100:,.2f}"))
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(width) if column < 2 else cell.rjust(width)
                       for column, (cell, width) in enumerate(zip(row, widths))) for row in rows]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


def run_showcase(output: str | Path, *, seed: int = 42, delay: float = 0.15, overwrite: bool = False) -> dict:
    directory = Path(output)
    if directory.exists() and any(directory.iterdir()) and not overwrite:
        raise FileExistsError("Choose an empty output directory, or pass --overwrite to replace the previous results")
    directory.mkdir(parents=True, exist_ok=True)
    source = _source_hashes()
    report = {"version": __version__, "created_utc": datetime.now(timezone.utc).isoformat(),
              "seed": seed, "split": "train", "index": 4, "synthetic": True,
              "model_used": False, "training_performed": False,
              "source_sha256": source, "runs": [],
              "scope": "Scripted policies on one synthetic payment migration case. Amounts are synthetic cents; no production savings or model performance is measured."}
    print("Airline Recovery Env: recover interrupted bookings without charging customers twice", flush=True)
    print("Four scripted strategies. Same incident and seed. Five real HTTP workers per run.\n", flush=True)
    with (directory / "trajectories.jsonl").open("w") as trace:
        for name in ("blanket", "reference", "reference-adopt", "source-aware"):
            policy = load_policy(name)
            with LiveAirlineEnv() as env:
                observation, _ = env.reset(seed=seed, options={"split":"train", "index":4})
                initial = env.stack.query("SELECT b.booking_id,b.amount_cents,c.charge_id,c.idempotency_key "
                    "FROM bookings b LEFT JOIN charges c ON c.booking_id=b.booking_id "
                    "WHERE b.status='pending' ORDER BY b.booking_id")
                cohort = [row["booking_id"] for row in initial]
                print(f"{name}: {len(cohort)} interrupted bookings", flush=True)
                while not env.done:
                    before = copy.deepcopy(observation)
                    action = policy(copy.deepcopy(observation))
                    observation, reward, terminated, truncated, info = env.step(action)
                    trace.write(json.dumps({"policy":name, "observation":before, "action":action,
                        "next_observation":observation, "reward":reward, "terminated":terminated,
                        "truncated":truncated}) + "\n")
                    trace.flush()
                    if action["tool"] == "reconcile_booking":
                        args = action["arguments"]
                        method = ("try inferred charge ID" if name == "source-aware" else "adopt selected charge") if "existing_charge_id" in args else "retry observed key" if "idempotency_key" in args else "retry deployed key"
                        print(f"  step {observation['step']:02d}: {method} for {args['booking_id']}", flush=True)
                    if delay:
                        time.sleep(delay)
                score = info["score"]
                final = [row for row in env.stack.query("SELECT b.booking_id,b.status,b.amount_cents,"
                    "COUNT(c.charge_id) AS charge_count,COALESCE(SUM(c.amount_cents),0) AS captured_cents "
                    "FROM bookings b LEFT JOIN charges c ON c.booking_id=b.booking_id GROUP BY b.booking_id ORDER BY b.booking_id")
                    if row["booking_id"] in cohort]
                run = {"policy":name,"initial_pending":initial,"final_cohort":final,"score":score}
                report["runs"].append(run)
                impact = score["details"]["business_impact"]
                print(f"  success={score['success']} | duplicate-charged bookings={impact['duplicate_charge_bookings']} | "
                      f"excess captured={impact['excess_capture_cents']} synthetic cents | reward={reward}\n", flush=True)
    report["matched_initial_cohorts"] = all(_cohort_shape(run["initial_pending"]) == _cohort_shape(report["runs"][0]["initial_pending"]) for run in report["runs"])
    report["source_stable"] = source == _source_hashes()
    report["expected_contrast_observed"] = (
        report["matched_initial_cohorts"] and report["source_stable"]
        and report["runs"][0]["score"]["details"]["business_impact"]["duplicate_charge_bookings"] > 0
        and not report["runs"][0]["score"]["success"]
        and all(run["score"]["success"] for run in report["runs"][1:]))
    (directory / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Results saved to {directory / 'summary.json'}", flush=True)
    print(report["scope"], flush=True)
    print("The source-aware control reconstructs charge IDs without reading the ledger. Its success limits claims about reasoning difficulty.", flush=True)
    print("\nComparison (same incident, same seed)", flush=True)
    print(_comparison_table(report["runs"]), flush=True)
    if not report["expected_contrast_observed"]:
        raise RuntimeError("Demonstration did not reproduce its expected contrast; inspect saved evidence")
    return report
