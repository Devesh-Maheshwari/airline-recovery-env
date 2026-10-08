# Changelog

All notable changes to Airline Recovery Env are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

Versions 0.2.0 to 0.3.0 were internal builds and were never published. Their
dates are build dates. 0.4.0 was the last internal build; 0.5.0 is the first version intended for publication.
The reasons behind the 0.2.1, 0.3.0, 0.4.0 and 0.5.0 changes are summarised in
[docs/LIMITATIONS.md](docs/LIMITATIONS.md#review-history).

## [0.5.1] - 2026-10-08

### Changed

- The hard-tier `patch_config` and `invalidate_cache` descriptions state that a
  promised fare is served only from its cached quote, so evicting that row or
  turning the pricing cache off breaks the hold. Agent-facing text changed, so
  hard-tier results from 0.5.0 are not comparable.

### Added

- Full traces for coding-agent runs: the world process writes `trace.jsonl` per
  episode (the reset observation, then every action with the reply the agent
  saw, or the rejection). `scripts/collect_agent_run.py` copies a run into the
  evidence layout with gzipped traces.
- `--trials N` on the external runner: repeated attempts per task and seed, with
  pass@k and pass^k (overall and by level) in `summary.json`.
- Ablation options on the external runner: `--budget-scale` (multiply the hard
  case's action budget) and `--explicit-instructions` (append the integrity
  rules in plain words). Records carry `budget_scale` and `instruction_variant`.
- `docs/SCENARIOS.md`: every fault mechanism and trap with the real-world failure
  it stands for, what a correct recovery does, which invariants catch a wrong
  one, and what is simplified; per-slot composition; counts of distinct case
  structures; open validation items.
- `scripts/throughput_benchmark.py`: oracle episodes per hour, step latency and
  memory at several concurrency levels.

### Measured

- Coding agents on the hard tier, seeds 1–5, with full traces: Codex gpt-6-astra
  59/60 with no integrity violation (0.5.0: 46/60, all 13 integrity failures from
  the unstated fare-hold rule); Sonnet 5.5 14/60; Haiku 4.5 9/60
  ([evidence](evidence/v0.5.1/hard/agents/)).
- Ablations (Sonnet 5.5 and Haiku 4.5, 24 instances each): neither clearer
  instructions nor twice the budget changes the outcome by more than two
  episodes; level 2 stays unsolved
  ([evidence](evidence/v0.5.0/hard/ablations/README.md)).
- Failure analysis of the 180 published agent episodes: 96 of 110 failures are
  integrity harm caused by the agent's own action, first harm at a median of 36%
  of the budget ([evidence](evidence/v0.5.0/hard/failure-analysis/README.md)).

### Fixed

- `trap_outcomes.fare_hold_broken` now also records a live fare hold hidden by
  turning the pricing cache off, not only one evicted by `invalidate_cache`.
  Diagnostic only: rewards and success are unchanged.
- The agent-facing `refund_unwarranted` description now matches the grader:
  a refund on a confirmed booking, or on no booking.
- Documentation that disagreed with the code: the pricing quote never reads
  `fare_holds` (a promised fare lives only in its cache row); level 1 can draw a
  process fault and often has no trap; distinct case structures are 17, 600 and
  about 6,000 at levels 1–3, not "hundreds per level"; `refund_unwarranted`
  flags refunds on confirmed or missing bookings only.

## [0.5.0] - 2026-10-06

### Measured

- Oracle on the hard tier: 72/72 episodes (12 slots × seeds 1–6), no integrity
  violation, budgets calibrated at 32/36/34 actions for levels 1–3.
- Scripted controls on the hard tier (seeds 1–3): every one of `reference`,
  `source-aware`, `blanket`, `blanket-hard`, `adopt-else-reconcile`,
  `wait-then-reconcile` solves at most 6 of 36 episodes, each with 30
  integrity-violation episodes; `nop` 3/36 (the alert-only instances).
- Coding agents on the hard tier (seeds 1–5, 60 episodes each): Codex
  gpt-6-astra 46/60, Claude Code Sonnet 5.5 14/60, Claude Code Haiku 4.5 10/60;
  the same agents scored 33/33, 32/33 and 30/33 on the easy tier.
- The hard reward gains a 0.10 success term (weights now sum to 1.0), so a
  verified, finished recovery always outscores the same work left unfinished.
  The coding-agent episodes were recorded before this change; their `reward`
  field omits the term, which adds exactly 0.10 to each successful episode.
  Success, integrity and step counts are unaffected.

Frontier coding agents solved the 0.4.0 suite because the tool descriptions
explained each fault's fix, every fix was safe to apply everywhere, the ledger
was readable in full, and nothing degraded while they waited. This release adds
a generated **hard tier** that removes those four properties. The easy tier,
its eleven cases, its reset observation and its reward are unchanged.

### Added

- **Hard tier** (`reset(options={"tier": "hard"})`, `--tier hard` on `list`,
  `demo`, `build-harbor`, the evaluator and the external runner). Twelve task
  slots at three levels (`train` 0–5, `eval` 0–2, `test` 0–2), IDs
  `airline-recovery-hard-<split>-<index>`, budgets of 32, 36 and 34 actions
  taken from the case (`LiveAirlineEnv(max_steps=None)`).
- **Procedural generation.** `generate_case(level, slot, seed)` samples faults
  from pools (payment, events, pricing, process, traps, noise) under
  constraints, deterministically and independently of the episode's random
  stream.
- **Ambiguous payment truth.** Charges have a state (`captured`, `submitted`,
  `declined`, `lost`); the provider's ledger is private; pending outcomes settle
  at a seeded step; idempotency keys expire after `payment.idempotency_window_steps`.
- Tools `provider_lookup` (rationed by `payment.lookup_quota`) and
  `void_booking`; `invalidate_cache` takes an optional `flight_id`; hard-tier
  `get_logs` requires a service.
- Customer cancellations, duplicate requests sharing a `client_reference`,
  refunds, fare holds, a check-in circuit breaker
  (`checkin.auto_pause_after_attempts`), degraded payment health, log
  retention, misleading alerts and lying log lines.
- Hard reward: `0.2*availability + 0.45*incident_recovery + 0.15*verified +
  0.10*revenue_retained - cost_penalty`, zero before the settlement horizon;
  customers abandon a request after four failed retries. Score details add
  `tier`, `level`, `budget`, `provider_lookups`, `abandoned_requests`,
  `revenue_retained`, `cost_penalty`, `settlement_horizon_step` and
  `trap_outcomes`.
- Violation codes `cancelled_request_fulfilled`, `duplicate_sale`,
  `unfunded_confirmation`, `refund_missing`, `refund_unwarranted`,
  `cancelled_booking_backed`.
- Policies `oracle` (observation-only, solves every generated case),
  `blanket-hard`, `adopt-else-reconcile` and `wait-then-reconcile`.
- Evaluation: `run_evaluation(tier=)`, run IDs prefixed `hard:`, `by_level` in
  every aggregate block, `tier`/`level` on records and in exported
  demonstrations. External runner: `--tier`, a hard instruction that states the
  case's budget and drops the easy hints.
- Harbor: hard tasks under `<output>/hard/<split>/<id>/`, `tier` in
  `task_spec.json` and in the receipt body, receipt `schema_version` 4 for hard
  tasks, `difficulty = "hard-<level>"`, the oracle as the solution.
- OpenEnv: `ResetOptions.tier`, `reset(tier=)`, the two new tools in
  `ToolName` and `openenv.yaml`.
- Tests `test_hard_backend.py`, `test_hard_generator.py`,
  `test_hard_verification.py`, `test_hard_oracle.py`, `test_hard_controls.py`,
  `test_hard_environment.py` (with a golden copy of the easy reset observation)
  and `test_hard_surfaces.py`. Documentation: a hard-tier section in
  `docs/ENVIRONMENT.md`, updated `docs/AGENTS.md`, `docs/LIMITATIONS.md` and
  `docs/OPENENV.md`.

### Changed

- `configuration_contracts` lists the new `checkin.auto_pause_after_attempts`
  field and the read-only `payment.lookup_quota` and
  `payment.idempotency_window_steps`. Their defaults reproduce easy-tier
  behaviour.
- Easy Harbor receipts and task specs carry `"tier": "easy"`; the verifier
  checks the tier.
- The evaluator's and OpenEnv server's `--max-steps` default is "from the
  tier": 48 on the easy tier, the case's budget on the hard tier.

### Fixed (pre-publication review)

- Easy Harbor solutions crashed on import (`policies.py` now also works as a
  top-level module and the solution folder includes `oracle.py`), so the Harbor
  oracle scored 0.
- Harbor tasks name the sidecar build folder `world-<task id>` and the
  verifier's files `<task id>.spec.json` and `<task id>.receipt.key`, so a
  cached Docker build context of one task can no longer be built into another.
  `manifest.json` paths are relative to the output folder.
- The hard reward gains a 0.10 success term (weights now sum to 1.0), so a
  successful episode scores exactly 0.10 more than the same work left
  unfinished. The episode contract states this and that only the provider's
  ledger decides the settlement horizon.
- A hard reset with `max_steps` below the case's budget is rejected instead of
  running an episode that can never complete.
- `invalidate_cache(service="pricing", flight_id=<held flight>)` now sets
  `trap_outcomes.fare_hold_broken`.
- Loopback HTTP calls wait up to 30 s instead of 5 s, longer than SQLite's
  10 s busy timeout, so a briefly stalled worker no longer fails a reset.
- The Harbor bridge rejects non-finite numbers such as `1e999` before stepping
  and cleans up its episode on SIGTERM. The external runner rejects repeated
  seeds.
- A refund on a booking that is still pending is a void in progress, not
  `refund_unwarranted` (both tiers).
- `showcase --overwrite` replaces results already in `--output`.
- A step raises a clear error if the episode's temporary database was removed.
- The OpenEnv task API accepts a `tier` argument, so hard slots can be listed.
- The `oracle` and control evidence under `evidence/v0.5.0/hard/` was
  re-recorded on the final source. The coding-agent evidence under
  `evidence/v0.5.0/hard/agents/` predates these fixes: its provenance hashes
  differ from the shipped harness files and its `reward` field omits the 0.10
  success term; success, integrity and steps are unaffected.

### Known limitations

- Budgets were calibrated on 2026-10-03 from the oracle's step distribution over
  72 episodes; hard-tier model results cover a few seeds per slot and are a first
  calibration. The source, including the generator and the oracle, is public. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md#the-hard-tier).

## [0.4.0] - 2026-10-02 (internal build)

An independent review of 0.3.0 found that the suite could be solved without
reading any observation, and that the Harbor verifier and the reward could be
gamed. This release closes those findings and removes the legacy simulator.

### Added (evaluation)

- `airline_recovery.live.external`: runs Claude Code or Codex CLIs against the live world through the Harbor control script and grades the signed receipt.
- `examples/openai_compatible_agent.py`: the tool-calling loop for any OpenAI-compatible endpoint (Together AI, vLLM, Ollama); `examples/llm_agent.py` for the Anthropic API.
- Recorded model baselines in `evidence/v0.4.0/`.

### Added (demo)

- `/replays`: a read-only page on the OpenEnv server (and the Hugging Face
  Space's landing page) with step-by-step replays of every recorded hard-tier
  episode, built from `evidence/` by `scripts/build_replays.py`.

### Renamed

- The project was developed under the working name "SWA"; the package is now `airline_recovery` (distribution `airline-recovery-env`, CLI `airline-recovery`), task IDs are `airline-recovery-<split>-<index>`, and the OpenEnv environment is `airline_recovery`.

### Changed

- **Entity IDs are salted per episode.** Request, booking, charge, event and
  passenger IDs can no longer be computed from the seed, so they have to be read
  from query results. The seed still fixes the incident: the fault parameters,
  how many bookings a payment migration interrupts (2 to 4), which
  malformed-event shape appears, and the step at which a delayed fault arrives
  (2 to 5). In 0.3.0 one constant action list that never read an observation
  solved 33 of 33 episodes.
- **Malformed queue events come in three shapes:** truncated JSON, valid JSON
  missing a field, and valid JSON naming a different booking. A query for
  `NOT json_valid(payload)` no longer finds them all.
- **Reward.** The reward is terminal only. It is 0 after any integrity
  violation or before the injection horizon; otherwise
  `0.2 * availability + 0.6 * incident_recovery + 0.2 * verified`, capped at
  0.95 unless every success condition holds. `incident_recovery` uses a frozen
  denominator, the requests the incident ever left incomplete. It replaces a
  recovery measure (`0.4 * availability + 0.4 * recovery + 0.2 * verified`)
  that healthy traffic diluted, under which stalling raised the reward and a
  do-nothing policy averaged 0.34.
- **Harbor grading.** The sidecar world now runs the one authoritative episode,
  with a fresh random seed per container start, and returns an HMAC-signed
  receipt when the episode ends. The verifier checks only that receipt, using a
  per-task key shared by the sidecar and verifier images and absent from the
  agent image. An episode that never ends scores 0. In 0.3.0 the verifier
  replayed an agent-written action list in a fresh world with fixed seed 42, so
  a constant 19-action file scored 11 of 11, and an agent could damage the live
  world, learn from it, and submit a clean list.
- `airline-recovery build-harbor` and `python -m airline_recovery.live.harbor` no longer pin a seed by
  default; `--seed` pins one for debugging. Each generation draws new receipt
  keys.
- The evaluator's default output directory is `runs/eval`.
- The OpenEnv dependency is `openenv>=0.7.0,<0.8` from PyPI.

### Added

- **Post-finish safety check.** When every other success condition holds,
  trusted traffic retries a booking whose payment acknowledgement was lost and
  attempts a booking across a fare change, against the settings the agent left
  deployed. A resulting violation fails the episode. In 0.3.0 an agent could
  switch idempotency and price validation off after recovering and still score
  1.0.
- Score fields `incident_recovery`, `details.incident_requests` and
  `details.safety_check`.
- Evaluation summaries carry `trust_boundary: in-process`,
  `tasks_solved_on_every_seed`, `integrity_violation_episodes`,
  `excess_capture_cents` and `mean_incident_recovery`. A `module:function`
  policy shares the interpreter with the grader, so local scores are
  self-reported.
- Worker processes exit when their parent process dies.
- `openenv.yaml` has a `validation:` block.
- `examples/llm_agent.py`, a policy driven by a hosted language model.
- Documentation: `docs/ENVIRONMENT.md`, `docs/AGENTS.md`,
  `docs/LIMITATIONS.md` and this changelog.

### Removed

- The legacy "prototype" configuration simulator and its CLI subcommands.
- `airline_recovery progress`, `STATUS.md`, and the `reports/`, `tasks/` and `data/`
  directories.
- `docs/LIVE_BACKEND.md`, `docs/EVALUATION.md`, `docs/CLI.md`,
  `docs/LIVE_REVIEW.md` and `docs/BENCHMARK_REVIEW.md`, replaced by the
  documents above.

### Known limitations

- The receipt keys in the committed `live-tasks/` are public. Regenerate tasks
  with `airline-recovery build-harbor --tier all` for results you intend to report.
- OpenEnv episodes run over the WebSocket client. The stateless HTTP `/reset`
  and `/step` routes create a throwaway environment per request and cannot
  carry an episode.
- The eleven cases can still be solved by a hand-written rule; the coding-agent
  baselines above were measured after this build. See
  [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## [0.3.0] - 2026-10-02

### Added

- Three payment-migration cases (`train` 4, `eval` 2, `test` 2), bringing the
  suite to eleven. Pending bookings are left charged under two payment key
  versions, or not charged at all, so a default retry can charge a customer
  twice.
- `booking.payment_key_version` setting.
- `reconcile_booking` options: `idempotency_key` retries an exact key, and
  `existing_charge_id` adopts a committed charge without capturing again.
  Adoption checks the booking, the accepted amount, the sole charge and the
  seat hold in one transaction.
- `airline-recovery showcase`, a four-strategy comparison on the payment-migration training
  case.
- Bundled policies `blanket`, `reference-adopt` and `source-aware`.
- `business_impact` in the score: duplicate-charged bookings, excess capture,
  pending bookings and incomplete requests.

### Known issues, fixed in 0.4.0

- Solvable by one constant, observation-blind action list.
- Harbor verification could be satisfied by a constant action file.
- Guards could be disabled after recovery without penalty.
- Stalling raised the reward.

## [0.2.1] - 2026-10-02

Hardening after a review showed that a fixed sixteen-action script that ignored
every observation solved all 24 episodes of 0.2.0.

### Changed

- Request, passenger and booking IDs vary with the seed.
- The injected malformed event uses an ordinary event ID with no fault label.
- `payment.provider_latency_ms` is observable but cannot be patched by the
  agent.
- Per-action and total episode cost use one ledger.
- The short CLI command names run the live benchmark.

### Added

- `episode_contract` and `configuration_contracts` in the reset observation:
  when probes count, what resets verification, how traffic advances, and the
  legal fields, types and bounds of every setting.
- `trl-tools` demonstration export, with tool schemas, assistant tool calls and
  matching tool responses.

### Fixed

- A malformed tool name from a policy no longer crashes the evaluator.
- OpenEnv metadata reports the package version.

### Known issues

- The same blanket repairs plus one SQL query to look up IDs still solved all
  24 episodes.

## [0.2.0] - 2026-10-01

First live benchmark.

### Added

- Five HTTP worker processes (pricing, inventory, payment, booking, check-in)
  sharing a SQLite WAL database, with request logging and trace IDs.
- Eight cases across `train`, `eval` and `test`: lost payment acknowledgement,
  paused consumer, stale fare cache, stopped worker, malformed event, event
  schema change, and two delayed compound cases.
- An outcome verifier over database records, with integrity violations that set
  the reward to 0.
- Twelve agent tools, including bounded read-only SQL.
- OpenEnv server and WebSocket client.
- Harbor tasks with a sidecar world.
- Evaluation runner with matched task-seed grids, Wilson intervals, provenance
  hashes, and `json-actions` demonstration export from successful training
  episodes.
- `reference` and `nop` policies.
