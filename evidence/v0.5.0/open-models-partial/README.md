# Partial open-model runs (Together AI)

These runs used `examples/openai_compatible_agent.py` against Together AI's
OpenAI-compatible endpoint, through `python -m airline_recovery.live.evaluate`
(easy tier: all splits, seeds 1-3; hard tier: all splits, seeds 1-2). The
Together account reached its credit limit partway through, so most episodes
ended with HTTP 402 before the model acted. Only episodes that ran to the end
without a provider or network error are kept here; the runs reached mostly the
first train tasks, so they are not a balanced sample and are not comparable to
the full grids in the README.

They ran in the grader's process (`trust_boundary: in-process`) on the 0.5.0
code before the final review fixes: their hard-tier `reward` field omits the
0.10 success term. `summary.json` counts, per model and tier, the episodes
attempted, finished cleanly, solved, and those with an integrity violation.
