# Hard-tier throughput on one machine

`python scripts/throughput_benchmark.py --concurrency 1,2,4,8 --seeds 1,2`, run
on 2026-10-07 on an 18-core Apple Silicon laptop (arm64, Python 3.12.12) with
nothing else heavy running. Each row runs the bundled oracle on all 12 hard task
slots × seeds 1 and 2 (24 episodes), with N episodes in flight at once. Every
episode owns its five worker processes and SQLite database. The oracle decides
instantly, so these numbers measure the environment, not a model.
`throughput.json` holds the raw values.

| Episodes in flight | Episodes per hour | Environment steps per second | Reset, median (s) | Step, median / 95th percentile (s) | Peak memory per episode (MB) | Solved |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 971 | 5.4 | 0.78 | 0.12 / 0.25 | 223 | 24/24 |
| 2 | 1,766 | 9.8 | 0.83 | 0.14 / 0.28 | 223 | 24/24 |
| 4 | 3,062 | 16.9 | 0.95 | 0.16 / 0.32 | 224 | 24/24 |
| 8 | 4,105 | 22.7 | 1.28 | 0.21 / 0.44 | 210 | 24/24 |

Peak memory is the resident memory of the episode process plus its five workers,
sampled every eight steps.

What this means for training and evaluation:

- An environment step costs about 0.1–0.2 s. For a model-driven episode the model
  dominates: the recorded coding-agent episodes took 174 s on average, of which
  the environment's share for about 30 steps is roughly 5 s.
- One machine of this size sustains at least eight concurrent episodes at about
  1.6 GB of memory in total; scaling beyond that was not measured here. The
  OpenEnv server's default is four concurrent sessions (`--max-sessions`).
- Oracle episodes are short (about 20 steps). Longer agent episodes scale step
  time linearly; per-step cost does not grow with episode length in these runs.
