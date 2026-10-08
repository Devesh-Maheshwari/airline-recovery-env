"""Compile the recorded hard-tier episodes into the replay viewer's data file.

    python scripts/build_replays.py

Reads the oracle's step-by-step replays from evidence/v0.5.0/hard/oracle/ and
each coding agent's 0.5.1 run from evidence/v0.5.1/hard/agents/ (episode records
and the world's per-step traces), and writes
airline_recovery/openenv_adapter/replays.json, which the server ships and the
/replays page reads.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARD = ROOT / "evidence" / "v0.5.0" / "hard"
AGENT_RUNS = ROOT / "evidence" / "v0.5.1" / "hard" / "agents"
OUT = ROOT / "airline_recovery" / "openenv_adapter" / "replays.json"
AGENTS = {"oracle": "Reference oracle", "codex-gpt-6-astra": "Codex (gpt-6-astra)",
          "claude-sonnet-5-5": "Claude Code (Sonnet 5.5)", "claude-haiku-4-5": "Claude Code (Haiku 4.5)"}
SHORT = {"oracle": "Oracle", "codex-gpt-6-astra": "Codex", "claude-sonnet-5-5": "Sonnet 5.5", "claude-haiku-4-5": "Haiku 4.5"}
LONG = 400
REPLY = 240


def _short(arguments: dict) -> dict:
    return {k: (v[:LONG] + "…" if isinstance(v, str) and len(v) > LONG else v) for k, v in arguments.items()}


def _trace_steps(path: Path) -> list[dict]:
    """One replay step per action from the world's trace: what was sent and what came back."""
    steps = []
    with gzip.open(path, "rt") as handle:
        for line in handle:
            entry = json.loads(line)
            if entry["kind"] == "rejected":
                steps.append({"tool": "(rejected request)", "arguments": {}, "ok": False, "error": entry.get("error", "")[:LONG]})
                continue
            if entry["kind"] != "action":
                continue
            observation, action = entry["observation"], entry["action"]
            result, summary = observation.get("result") or {}, observation.get("summary") or {}
            reply = json.dumps(result.get("data"), separators=(",", ":")) if "data" in result else ""
            steps.append({"tool": action.get("tool"), "arguments": _short(action.get("arguments") or {}),
                          "ok": bool(result.get("ok", True)), "error": str(result.get("error") or "")[:LONG],
                          "reply": reply[:REPLY] + ("…" if len(reply) > REPLY else ""),
                          "pending": summary.get("pending_bookings"), "outbox": summary.get("outbox_pending"),
                          "verified": summary.get("verification_windows"), "cancelled": summary.get("cancelled_bookings"),
                          "alerts": [a.get("message") for a in observation.get("alerts") or []]})
    return steps


def _episode(agent: str, record: dict, steps: list[dict]) -> dict:
    score = record.get("score") or {}
    details = score.get("details") or {}
    return {
        "agent": agent, "task": f"{record['split']}-{record['task_index']:03d}", "split": record["split"],
        "index": record["task_index"], "seed": record["seed"], "level": record.get("level"),
        "success": bool(record.get("success")), "finished": record.get("finished", True),
        "truncated": bool(record.get("truncated")), "steps_taken": record.get("steps"),
        "budget": details.get("budget"),
        "violations": sorted({v.split(":")[0] for v in details.get("violations", [])}),
        "excess_capture_cents": (details.get("business_impact") or {}).get("excess_capture_cents", 0),
        "verified": score.get("verified"), "incident_recovery": score.get("incident_recovery"),
        "steps": steps,
    }


def build() -> dict:
    episodes = []
    oracle_steps = {}
    for line in (HARD / "oracle" / "replays.jsonl").read_text().splitlines():
        row = json.loads(line)
        oracle_steps[row["run_id"]] = row["steps"]
    for line in (HARD / "oracle" / "episodes.jsonl").read_text().splitlines():
        record = json.loads(line)
        steps = [{**step, "arguments": _short(step["arguments"])} for step in oracle_steps.get(record["run_id"], [])]
        episodes.append(_episode("oracle", record, steps))
    for name in ("codex-gpt-6-astra", "claude-sonnet-5-5", "claude-haiku-4-5"):
        for folder in sorted((AGENT_RUNS / name).iterdir()):
            for line in (folder / "episodes.jsonl").read_text().splitlines():
                record = json.loads(line)
                episode = f"hard-{record['split']}-{record['task_index']:03d}-seed{record['seed']}"
                trace, log = folder / "traces" / f"{episode}.jsonl.gz", folder / "actions" / f"{episode}.json"
                if trace.exists():
                    steps = _trace_steps(trace)
                else:
                    actions = json.loads(log.read_text())["actions"] if log.exists() else []
                    steps = [{"tool": a.get("tool"), "arguments": _short(a.get("arguments") or {})} for a in actions]
                episodes.append(_episode(name, record, steps))
    episodes.sort(key=lambda e: (list(AGENTS).index(e["agent"]), e["split"], e["index"], e["seed"]))
    return {"version": 2, "agents": AGENTS, "agent_short": SHORT, "agent_order": list(AGENTS), "episodes": episodes}


if __name__ == "__main__":
    data = build()
    OUT.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))
    print(f"{len(data['episodes'])} episodes -> {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KiB)")
