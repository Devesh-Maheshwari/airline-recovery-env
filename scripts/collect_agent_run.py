"""Copy one external-agent run into the evidence tree.

    python scripts/collect_agent_run.py RUN_DIR DEST_DIR

Keeps what a reviewer needs and nothing secret: the run's episodes.jsonl,
summary.json and provenance.json; per episode, the world's trace (reset
observation plus every action and reply) gzipped under traces/, the agent's
action list under actions/, and the agent CLI's final message and usage under
final/. Sidecar keys and world folders are never copied.
"""
from __future__ import annotations

import gzip
import json
import shutil
import sys
from pathlib import Path


def _final_message(stdout: str) -> dict:
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if "result" in event:  # Claude Code's JSON summary
            return {key: event.get(key) for key in ("result", "num_turns", "stop_reason", "terminal_reason",
                                                     "total_cost_usd", "usage")}
        item = event.get("item") or {}
        if item.get("type") == "agent_message":  # Codex's last message
            return {"result": item.get("text")}
    return {}


def collect(run: Path, dest: Path) -> int:
    if (dest / "episodes.jsonl").exists():
        raise FileExistsError(f"{dest} already holds a run")
    for sub in ("traces", "actions", "final"):
        (dest / sub).mkdir(parents=True, exist_ok=True)
    for name in ("episodes.jsonl", "summary.json", "provenance.json"):
        shutil.copy(run / name, dest / name)
    count = 0
    for folder in sorted((run / "episodes").iterdir()):
        trace = folder / "trace.jsonl"
        if trace.exists():
            # A fixed header (no name, no time) keeps re-collected files byte-identical.
            with trace.open("rb") as source, (dest / "traces" / f"{folder.name}.jsonl.gz").open("wb") as raw, \
                    gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as target:
                shutil.copyfileobj(source, target)
        episode = folder / "agent" / "episode.json"
        if episode.exists():
            artifact = json.loads(episode.read_text())
            (dest / "actions" / f"{folder.name}.json").write_text(
                json.dumps({"actions": artifact.get("actions", [])}, indent=1) + "\n")
        stdout = folder / "agent" / "agent.stdout"
        if stdout.exists():
            (dest / "final" / f"{folder.name}.json").write_text(
                json.dumps(_final_message(stdout.read_text()), indent=1, sort_keys=True) + "\n")
        count += 1
    return count


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(f"{collect(Path(sys.argv[1]), Path(sys.argv[2]))} episodes -> {sys.argv[2]}")
