# Hard tier specification (0.5.0)

This document specifies the hard tier as implemented in 0.5.0; where it and
the code disagree, the code is right. The 11 easy cases, their IDs, the easy
reset observation and the easy reward are unchanged by it.

## 1. Goal and principles

Frontier coding agents solve the easy tier because tool text explains each
fault's fix, every fix is free to apply everywhere, the ledger is readable in
full, and nothing degrades while they wait. The hard tier attacks those four
properties while keeping an oracle that solves every generated instance from
public evidence alone:

1. **Ambiguous payment truth.** A capture can be captured, declined or
   pending at the provider; the local `charges` table records attempts, the
   provider's ledger is private, and the agent learns the truth only through a
   quota-limited `provider_lookup` or by waiting for a step-based settlement.
2. **Retry is not always safe.** Idempotency keys expire after a seeded window;
   a retry with an expired key is a new charge.
3. **Traps.** Cancelled bookings that must be voided, duplicate requests from
   one customer, a payment worker that looks unhealthy but holds in-flight
   captures, a fare hold that a blanket cache invalidation breaks, events that
   look malformed but are valid, alerts that mislead.
4. **Pressure.** Smaller budgets, a cost term, customers abandoning retried
   requests, a circuit breaker that re-pauses the consumer when only the
   symptom was treated, and a settlement horizon so finishing early fails.
5. **Procedural generation.** `generate_case(level, slot, seed)` samples faults
   from pools under constraints, so a seed is a structural sample: 17 case
   structures at level 1, 600 at level 2 and about 6,000 at level 3.

Workers stay tier-agnostic: no tier flag reaches `worker.py`. Hard behaviour is
data-driven (rows in new tables, non-default config values) and therefore
absent in the easy tier by construction.

## 2. Data model (`store.py`)

All tables are created by `store.initialize` for both tiers. New columns carry
defaults that reproduce easy-tier behaviour.

| Table | Visibility | Definition |
|---|---|---|
| `episode_clock(step INTEGER NOT NULL)` | private | Single row, `step=0` at init. `environment.step` sets it to the new step number before executing the agent's action. |
| `charges` | public | Add `state TEXT NOT NULL DEFAULT 'captured' CHECK(state IN ('captured','submitted','declined','lost'))` and `created_step INTEGER NOT NULL DEFAULT 0`. All inserts use named columns. |
| `provider_ledger(attempt_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, booking_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('captured','declined','pending')), final_state TEXT CHECK(final_state IN ('captured','declined')), settles_at_step INTEGER, created_step INTEGER NOT NULL)` | private | The provider's truth. Index on `idempotency_key` and `booking_id`. |
| `provider_plan(idempotency_key TEXT PRIMARY KEY, outcome TEXT NOT NULL CHECK(outcome IN ('captured','declined','pending')), final_state TEXT, settles_at_step INTEGER)` | private | Written by the injector before the incident request; consulted once, on the first capture for that key. |
| `customer_events(event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('cancel')), step INTEGER NOT NULL)` | public | Customer cancellations. |
| `refunds(refund_id TEXT PRIMARY KEY, charge_id TEXT NOT NULL, booking_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, created_step INTEGER NOT NULL)` | public | |
| `fare_holds(hold_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, price_cents INTEGER NOT NULL, until_step INTEGER NOT NULL)` | public | A promised fare for a flight until a step. |
| `bookings` | public | Add `client_reference TEXT`, `confirmed_at INTEGER`; status CHECK becomes `('pending','confirmed','cancelled')`. |
| `request_logs` | public | Unchanged schema. Hard tier installs, via `admin_execute`, an `AFTER INSERT` trigger that deletes rows older than the newest R (R from the case). Never installed in the easy tier. |

`PUBLIC_TABLES` gains `customer_events`, `refunds`, `fare_holds`. The SQL
authorizer must deny `episode_clock`, `provider_ledger`, `provider_plan` exactly
as it denies `service_endpoints` today.

### Configuration fields (`DEFAULT_CONFIG` / `BOUNDS`)

| Service | Field | Type | Bounds | Default | Notes |
|---|---|---|---|---|---|
| `checkin` | `auto_pause_after_attempts` | int | 0..50 | 0 (off) | Circuit breaker, see pump. |
| `payment` | `lookup_quota` | int | 0..20 | 3 | Read-only to the agent (like `provider_latency_ms`). |
| `payment` | `idempotency_window_steps` | int | 1..1000 | 1000 | Read-only to the agent. Keys older than this many steps are forgotten. |

`configuration_contracts()` keeps deriving from `DEFAULT_CONFIG`; mark the two
new payment fields `readOnly: true`.

## 3. Worker semantics (`worker.py`, `runtime.py`)

Let `clock` be the value in `episode_clock`. "Ledger captured charge for a
booking" means a `provider_ledger` row with `state='captured'`; when a booking
has no ledger rows at all, fall back to `charges` rows with `state='captured'`
(this keeps the easy tier and existing tests unchanged).

### payment `POST /capture` `{booking_id, idempotency_key, amount_cents, timeout_ms}`

1. If `charges` has a row for the key (newest by rowid), let `age = clock - created_step`.
   - If `age > idempotency_window_steps`: the key is forgotten; continue as a new request (step 3), inserting a new charge and a new ledger row with state `captured`.
   - Else look at the ledger row for that key (if none, treat as `captured`):
     - `captured`: as today, return the existing charge (409 if booking or amount differ).
     - `pending`: return 504 `{"error":"capture outcome pending at provider"}` without writing anything.
     - `declined`: a fresh attempt is legal: insert a new charge (`state='captured'`) and a ledger row (`state='captured'`), then continue to step 4.
2. If `idempotency_enabled` is false, behave as today (fresh `attempt:` key each time); planned outcomes do not apply.
3. New key. If `provider_plan` has the key: insert a ledger row with `state=outcome`, `final_state`, `settles_at_step`; insert the charge with `state='submitted'`; delete the plan row; return 504 `{"error":"capture acknowledgement exceeded caller deadline; payment outcome is uncertain"}`. Otherwise insert the charge with `state='captured'` and a ledger row with `state='captured'`, and apply today's latency rule.
4. Return 200 with the charge row (plus `state`).

### payment `POST /settle` (trusted; environment calls it once per step)

For each ledger row with `state='pending'` and `settles_at_step <= clock`: set
`state=final_state`. For each ledger row whose state is final, if the matching
charge row has `state='submitted'` and `settles_at_step <= clock`, set the
charge's state to the ledger state. A charge in state `lost` is never updated
by settlement. Returns `{"settled": n}`.

### payment `GET /provider/lookup?idempotency_key=K`

Returns `{"idempotency_key", "state", "booking_id", "amount_cents"}` from the
ledger (`state` is `captured`, `declined` or `pending`), or 404 if unknown. The
environment enforces the quota, not the worker.

### payment `POST /refund` `{charge_id}` (trusted; called by booking `/void`)

Requires a ledger-captured charge for that `charge_id`'s key; inserts a
`refunds` row for the full amount; idempotent per charge. 409 otherwise.

### booking `POST /void` `{booking_id}`

- 404 unknown booking; 409 if status is `confirmed`; 200 no-op if already `cancelled`.
- 409 `{"error":"payment outcome unresolved"}` if the booking has a ledger row in state `pending`.
- Otherwise: status `cancelled`, delete the hold, and for each ledger-captured charge call payment `/refund`. Returns `{"booking_id","status":"cancelled","refunded_cents"}`.

### booking `POST /book`

- `confirm()` sets `confirmed_at = clock` and keeps writing the outbox event.
- `validate_price` passes if the quote matches `flights` **or** a `fare_holds` row for the flight with `until_step >= clock` and the same price.
- Adoption (`existing_charge_id`) requires: the charge belongs to the booking, amount matches, it is ledger-captured (or, with no ledger rows, locally captured), it is the only captured charge for the booking, and a matching hold exists. Otherwise 409 as today.
- Default retry (no options) of a pending booking uses the deployed key prefix exactly as today; the capture rules above decide what happens.

### pricing `GET /quote`

The quote does not read `fare_holds`. A promised fare exists only as the cached
`quote:<flight_id>` row, seeded with the held price. Disabling the cache or
evicting that row makes new quotes carry the current fare, which breaks the
promise; booking validation accepts a quote at the held price while the hold is
live.

### runtime

- `restart("payment")`: before relaunching, set `charges.state='lost'` for rows in state `submitted`. Lookup still reveals the ledger truth.
- `invalidate_cache(service, flight_id=None)`: with `flight_id`, delete only `quote:<flight_id>`.
- `pump()`: after a failed head event, if `0 < auto_pause_after_attempts <= attempts` for that event, patch `checkin.consumer_enabled=false` and write a `request_logs` row for `checkin` with message `consumer paused by circuit breaker after repeated delivery failure`.
- `metrics()`: payment gains `"health": "degraded"` when more than 30% of its last 50 request_logs rows are status 504, else `"healthy"`; other services always `"healthy"`.
- `lookup()` targeted reads remain for the trusted environment only.

## 4. Episode interface (`environment.py`)

### Tier plumbing

- `reset(seed, options={"split","index","tier"})`; `tier` defaults to `"easy"`.
- `case_for(split, index, tier="easy")`; for `hard`, returns `hardcases.generate_case(level, slot, seed)` resolved from the slot table in §6 (the environment passes the seed).
- `max_steps`: for hard, taken from the case's `budget` unless the caller passed `max_steps` explicitly; `LiveAirlineEnv(max_steps=None)` is allowed and means "from the case" (easy default stays 48).
- Hard task IDs: `airline-recovery-hard-{split}-{index:03d}`. `task_manifest(tier="easy")` returns exactly today's output.

### Order of operations in `step()` (hard; easy keeps today's order)

1. `step_count += 1`; write `episode_clock`.
2. Expire fare holds (no action needed; holds are compared against the clock).
3. Payment `/settle`.
4. Validate and execute the agent's action.
5. Delayed faults at `case.delayed_step` (unchanged mechanism).
6. Traffic `_workload()`, then abandonment: any never-accepted request retried 4 times is removed from `requirements` and counted in `abandoned_requests` (it keeps counting in `incident_requests`).
7. Score if done.

### Tools

Validation schemas are shared (`TOOLS`); descriptions differ (`TOOLS_HARD`
holds API semantics only, no cause→fix hints). Changes to the schema list,
present in both tiers:

| Tool | Change |
|---|---|
| `provider_lookup` | New. `{idempotency_key: string 1..200}` required. Read-only but quota-limited: each **successful** call consumes one unit of `payment.lookup_quota`; beyond the quota the step is rejected (`ok=false`, error `provider lookup quota exhausted`). Result: `{idempotency_key, state, booking_id, amount_cents}`. 404 from the worker → `ok=false`. |
| `void_booking` | New, mutating. `{booking_id: string}` required. Returns the worker response. Added to `MUTATING_TOOLS` and `evaluate.MUTATIONS`. |
| `invalidate_cache` | Gains optional `flight_id: string 1..20`. |
| `get_logs` | Unchanged schema; in the hard tier the environment rejects a call without `service` (`error: service is required`). |
| `reconcile_booking` | Unchanged schema. |

Hard descriptions follow these constraints: state
what the call does to which rows and nothing about when to use it. Example:
`patch_config` → "Write validated fields of one service's settings; see
configuration_contracts for fields, types and bounds."

### Hard reset observation

Easy: byte-identical to today. Hard: fields `episode_id, step, alerts, summary,
result, available_tools, configuration_contracts, episode_contract, mission,
tier, level`. `episode_contract` keys:
`max_actions, verification_eligible_from_step, required_healthy_probes,
step_numbering, mutating_tools, verification_reset_rule, traffic_rule,
cost_rule, provider_lookup_quota, graded_invariants, telemetry_rule,
success_conditions, post_finish_safety_check, reward`.

- `graded_invariants`: a dict of violation code → one-line meaning (all codes in §5), no fix hints.
- `telemetry_rule`: "Alerts, logs and metrics are best-effort and may be stale or wrong; durable business rows and the payment provider are authoritative."
- `post_finish_safety_check`: "When every other success condition holds, trusted traffic repeats several ordinary hazards against the settings you leave deployed; an integrity violation there fails the episode."
- `mission`: "Return the system to a state where every customer request has its correct outcome and every integrity rule holds, then verify and finish. Budget: N actions."
- `summary` adds `cancelled_bookings`, `fare_holds_active`, `provider_lookups_remaining`.
- Alerts: the environment emits the structural alerts (worker down, probes failing, pending bookings, backlog) plus the case's `misleading_alert` if any.

### Hard score and reward

Success (all of): every required request resolved (confirmed with hold, one
ledger-captured charge, check-in and delivered valid event; **or** cancelled
with a matching cancel event and, if charged, a refund); no violation at any
point including the post-finish safety check; availability 1 over the last
three windows; two counted probes since the last reset; horizon complete:
`step >= 6`, delayed faults injected, and no `provider_ledger` row still in
state `pending` at the provider (an agent learns this from a lookup answering
`pending`; a local charge can stay `submitted` after the provider has answered,
so local state alone does not decide it); terminated by `finish` within budget.

A voided booking whose request has no cancel event stays incomplete forever, so
"void everything" never succeeds.

```
reward = 0.2*availability + 0.45*incident_recovery + 0.15*verified
         + 0.10*revenue_retained + 0.10*success - cost_penalty
revenue_retained = 1 - abandoned_requests / max(1, incident_requests)
cost_penalty = min(0.25, 0.004*reads + 0.02*mutations + 0.03*provider_lookups)
```

Zero after any violation or before the horizon; at most 1.0. The success term
makes a verified, finished recovery outscore the same work left unfinished.
`reads` = successful non-mutating tool calls excluding `probe`, `finish` and
`provider_lookup` (lookups are priced separately).
Score `details` adds `tier, level, budget, provider_lookups, abandoned_requests,
revenue_retained, cost_penalty, settlement_horizon_step` and `trap_outcomes`
(descriptive: `cancelled_voided, cancelled_completed, duplicate_voided,
duplicate_completed, unknown_resolved_by_lookup, unknown_resolved_by_settlement,
payment_restarted_with_inflight, fare_hold_broken`).

Post-finish safety check (hard): seeded choice of 3 from
`{lost_ack_retry, fare_change, cancel_then_retry, expired_key_retry,
duplicate_event_delivery}`; each uses the restore pattern of the existing
check. Easy keeps its two fixed hazards.

## 5. Verification (`verification.py`)

`assess(snapshot, requirements, rejected, anchors)` keeps its signature and
reads new tables with `snapshot.get(name, [])`. Truth for "a charge": ledger
captured rows when the booking has ledger rows, else local captured rows.
Completeness of a required request: as today, **or** booking `cancelled` and a
`customer_events` cancel exists for its `request_id` and (if any ledger-captured
charge) a matching `refunds` row exists.

New codes (row predicates only):

| Code | Trigger |
|---|---|
| `cancelled_request_fulfilled:<bkg>` | status `confirmed` and a cancel event with `step <= confirmed_at` exists for the request |
| `duplicate_sale:<client_reference>` | two or more `confirmed` bookings share a non-null `client_reference` |
| `unfunded_confirmation:<bkg>` | status `confirmed` and ledger-captured charges ≠ 1 (only when the booking has ledger rows; otherwise today's `unbacked_confirmation` applies) |
| `refund_missing:<bkg>` | status `cancelled`, a ledger-captured charge, no refund of equal amount |
| `refund_unwarranted:<bkg>` | a refund for a `confirmed` booking, or for no booking |
| `cancelled_booking_backed:<bkg>` | status `cancelled` with a hold or a check-in |

`duplicate_charge` counts captured charges (ledger when present). Fare holds
need no new code: the traffic generator's expected amount honours live holds,
so a booking accepted at another fare trips `accepted_request_changed`.
Existing snapshots without the new keys must yield exactly today's codes.

## 6. Generator (`hardcases.py`, `hard_injector.py`)

```python
@dataclass(frozen=True)
class Fault:   kind: str; params: dict
@dataclass(frozen=True)
class HardCase:
    tier: str = "hard"; split: str; index: int; level: int; slot: int; seed: int
    budget: int; initial: tuple[Fault, ...]; delayed: tuple[Fault, ...]
    delayed_step: int; traps: tuple[Fault, ...]; noise: tuple[Fault, ...]
    scale_rows: int; log_retention: int | None; lookup_quota: int
    idempotency_window: int; safety_hazards: tuple[str, ...]
    misleading_alert: dict | None
```

`generate_case(level, slot, seed)` is deterministic from
`random.Random(f"airline-recovery:hard:v1:{split}:{level}:{slot}:{seed}")`, separate
from the episode RNG. Slot table: train slots 0–5 → levels 1,1,2,2,3,3; eval
0–2 → 1,2,3; test 0–2 → 1,2,3. `delayed_step` in 2..5.

Pools (≤1 from each of A–D; E and F are modifiers):

- **A payment:** `lost-ack-mixed` (cohort 4–10 pending with planned outcomes captured/declined/pending, at least one of each when cohort ≥ 3; `settles_at_step` in 4..budget−8); `key-migration-live` (both key prefixes present, deployed version drawn, ≥1 declined); `payment-degraded-inflight` (latency > deadline storm, ≥2 pending-unknown, misleading alert recommends restart); `expired-key` (modifier on any of the above: `idempotency_window_steps` is seeded longer than the budget and the chosen keys are backdated past it, so exactly those keys are already expired at reset and stay expired).
- **B events:** `poison` (three malformed shapes plus one valid-looking decoy), `schema-mixed` (schema 1 and 2 interleaved), `breaker-paused` (`auto_pause_after_attempts=3` with a poison head).
- **C pricing:** `stale-cache`, `stale-cache+fare-hold` (hold on the other flight; scoped invalidation required).
- **D process:** `pricing-down`, `inventory-down-then-up` (pending rows without holds), `checkin-down`.
- **E traps:** `cancelled-pending` (1–3 cancel events at reset or at `delayed_step`), `duplicate-client-reference` (one customer, two request IDs; one confirmed and one pending, or both pending), `restart-bait` (requires A=degraded).
- **F noise (level 3):** `log-retention` R ∈ {300,400,500}; `misleading-alert`; `lying-log-line` (one of three fixed lies, each refuted by a durable row); `self-healed-transient` (a 503 burst in logs from a worker that is running).
- **Alert-only instance:** at level 1, one slot in six draws no A–D fault and only F decoys; the correct episode is inspect, probe twice, finish.

Constraints: E.cancelled requires A; E.duplicate requires A or D.inventory;
E.restart-bait requires A.degraded; C.fare-hold requires C.stale-cache; the
number of pending-unknown charges that settle after `budget−8` ≤ `lookup_quota`;
cohort ≤ 10; `lookup_quota ≥ expired_count + 1` (an expired key can only be resolved by lookup); level ladder: hard-1 = one A or B fault + one compatible E trap, budget 32;
hard-2 = A + B (+C 50%) + two E traps + one delayed fault from B or C, budget 36;
hard-3 = hard-2 + one or two F + `scale_rows` 300–400 historical confirmed
bookings (all anchored) + breaker bias, budget 34. Budgets were calibrated on
2026-10-03 from the oracle's step distribution over 72 episodes (§8).

`hard_injector.inject(env, fault)` executes each fault through HTTP and
`admin_execute` like today's `_inject`, writing `provider_plan` rows before the
incident requests. It never writes the fault name anywhere an agent can read.

## 7. Oracle and controls (`oracle.py`, `policies.py`)

`OraclePolicy` is observation-only (imports nothing from environment,
scenarios, hardcases, verification or runtime; a test asserts this). Round 1
(5 reads): `get_config`; `get_metrics`; SQL-A pending bookings LEFT JOIN holds,
charges (state, key, charge_id, created_step), cancel events, and sibling
counts by `client_reference`; SQL-B pending outbox with payload, `json_valid`,
attempts; SQL-C fare holds, flights, cache rows and 409 counts from
`request_logs` grouped by message. Decision table per pending booking:

| Evidence | Action |
|---|---|
| cancel event for the request | `void_booking` |
| shares `client_reference` with a confirmed sibling or an older pending sibling | `void_booking` the newer |
| local charge `captured` | `reconcile_booking(existing_charge_id)` |
| local charge `declined` | `reconcile_booking(idempotency_key)` (fresh legal attempt) |
| local charge `submitted` or `lost` | `provider_lookup(key)` if quota remains, else probe and re-read later; captured → adopt, declined → retry key, pending → defer |
| no charge, hold present | plain `reconcile_booking` after settings are safe |
| no charge, no hold | plain `reconcile_booking`; on sold-out 409 → `void_booking` |

Settings: `payment_timeout_ms > provider_latency_ms`; idempotency flags true;
`accepted_schema` = max observed schema version; `batch_size ≥` pending events;
quarantine only events failing the public validity predicate; enable the
consumer after quarantining; scoped `invalidate_cache` only for flights whose
cache version < `flights.version` with no live hold; restart only `running=false`
workers, never payment while any charge is `submitted`. Then probe ×2 and
finish; on an unhealthy probe re-run round 1 (max 3 rounds).

`load_policy` gains `oracle`, `blanket-hard` (blanket plus restart-all,
unscoped invalidate, reconcile-all-pending), `adopt-else-reconcile` and
`wait-then-reconcile` (one-branch controls).

## 8. Tests and calibration

The hard tier is covered by these test modules:

- `tests/test_hard_backend.py`: capture plan semantics, settlement, expiry, lookup, void/refund, fare holds, scoped invalidation, breaker, degraded health, restart marks lost, private tables denied.
- `tests/test_hard_generator.py`, `tests/test_hard_verification.py`, `tests/test_hard_oracle.py`, `tests/test_hard_controls.py`: determinism and structure variety over 500 cases; constraint checks; every trap state → exactly the expected code, correct resolution → clean; easy snapshots unchanged; oracle success on levels 1–3 × slots × seeds 1..N (N from `AIRLINE_HARD_SEEDS`, default 4 in CI); controls fail for the designed reason (measured: at most 6 of 36 episodes on seeds 1–3).
- `tests/test_hard_environment.py`, `tests/test_hard_surfaces.py`: golden easy reset observation unchanged; hard observation leaks no fault names or settle steps; quota exhaustion; settlement horizon; abandonment; cost arithmetic; CLI/evaluate/external/harbor/OpenEnv tier plumbing.
- Calibration: oracle 100% on seeds 1–20 per slot with budget ≥ 1.25 × its p95 steps; nop/blanket/blanket-hard/source-aware/reference/reference-adopt/one-branch rules each failing for the designed reason; then Claude Code (Sonnet, Haiku) and Codex via `external.py --tier hard`, target 30–70% at levels 1–2 and ≤30% at level 3; report success, `tasks_solved_on_every_seed`, pass^k, violations, steps, cost. If a level is ≥90%, strengthen the trap pool that `trap_outcomes` shows is not biting, before touching budgets.

## 9. Surfaces

- CLI: `list --tier easy|hard|all` (default easy), `demo --tier`, `build-harbor --tier` (hard tasks under `<output>/hard/<split>/<id>/`), `showcase` unchanged.
- `evaluate.py`: `--tier`; `run_evaluation(tier=)`; `run_id` prefixed `hard:` for hard; `aggregate` adds `by_level`; `PUBLIC_OBSERVATION_FIELDS` gains `tier`, `level`.
- `external.py`: `--tier`; hard instruction text states the budget and drops the easy hints.
- `harbor.py`/`bridge.py`: `tier` in `task_spec.json` and in the receipt body; hard receipts use `schema_version: 4` and `verify_receipt` checks tier; `metadata.difficulty = "hard-N"`; solution copies `oracle.py` for hard tasks.
- OpenEnv: `ResetOptions.tier`, `reset(tier=)`, `ToolName` gains the new tools; `/splits` and task listing unchanged; `openenv.yaml` `declared_tools` updated.
- Docs: `docs/ENVIRONMENT.md` hard-tier section, `docs/LIMITATIONS.md`, `docs/AGENTS.md`, `docs/OPENENV.md`, README and `CHANGELOG.md`.
