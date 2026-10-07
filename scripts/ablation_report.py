"""Compare the hard-tier ablation conditions on the same instances.

    python scripts/ablation_report.py [ABLATION_DIR]

ABLATION_DIR (default evidence/v0.5.0/hard/ablations) holds <model>/<condition>/
episodes.jsonl for the conditions standard, explicit (integrity rules stated in
plain words) and budget2 (twice the action budget). Writes report.json there and
prints a markdown table. Paired counts compare each condition with standard on
identical (task, seed) instances.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = ("standard", "explicit", "budget2")


def _load(path: Path) -> dict[tuple, dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {(r["split"], r["task_index"], r["seed"]): r for r in rows}


def _outcome(row: dict) -> str:
    if row.get("success"):
        return "solved"
    if row.get("integrity") is False:
        return "harmed"
    if not row.get("finished"):
        return "no_receipt"
    if row.get("truncated"):
        return "out_of_budget"
    return "other"


def summarize(rows: dict[tuple, dict]) -> dict:
    outcomes = [_outcome(r) for r in rows.values()]
    used = [r["steps"] / r["budget"] for r in rows.values() if r.get("steps") and r.get("budget")]
    by_level: dict[str, list[bool]] = {}
    for r in rows.values():
        by_level.setdefault(str(r.get("level")), []).append(bool(r.get("success")))
    return {"episodes": len(rows), **{k: outcomes.count(k) for k in ("solved", "harmed", "no_receipt", "out_of_budget", "other")},
            "by_level": {lv: f"{sum(v)}/{len(v)}" for lv, v in sorted(by_level.items())},
            "median_budget_used": round(statistics.median(used), 2) if used else None}


def paired(base: dict[tuple, dict], other: dict[tuple, dict]) -> dict:
    shared = sorted(set(base) & set(other))
    b = [bool(base[k].get("success")) for k in shared]
    o = [bool(other[k].get("success")) for k in shared]
    return {"instances": len(shared), "both": sum(x and y for x, y in zip(b, o)),
            "only_condition": sum(y and not x for x, y in zip(b, o)),
            "only_standard": sum(x and not y for x, y in zip(b, o)), "neither": sum(not x and not y for x, y in zip(b, o))}


def report(folder: Path) -> dict:
    result = {}
    for model in sorted(p for p in folder.iterdir() if p.is_dir()):
        runs = {c: _load(model / c / "episodes.jsonl") for c in CONDITIONS if (model / c / "episodes.jsonl").exists()}
        result[model.name] = {c: summarize(rows) for c, rows in runs.items()}
        if "standard" in runs:
            for c in CONDITIONS[1:]:
                if c in runs:
                    result[model.name][c]["paired_vs_standard"] = paired(runs["standard"], runs[c])
    return result


def markdown(result: dict) -> str:
    lines = ["| Model | Condition | Solved | Harmed a customer | No receipt | Out of budget | Other | Level 1 / 2 / 3 | Median budget used | Paired vs standard (only here / only standard) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for model, conditions in result.items():
        for c, s in conditions.items():
            p = s.get("paired_vs_standard")
            pair = f"{p['only_condition']} / {p['only_standard']}" if p else "—"
            levels = " · ".join(s["by_level"].values())
            lines.append(f"| {model} | {c} | {s['solved']}/{s['episodes']} | {s['harmed']} | {s['no_receipt']} | {s['out_of_budget']} | {s['other']} | "
                         f"{levels} | {s['median_budget_used']} | {pair} |")
    return "\n".join(lines)


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "evidence" / "v0.5.0" / "hard" / "ablations"
    data = report(folder)
    (folder / "report.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(markdown(data))
