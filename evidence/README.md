# Recorded baseline runs

`v0.4.0/<policy>/` holds the output of

```bash
python -m airline_recovery.live.evaluate --policy <policy> --split all --seeds 1,2,3,4,5 --output <dir>
```

for each bundled scripted policy on the 0.4.0 source: `summary.json` (aggregates
by split and task), `episodes.jsonl` (one scored record per episode) and
`provenance.json` (seeds, platform, and SHA-256 of every file in `airline_recovery/live/`).
Full trajectories are omitted for size; rerun the command to regenerate them.

These are scripted baselines run in the grader's process
(`trust_boundary: in-process`), not model results. See
[docs/LIMITATIONS.md](../docs/LIMITATIONS.md).

## v0.5.0 (hard tier)

`v0.5.0/hard/<policy>/`: `python -m airline_recovery.live.evaluate --tier hard --policy <policy> --split all`
with seeds 1–6 for `oracle` and 1–3 for the controls.

`v0.4.0/agents/<agent>/` and `v0.5.0/hard/agents/<agent>/`: coding-agent CLIs run
through `python -m airline_recovery.live.external` (easy tier seeds 1–3, hard tier
seeds 1–5). Each `episodes.jsonl` record carries the signed-receipt score, the
agent's step count, exit code and reported token usage; `provenance.json` records
the exact CLI command and source hashes (hard-tier agents: `provenance-seeds1-2.json` and `provenance-seeds3-5.json`, one per run). The agents ran under the accounts the CLIs
were logged into on one machine; costs are the CLIs' own estimates.

Notes on these records:

- The hard-tier agent runs were recorded before the hard reward gained its 0.10
  success term. Their `reward` field omits it; adding 0.10 to each successful
  episode gives the current formula. Success, integrity, steps and costs are
  unaffected. The `oracle` and control runs were re-recorded on the final code.
- `actions/` under each hard-tier agent holds every episode's submitted action
  list and the sidecar's signed receipt (the signing keys were discarded).
- The 0.4.0 `reference`, `reference-adopt`, `blanket` and `source-aware` runs
  record `source_stable_through_completion: false`; their hashes are those at
  completion. Later changes touched only the evaluation harness, not the world
  or the grader.
