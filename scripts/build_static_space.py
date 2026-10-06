"""Write a static Hugging Face Space that shows the replay viewer without a server.

    python scripts/build_replays.py
    python scripts/build_static_space.py OUTPUT_DIR

The folder holds index.html, data.json and a Space README. Static Spaces need no
paid hardware; the live playground needs the Docker Space (the root Dockerfile).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
# Load the page by path: the adapter package itself needs the optional openenv extra.
_spec = importlib.util.spec_from_file_location("replays", ROOT / "airline_recovery" / "openenv_adapter" / "replays.py")
_replays = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_replays)
DATA, PAGE = _replays.DATA, _replays.PAGE

REPO = "https://github.com/Devesh-Maheshwari/airline-recovery-env"
README = f"""---
title: Airline Recovery Env
emoji: 🛫
colorFrom: blue
colorTo: gray
sdk: static
pinned: false
license: mit
short_description: Replays of AI agents repairing a broken airline system
tags:
  - openenv
  - agent-environment
  - reinforcement-learning
  - benchmark
---

# Airline Recovery Env: agent replays

Step-by-step replays of every recorded hard-tier episode of
[Airline Recovery Env]({REPO}): an OpenEnv and Harbor environment where an AI
agent repairs a broken airline booking and payment system and is graded on what
actually happened to the customers.

This Space is a static viewer. To run the environment, play an incident yourself
or evaluate your own agent, follow the quick start in the
[GitHub repository]({REPO}#try-it-in-one-minute).
"""


def build(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    page = (PAGE.replace('fetch("/replays/data.json")', 'fetch("data.json")')
                .replace('<a class="cta" href="/web/">Play it yourself</a>',
                         f'<a class="cta" href="{REPO}#try-it-in-one-minute">Run it yourself</a>')
                .replace('Want to try it yourself? Open the <a href="/web/">playground</a>.', ""))
    assert 'href="/web/"' not in page and 'fetch("data.json")' in page
    (output / "index.html").write_text(page)
    shutil.copy(DATA, output / "data.json")
    (output / "README.md").write_text(README)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build(Path(sys.argv[1]))
    print(f"static Space written to {sys.argv[1]}")
