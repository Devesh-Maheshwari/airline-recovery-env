# Environment reference

This page describes Airline Recovery Env 0.5.0 as implemented in `airline_recovery/live/`. Where this page and
the code disagree, the code is right; the relevant module is named in each section.

Related pages: [bringing an agent](AGENTS.md), [OpenEnv server](OPENENV.md),
[limitations](LIMITATIONS.md), [changelog](../CHANGELOG.md).

## Contents

- [What runs](#what-runs)
- [Data model](#data-model)
- [Episode interface](#episode-interface)
- [Reset observation](#reset-observation)
- [Tools](#tools)
- [Configuration fields](#configuration-fields)
- [Episode rules](#episode-rules)
- [Success conditions](#success-conditions)
- [Reward and score](#reward-and-score)
- [Integrity violation codes](#integrity-violation-codes)
- [Cases](#cases)
- [What the seed controls](#what-the-seed-controls)
- [Example trajectory](#example-trajectory)
- [Hard tier](#hard-tier)

## What runs

Each episode starts five operating-system processes (`airline_recovery/live/worker.py`), each
an HTTP server bound to a random port on `127.0.0.1`, sharing one SQLite database
in WAL mode inside a temporary directory. Closing the environment terminates the
workers and deletes the directory. A worker also exits by itself within about a
second if its parent process dies.

| Service | Endpoint | Behaviour |
|---|---|---|
| `pricing` | `GET /quote?flight_id=` | Returns the fare and fare version. With `cache_enabled`, the quote is stored under `quote:<flight_id>` and served from the cache until evicted. |
| `inventory` | `POST /reserve` | Creates one seat hold per booking inside a write transaction. With `enforce_capacity`, returns 409 when the flight is full. Repeating a reservation for the same booking is a no-op. |
| `payment` | `POST /capture` | Writes a charge. With `idempotency_enabled`, a repeated `idempotency_key` returns the existing charge (409 if the booking or amount differ). If `provider_latency_ms` exceeds the caller's deadline, the charge is **committed first** and the response is 504. |
| `booking` | `POST /book` | Orchestrates quote, seat hold, capture and confirmation; see below. |
| `checkin` | `POST /consume` | Consumes one outbox event and creates a check-in row. Returns 503 when `consumer_enabled` is false, 422 for a malformed or unsupported payload, 409 if the booking is not confirmed. |

Every service also answers `GET /health`. Every request is written to
`request_logs` with its status, duration, trace ID and error message; nested
calls made by `booking` share the caller's trace ID. A stopped worker produces
real refused connections, which callers report as 503. Workers accept only
requests carrying their episode's random token and answer 403 to anything else,
so a process outside the episode cannot drive them.

Agents do not call these endpoints. They act through the [tools](#tools), and
see the endpoints only in logs.

### Booking flow

For a new `request_id`, `booking`:

1. fetches a quote from `pricing`;
2. with `validate_price`, rejects the quote with 409 if its amount or version
   differs from the flight record; otherwise inserts a `pending` booking at the
   quoted amount;
3. reserves a seat at `inventory` (a 409 removes the pending booking);
4. captures payment with the key `booking:<booking_id>` when
   `payment_key_version` is 1 or `booking-v2:<booking_id>` when it is 2. With
   `payment_idempotency_enabled` false, it sends a random `attempt:<uuid>` key
   instead;
5. marks the booking `confirmed` and writes a schema-version-1 outbox event in
   one transaction.

Repeating a `request_id` with the same flight and passenger returns the existing
confirmed booking, or resumes a pending one at its originally accepted amount
without requesting a new quote. A different flight or passenger under an existing
`request_id` returns 409.

A capture that returns 504 leaves the booking `pending` with a committed charge.
Resuming it sends the key for the **currently deployed** `payment_key_version`.
If that differs from the key the charge was made under, the payment service
treats it as a new payment and writes a second charge.

### Outbox delivery

Pending events are delivered in ascending `event_id` order. Delivery stops at the
first event the consumer rejects, so one bad event blocks everything behind it.
Each attempt increments the event's `attempts` counter. A batch never exceeds
the `checkin.batch_size` setting.

## Data model

All state is in one SQLite database (`airline_recovery/live/store.py`). The `query_sql` tool
can read these tables and `sqlite_master`/`sqlite_schema`:

| Table | Columns |
|---|---|
| `flights` | `flight_id`, `capacity`, `price_cents`, `version` |
| `bookings` | `booking_id`, `request_id` (unique), `flight_id`, `passenger_id`, `amount_cents`, `status` (`pending` or `confirmed`) |
| `holds` | `booking_id`, `flight_id`, `passenger_id` |
| `charges` | `charge_id`, `booking_id`, `idempotency_key`, `amount_cents` |
| `outbox` | `event_id`, `booking_id`, `payload` (JSON text), `status` (`pending`, `delivered` or `quarantined`), `attempts` |
| `checkins` | `booking_id`, `flight_id`, `passenger_id` |
| `service_config` | `service`, `value` (JSON text of the deployed settings) |
| `cache` | `key`, `value` |
| `request_logs` | `id`, `service`, `method`, `path`, `status`, `duration_ms`, `trace_id`, `message` |

The `query_sql` tool description names only the first six tables and
`request_logs`; `service_config` and `cache` are readable as well. The
`service_endpoints` table is not readable.

Flights at reset (with the default 48-action budget):

| Flight | Capacity | Fare (cents) | Role |
|---|---:|---:|---|
| `F100` | 120 | 20000 | Normal sales |
| `F200` | 150 | 25000 | Normal sales |
| `F900` | 1 | 15000 | Sold out after the first booking; every later request for it must be refused |

Capacity of `F100` and `F200` is raised to at least `2 * max_steps + 20` so
that valid traffic never legitimately sells out.

Identifier shapes: `req_<hex>`, `bkg_<hex>`, `chg_<hex>`,
`evt_<8-digit ordinal>_<booking_id>`, `SYN-P<hex>` for passengers. See
[what the seed controls](#what-the-seed-controls) for how they are derived.

### `query_sql` limits

Enforced in `LiveStack.query` (`airline_recovery/live/runtime.py`) on a read-only connection
with a SQLite authorizer:

| Limit | Value |
|---|---|
| Query length | 1 to 4000 characters |
| Statements per call | One |
| Permitted operations | `SELECT`, column reads of the tables above, SQL functions |
| Rejected | Writes, `PRAGMA` (including `pragma_*` table functions), `ATTACH`, recursive common table expressions, temporary tables, `load_extension`, `readfile`, `writefile`, any other table |
| Rows returned | First 500; further rows are dropped without an error |
| Response size | 512 KiB; larger results are an error |
| Single value size | 262144 bytes |
| Computation | About one million SQLite virtual-machine instructions, then the query is aborted |

BLOB values are returned as `0x`-prefixed hex strings. A rejected query returns
`Read-only query rejected: <reason>`. JSON functions such as `json_valid` and
`json_extract` are available.

## Episode interface

`airline_recovery.live.environment.LiveAirlineEnv` is the episode boundary.

```python
from airline_recovery.live.environment import LiveAirlineEnv

with LiveAirlineEnv() as env:                      # max_steps=48 by default; 8..256 allowed
    observation, info = env.reset(seed=1, options={"split": "train", "index": 1})
    observation, reward, terminated, truncated, info = env.step(
        {"tool": "get_metrics", "arguments": {}})
```

- `reset(seed=0, options=None)`: `seed` must be an `int`; `options` accepts
  `split` (`train`, `eval`, `test`; default `train`), `index` (default 0) and
  `tier` (`easy` or `hard`; default `easy`, see [hard tier](#hard-tier)).
  Returns the [reset observation](#reset-observation) and
  `{"synthetic": true, "backend": "live-http-sqlite", "action_budget": <max_steps>}`.
  Everything on this page up to the hard-tier section describes the easy tier,
  which is what a reset without a `tier` option runs.
- `step(action)`: `action` must be a dict with exactly the keys `tool` (string)
  and `arguments` (dict). Returns `(observation, reward, terminated, truncated, info)`.
  `info` always has `action_cost` and `requests`; on the final step it also has
  `score`. Calling `step` before `reset` or after the episode ends raises
  `RuntimeError`.
- `state()`: `{"episode_id", "step_count", "done"}`.
- `LiveAirlineEnv.tools()` and `LiveAirlineEnv.task_manifest()` return the tool
  schemas and the public task list without starting an episode.

`terminated` is true when `finish` is accepted. `truncated` is true when the
action budget is exhausted without `finish`. Either ends the episode and
produces a score.

## Reset observation

| Field | Type | Contents |
|---|---|---|
| `episode_id` | string | Random per reset. |
| `step` | integer | 0 at reset; the first action is step 1. |
| `alerts` | list | Current alerts, each `{service, severity, message}`. See below. |
| `summary` | object | Counters. See below. |
| `result` | object or null | `null` at reset; afterwards the previous action's result. |
| `available_tools` | list | Tool schemas: `{name, description, parameters}` with JSON Schema parameters. Reset only. |
| `configuration_contracts` | object | Per service: `observed_fields` and `patch_schema` (types, bounds, read-only flags). No current or baseline values. Reset only. |
| `episode_contract` | object | The episode rules, success conditions and reward formula as text and numbers. Reset only. |
| `mission` | string | Task statement. Reset only. |

Later observations carry only `episode_id`, `step`, `alerts`, `summary` and
`result`. An agent that needs the schemas or rules later must keep them.

`result` after each step is `{"tool", "ok", "data"}`, plus `"error"` (a string)
when `ok` is false. `tool` is `null` if the action did not name one as a string.

`summary` fields: `booking_count`, `confirmed_bookings`, `pending_bookings`,
`checkins`, `outbox_pending`, `verification_windows`,
`verification_eligible_from_step` (6), `required_verification_windows` (2),
`remaining_actions`.

Alerts that can appear:

| Service | Severity | Message | Condition |
|---|---|---|---|
| any | critical | `HTTP worker unavailable` | That worker process is not running |
| `booking` | critical | `Customer transaction probes are failing` | The latest traffic window had an unexpected outcome |
| `booking` | warning | `Accepted bookings remain pending` | At least one booking is `pending` |
| `checkin` | warning | `Durable event backlog has undelivered records` | At least one outbox event is `pending` |

Task IDs, observations and alerts never name the injected fault.

## Tools

Twelve tools (`TOOLS`, `_validate` and `_execute` in `airline_recovery/live/environment.py`).
`service` is always one of `inventory`, `pricing`, `payment`, `booking`,
`checkin`. Tools marked *mutating* reset the verification count when they
succeed and cost more (see [episode rules](#episode-rules)).

| Tool | Arguments | Mutating | Returns in `result.data` |
|---|---|---|---|
| `get_metrics` | `service` (optional) | no | `services`: per service `running`, `requests`, `errors` (status 400 or above), `status_counts`; plus `outbox` and `bookings` counts by status |
| `get_logs` | `service` (optional) | no | The 40 most recent `request_logs` rows, oldest first, optionally for one service |
| `get_config` | `service` (optional) | no | That service's deployed settings, or all five keyed by service |
| `patch_config` | `service` (required), `values` (required object) | yes | The service's full settings after the change |
| `restart_service` | `service` (required) | yes | `{"restarted": <service>}`. Database records and settings are preserved |
| `query_sql` | `query` (required string, 1 to 4000 characters) | no | List of row objects; see [limits](#query_sql-limits) |
| `replay_events` | `limit` (optional integer 1 to 50, default 10) | yes | `attempted`, `delivered`, `remaining`, `blocked_event_id`, `error` |
| `quarantine_event` | `event_id` (required string) | yes | `{"event_id", "status": "quarantined"}` |
| `invalidate_cache` | `service` (required) | yes | `{"service", "cleared"}`. Only `pricing` has a cache; other services return `cleared: 0` |
| `reconcile_booking` | `booking_id` (required), `idempotency_key` or `existing_charge_id` (optional, mutually exclusive); each a string of 1 to 200 characters | yes | The booking service's response: `status`, `body`, `duration_ms`, `trace_id` |
| `probe` | none | no | `healthy`, `verification_windows`, `customer_requests`, `backlog_complete` |
| `finish` | none | no | `{"requested": "finish"}`; ends the episode |

### Validation

An action is rejected before execution, with `result.ok` false, when:

| Condition | `result.error` |
|---|---|
| Not a dict with exactly `tool` and `arguments` | `Action requires exactly tool and arguments` |
| `tool` not a string or `arguments` not a dict | `Tool must be string and arguments must be object` |
| Unknown tool name | `Unknown tool` |
| An argument not in the schema, or a required one missing | `Unexpected or missing arguments` |
| Wrong argument type (a boolean is not an integer) | `Invalid <name> type` |
| `service` not one of the five | `Unsupported service` |
| Empty string, or longer than its limit (300 where the schema gives none) | `Invalid <name> length` |
| Integer out of range | `Invalid <name> range` |

A rejected action still consumes a step, still advances traffic and still costs
0.001. It does not reset the verification count by itself. The same holds for an
action that passes validation and then fails in execution, with one exception:
a `reconcile_booking` that fails with HTTP 504 (see below).

### Tool-specific behaviour

**`patch_config`** validates every field before writing anything
(`store.patch_config`). Errors: `Configuration values must be a nonempty object`,
`Unknown configuration field for <service>: <name>`, `<name> must be bool` or
`must be int`, `<name> must be between <low> and <high>`. Patching
`payment.provider_latency_ms` is refused with
`provider_latency_ms is an external observation, not an operator setting`.
Workers read settings on every request, so no restart is needed.

**`replay_events`** delivers at most `min(limit, checkin.batch_size)` pending
events in `event_id` order and stops at the first rejection. A blocked delivery
is not a tool error: `ok` is true, `blocked_event_id` names the event and
`error` carries the consumer's message.

**`quarantine_event`** moves one `pending` event to `quarantined`, keeping its
payload. Any other event ID fails with `event_id must identify a pending event`.
Quarantining an event that is valid for its booking is an integrity violation
(`valid_event_discarded`).

**`reconcile_booking`** resubmits an existing booking's original request to the
booking service. It has three modes:

| Call | Effect |
|---|---|
| `booking_id` only | Captures with the key for the currently deployed `payment_key_version`. It does not look for earlier payments. If the booking was charged under the other key version, this writes a second charge. |
| `booking_id` + `idempotency_key` | Captures with exactly that key, so a key read from `charges` reuses the existing charge. Requires `booking.payment_idempotency_enabled` and `payment.idempotency_enabled` to be true; with booking idempotency off the supplied key is ignored. |
| `booking_id` + `existing_charge_id` | Adopts a committed charge without capturing again. Succeeds only if that charge is the sole charge for the booking, matches the accepted amount, and a matching seat hold exists; otherwise 409 and no business rows change. |

Errors: `booking_id was not found`;
`idempotency_key and existing_charge_id are mutually exclusive`;
`Booking recovery HTTP <status>: <message>` for any response of 400 or above.
A failed call can still have had an effect: a capture that returns 504 has
already committed its charge. For that reason a `reconcile_booking` that fails
with 504 is treated as a mutation even though `ok` is false: it resets the
verification count, costs the mutating surcharge and is counted in
`details.mutations`.

**`probe`** takes no action of its own. Traffic advances on every step; `probe`
is the only tool whose healthy windows are counted towards verification.

## Configuration fields

From `DEFAULT_CONFIG` and `BOUNDS` in `airline_recovery/live/store.py`. The reset observation
publishes the same types and bounds under `configuration_contracts`, without
values. Types are exact: a boolean is not accepted for an integer field.

| Service | Field | Type | Bounds | Value in a healthy stack | Effect |
|---|---|---|---|---|---|
| `inventory` | `enforce_capacity` | boolean | | `true` | When false, seats are held beyond capacity |
| `pricing` | `cache_enabled` | boolean | | `true` | When false, every quote reads the flight record |
| `payment` | `provider_latency_ms` | integer | 0 to 10000 | 80 | Read-only for agents. A capture whose caller deadline is lower commits, then returns 504 |
| `payment` | `idempotency_enabled` | boolean | | `true` | When false, a repeated key writes another charge |
| `booking` | `payment_timeout_ms` | integer | 1 to 10000 | 200 | Deadline sent with each capture |
| `booking` | `payment_idempotency_enabled` | boolean | | `true` | When false, every capture uses a fresh random key |
| `booking` | `payment_key_version` | integer | 1 to 2 | 1 | Selects the `booking:` or `booking-v2:` key prefix |
| `booking` | `validate_price` | boolean | | `true` | When false, a stale cached quote becomes the accepted fare |
| `checkin` | `consumer_enabled` | boolean | | `true` | When false, deliveries return 503 |
| `checkin` | `batch_size` | integer | 1 to 50 | 10 | Upper bound on events per delivery batch |
| `checkin` | `accepted_schema` | integer | 1 to 2 | 1 | 1 accepts only schema-version-1 events; 2 accepts versions 1 and 2 |

Settings are never graded directly. The unsafe switches change real
transactions, and those transactions are what the verifier reads.

## Episode rules

1. **Budget.** 48 actions by default. Every call to `step` consumes one,
   including reads and rejected actions.
2. **Traffic advances on every action.** After the action executes, and before
   its observation is built, the environment runs one traffic window:
   - it retries every required request that has no booking row yet;
   - it sends one new booking for `F100` and one for `F200`;
   - it redelivers the first of those requests, which must not create a second charge;
   - it sends one booking for the sold-out flight `F900`, which must be refused with 409;
   - it delivers pending outbox events (up to `checkin.batch_size`).

   The window's value is the fraction of those requests with the expected
   outcome. Traffic does **not** retry bookings that were accepted and are
   `pending`; those are left for `reconcile_booking`.
3. **Healthy window.** A window is healthy when its value is 1, every required
   request is complete, and no integrity violation has occurred at any point in
   the episode.
4. **Verification.** A `probe` counts when it is accepted, the step number is 6
   or higher, and its window is healthy. Two counted probes are required.
   The count returns to zero on every successful mutating tool call (including
   one that changes nothing), on a `reconcile_booking` that fails with 504, on
   any unhealthy window, and when a delayed fault arrives.
5. **Delayed faults.** Four cases inject a second fault during recovery. It
   arrives after the agent's action at a seeded step between 2 and 5, before
   that step's traffic.
6. **Injection horizon.** The horizon is complete at step 6 or later, once any
   delayed fault has been injected. Finishing earlier scores 0.
7. **Cost.** Each action costs 0.001; a successful mutating call, or a
   `reconcile_booking` that fails with 504, costs a further 0.01. Cost is
   reported in the score and does not change the reward.

## Success conditions

`success` is true only when all of these hold at the end of the episode:

1. Every required request is complete: its booking is `confirmed`, with exactly
   one charge, a seat hold, a check-in row and a delivered valid event.
2. No integrity violation occurred at any point in the episode. Violations are
   remembered even if the offending rows later change.
3. Availability is 1: every request in the last three traffic windows had the
   expected outcome.
4. Two counted healthy probes since the last reset of the verification count.
5. The injection horizon is complete.
6. The deployed settings survive the post-finish safety check.

"Required requests" are all customer requests issued by the trusted traffic:
three bookings accepted before the incident, any issued while injecting the
fault, and two per traffic window. Sold-out requests for `F900` are not
required; they must stay refused.

### Post-finish safety check

When conditions 1 to 5 hold, `_safety_check` runs once against the settings the
agent left deployed:

- **Lost acknowledgement, then retry.** The booking deadline is temporarily set
  below the provider latency, one `F100` booking is sent (its capture commits
  and returns 504), the original deadline and latency are restored, and the same
  request is sent again. With idempotency disabled at either `payment` or
  `booking`, the retry writes a second charge: `duplicate_charge` and
  `unbacked_confirmation`.
- **Booking across a fare change.** A quote for `F200` is requested, the fare is
  then raised by 1900 cents, and a new `F200` booking is sent. With
  `validate_price` false and the pricing cache enabled, the booking is accepted
  at the old fare: `accepted_request_changed`.

Any violation found there sets `integrity` and `success` to false and the reward
to 0. The outcome is reported in `score.details.safety_check` as
`{ran, violations}`; it is `{"ran": false}` when conditions 1 to 5 did not
hold. When the check finds a violation, `details.business_impact` reports the
records as the check left them. The check's own two requests are graded on
integrity only. They are not added to the required requests and do not affect
`recovery` or `incident_recovery`.

## Reward and score

Reward is terminal only. Every step before the last returns 0. The final step
returns:

```text
if any integrity violation occurred, or the injection horizon is incomplete:
    reward = 0
else:
    reward = 0.2 * availability + 0.6 * incident_recovery + 0.2 * verified
    if not success:
        reward = min(reward, 0.95)
```

| Term | Definition |
|---|---|
| `availability` | Mean value of the last three traffic windows |
| `incident_recovery` | Of the requests that were incomplete at the end of any traffic window during the episode, the share that are complete now. The denominator only grows, so healthy traffic that arrives while an agent waits does not dilute it. It is 1.0 if no request was ever left incomplete |
| `verified` | 1 if two counted healthy probes are standing, else 0 |

`info["score"]` on the final step:

| Field | Meaning |
|---|---|
| `reward` | As above, rounded to six places |
| `success` | All success conditions hold |
| `availability`, `incident_recovery`, `verified` | The reward terms |
| `integrity` | No violation at any point, including the safety check |
| `recovery` | Completed required requests divided by all required requests, at the end of the episode |
| `cost` | Total action cost |
| `details.violations` | Every violation code seen during the episode |
| `details.business_impact` | `duplicate_charge_bookings`, `excess_capture_cents`, `pending_bookings`, `incomplete_customer_requests`; measured after the safety check when that check found a violation |
| `details.safety_check` | `{ran, violations}` for the post-finish safety check |
| `details.incident_requests` | Denominator of `incident_recovery` |
| `details.completed_requests`, `details.required_requests`, `details.incomplete_request_ids`, `details.quarantined_valid_events` | Verifier counts |
| `details.requests` | Customer requests sent by traffic |
| `details.service_http_requests` | Rows in `request_logs` |
| `details.steps`, `details.mutations` | Actions taken; mutating calls that succeeded or failed with 504 |
| `details.horizon_complete` | Injection horizon reached |
| `details.backend` | `live-http-sqlite` |
| `details.integrity` | Integrity of the final database snapshot taken before the safety check. Use the top-level `integrity` for the episode verdict |

A reward below 1 does not mean the incident was partly solved in any useful
sense. Compare runs on `success`.

## Integrity violation codes

Produced by `assess` in `airline_recovery/live/verification.py` from database rows alone.
Each code is suffixed with the entity it concerns, for example
`duplicate_charge:bkg_…`. Any one of them sets the episode reward to 0.

| Code | Suffix | Trigger |
|---|---|---|
| `oversold` | flight ID | A flight has more seat holds than its capacity, or holds exist for an unknown flight |
| `orphan_charge` | booking ID | A charge refers to a booking that does not exist |
| `duplicate_charge` | booking ID | A booking has more than one charge |
| `incorrect_charge` | booking ID | A booking's single charge differs from its accepted amount |
| `accepted_request_changed` | booking ID | A booking's flight, passenger or amount differs from what the customer request required |
| `rejected_request_fulfilled` | booking ID | A sold-out request that must be refused has a confirmed booking, a charge or a seat hold |
| `unbacked_confirmation` | booking ID | A confirmed booking lacks a seat hold or does not have exactly one charge |
| `invalid_hold` | booking ID | A seat hold has no booking, or its flight or passenger differs from the booking |
| `ineligible_checkin` | booking ID | A check-in exists for a missing or unconfirmed booking, or its flight or passenger differs |
| `preincident_booking_modified` | booking ID | One of the three bookings confirmed before the incident is missing or changed |
| `valid_event_discarded` | event ID | An event that is valid for its booking was quarantined |

An event is valid when its payload is a JSON object with an integer
`schema_version` of 1 or 2, and its `booking_id`, `flight_id` and `passenger_id`
match the booking the outbox row belongs to.

## Cases

Eleven cases (`airline_recovery/live/scenarios.py`; injection in `LiveAirlineEnv._inject`).
The fault families are named here because the source is public. Task IDs,
descriptions, observations and alerts do not reveal them: all eleven tasks carry
the same description.

| Split | Index | Task ID | Kind | Initial fault | Delayed fault |
|---|---:|---|---|---|---|
| train | 0 | `airline-recovery-train-000` | single | Lost payment acknowledgement | |
| train | 1 | `airline-recovery-train-001` | single | Paused check-in consumer | |
| train | 2 | `airline-recovery-train-002` | single | Stale fare cache | |
| train | 3 | `airline-recovery-train-003` | single | Pricing worker stopped | |
| train | 4 | `airline-recovery-train-004` | single | Payment-key migration | |
| eval | 0 | `airline-recovery-eval-000` | single | Malformed queue event | |
| eval | 1 | `airline-recovery-eval-001` | single | Event schema change | |
| eval | 2 | `airline-recovery-eval-002` | compound | Payment-key migration | Stale fare cache |
| test | 0 | `airline-recovery-test-000` | compound | Lost payment acknowledgement | Malformed queue event |
| test | 1 | `airline-recovery-test-001` | compound | Payment worker stopped | Stale fare cache |
| test | 2 | `airline-recovery-test-002` | compound | Payment-key migration | Event schema change |

Fault families:

| Family | What the injector does |
|---|---|
| Lost payment acknowledgement | Sets `payment.provider_latency_ms` to 320–650 and `booking.payment_timeout_ms` to 60–150. Captures commit and return 504, leaving charged `pending` bookings |
| Paused check-in consumer | Sets `checkin.consumer_enabled` to false |
| Stale fare cache | Caches a quote for `F100`, then raises the fare by 1700, 2300 or 3100 cents and bumps its version. New `F100` bookings are rejected as stale |
| Worker stopped | Terminates the `pricing` or `payment` process |
| Payment-key migration | Interrupts 2 to 4 bookings on one flight (`F100` or `F200`). Each was either charged under key version 1, charged under version 2, or never charged because `payment` was down. At least one was charged under the version that is no longer deployed. The deadline is then restored to 200 ms |
| Malformed queue event | Inserts one pending event, with an ordinary event ID, for an existing booking. Its payload is one of three shapes: truncated JSON; valid JSON missing `passenger_id`; valid JSON naming a different booking. `NOT json_valid(payload)` finds only the first |
| Event schema change | Leaves pending events with `schema_version` 2 while `checkin.accepted_schema` is 1 |

## What the seed controls

`reset(seed=...)` seeds one `random.Random` used by the injector.

| Fixed by the seed | Not fixed by the seed |
|---|---|
| Fault parameters: provider latency and booking deadline; the fare increase | Request, booking, charge, event and passenger IDs |
| For payment-key migration: the deployed key version, how many bookings are interrupted (2 to 4), the state of each, and the flight | `episode_id` and trace IDs |
| Which of the three malformed-event shapes appears | Request durations and wall-clock time |
| The step at which a delayed fault arrives (2 to 5) | Worker ports |

Entity IDs are salted with the random `episode_id`. Request and passenger IDs
are UUIDv5 values in a namespace derived from the seed and the episode ID, so
they differ on every reset, including two resets with the same seed. Booking IDs
are derived from the request ID, charge IDs from the booking ID and the charge's
ordinal for that booking, and event IDs from the outbox ordinal and the booking
ID. An agent therefore cannot compute any ID before the episode starts and has
to read them from query results. Within an episode the derivation is public: a
booking's first charge ID can be computed from its booking ID without reading
`charges`, which is what the bundled `source-aware` policy does.

Two resets with the same task and seed produce the same incident with different
identifiers. They are not byte-identical trajectories.

## Example trajectory

The bundled `reference` policy on `train` index 1, seed 1. Reproduce with:

```bash
python -m airline_recovery.live.evaluate --policy reference --split train --index 1 --seeds 1 --output runs/example
```

The reset observation shows one alert,
`Durable event backlog has undelivered records`, with
`confirmed_bookings: 5`, `pending_bookings: 0`, `checkins: 3`,
`outbox_pending: 2`.

| Step | Action | Result |
|---:|---|---|
| 1 | `get_metrics {}` | All five workers running. `checkin` has one 503. Outbox: 3 delivered, 2 pending |
| 2 | `get_logs {}` | The 40 most recent request log rows |
| 3 | `get_config {}` | `checkin.consumer_enabled` is `false`; everything else is unremarkable |
| 4 | `query_sql` for pending bookings joined to their holds and charges | No rows: no booking is stuck |
| 5 | `query_sql` for pending outbox events | Well-formed schema-version-1 events; the first has 5 delivery attempts |
| 6 | `patch_config {"service": "checkin", "values": {"consumer_enabled": true}}` | Consumer re-enabled. This step's traffic window delivers a batch of 10: check-ins go from 3 to 13, 4 events still pending |
| 7 | `replay_events {"limit": 50}` | `attempted: 4, delivered: 4, remaining: 0` |
| 8 | `probe {}` | `healthy: true, verification_windows: 1` |
| 9 | `probe {}` | `healthy: true, verification_windows: 2` |
| 10 | `finish {}` | `terminated: true`, reward 1.0, `success: true` |

Between steps 1 and 5 the backlog grew from 4 to 12 pending events, because
traffic kept confirming two new bookings per action while the consumer was
paused. Steps 6 and 7 are mutating, so the two probes had to come after them.
The final score reported `incident_requests: 14`, `mutations: 2`,
`cost: 0.03` and a safety check with no violations.

IDs in your run will differ from any other run; the sequence of tools and the
counts above are what the seed fixes for this policy.

## Hard tier

The easy tier can be solved by reading the tool descriptions: each one says
what a fault looks like and how to fix it, every fix is safe to apply
everywhere, the ledger is complete, and nothing gets worse while you look. The
hard tier removes those four properties. Payment truth is ambiguous, retries
are not always safe, several states are traps, and waiting has a cost. Cases
are generated from fault pools, so there are hundreds of distinct instances per
level, and a bundled oracle solves every one of them from public evidence
alone.

Select it with `reset(options={"split": ..., "index": ..., "tier": "hard"})`.
Construct the environment with `LiveAirlineEnv(max_steps=None)` to take the
action budget from the case (an explicit `max_steps` still wins). Task IDs are
`airline-recovery-hard-<split>-<index>`; `LiveAirlineEnv.task_manifest(tier="hard")`
lists them.

| Split | Indices | Levels |
|---|---|---|
| `train` | 0–5 | 1, 1, 2, 2, 3, 3 |
| `eval` | 0–2 | 1, 2, 3 |
| `test` | 0–2 | 1, 2, 3 |

The seed is a structural sample: it chooses which faults a case contains, not
only their parameters. Two seeds of the same task are different incidents.

### Levels and budgets

| Level | Composition | Budget |
|---|---|---:|
| hard-1 | One payment or event fault plus one trap | 32 actions |
| hard-2 | A payment fault and an event fault (a pricing fault half the time), two traps, and one delayed event or pricing fault | 36 actions |
| hard-3 | hard-2 plus one or two noise faults, 300–400 historical confirmed bookings in the tables, and a bias towards the circuit breaker | 34 actions |

About one level-1 instance in six, for any level-1 slot and seed, is alert-only:
no fault from the main pools, only decoys. The correct episode there is to inspect, probe twice and finish.
Budgets were calibrated from the oracle's step distribution over 72 episodes.

### What changes in the data model

Tables readable through `query_sql`, in addition to the easy-tier ones:

| Table | Columns | Meaning |
|---|---|---|
| `customer_events` | `event_id`, `request_id`, `kind` (`cancel`), `step` | A customer cancelled their request at that step |
| `refunds` | `refund_id`, `charge_id`, `booking_id`, `amount_cents`, `created_step` | Refunds issued when a booking is voided |
| `fare_holds` | `hold_id`, `flight_id`, `price_cents`, `until_step` | A fare promised for a flight until a step |

Changed columns: `charges` gains `state` (`captured`, `submitted`, `declined` or
`lost`) and `created_step`; `bookings` gains `client_reference` and
`confirmed_at`, and `status` can be `cancelled`. Three further tables exist and
are refused by the SQL authorizer like `service_endpoints`: the episode clock,
the payment provider's ledger and its plan. The local `charges` table records
attempts; the provider's ledger is the truth, and the only ways to learn it are
`provider_lookup` and waiting for settlement.

At level 3, when the log-retention noise fault is drawn, `request_logs` keeps
only the newest 300, 400 or 500 rows.

New configuration fields:

| Service | Field | Type | Bounds | Healthy value | Effect |
|---|---|---|---|---|---|
| `checkin` | `auto_pause_after_attempts` | integer | 0 to 50 | 0 (off) | Circuit breaker: after this many failed deliveries of the head event, the consumer pauses itself and logs `consumer paused by circuit breaker after repeated delivery failure` |
| `payment` | `lookup_quota` | integer | 0 to 20 | 3 (default; each hard case sets 2–4, more when keys have expired) | Read-only. Successful `provider_lookup` calls allowed per episode |
| `payment` | `idempotency_window_steps` | integer | 1 to 1000 | 1000 | Read-only. A capture key older than this many steps is forgotten; a retry with it is a new charge |

### Payment semantics

A capture at the provider is **captured**, **declined** or **pending**. When a
capture's acknowledgement is lost, the local charge is recorded as `submitted`
and the booking stays `pending`; the provider's outcome may be any of the three.

- A pending outcome settles at a seeded step. The environment settles the
  provider's ledger once per step, before your action executes; a `submitted`
  charge then takes the provider's final state.
- Retrying a key whose outcome is still pending returns 504 and writes nothing.
  Retrying a declined key is a legal fresh attempt. Retrying a captured key
  returns the existing charge.
- A key older than `idempotency_window_steps` is forgotten. Retrying it makes a
  second charge.
- Restarting the payment worker marks every `submitted` charge `lost` locally.
  The provider still knows the truth; `provider_lookup` still reveals it. The
  worker reports `health: degraded` when more than 30% of its last 50 requests
  were 504s, which is a symptom, not a reason to restart.
- Voiding a booking refunds each provider-captured charge in full. A booking
  whose outcome is still pending cannot be voided (409).
- A live fare hold makes the held price the valid quote for that flight;
  `validate_price` accepts either the flight record or a live hold. Invalidating
  the whole pricing cache breaks a held fare; invalidate the one flight that is
  stale instead.
- Adopting a charge (`existing_charge_id`) requires it to be captured at the
  provider, the only captured charge for the booking, for the accepted amount,
  with a seat hold.

### Tools

The hard tier has the easy tools plus two, with the same argument validation.
Descriptions state what each call does to which rows and nothing about when to
use it.

| Tool | Arguments | Mutating | Behaviour |
|---|---|---|---|
| `provider_lookup` | `idempotency_key` (required, 1 to 200 characters) | no | Returns `{idempotency_key, state, booking_id, amount_cents}` from the provider. Each successful call uses one unit of `payment.lookup_quota`; past the quota the call is rejected with `provider lookup quota exhausted`. An unknown key fails without using quota |
| `void_booking` | `booking_id` (required) | yes | Cancels a pending booking, releases its seat and refunds captured charges. Returns `{booking_id, status, refunded_cents}`. 409 for a confirmed booking or an unresolved payment |
| `invalidate_cache` | `service` (required), `flight_id` (optional, 1 to 20 characters) | yes | With `flight_id`, evicts only that flight's quote |
| `get_logs` | `service` (required on the hard tier) | no | A call without `service` is rejected with `service is required` |

### Reset observation

The hard reset observation has the easy fields plus `tier` (`"hard"`) and
`level` (1 to 3). `summary` adds `cancelled_bookings`, `fare_holds_active` and
`provider_lookups_remaining`. `episode_contract` has these keys: `max_actions`,
`verification_eligible_from_step`, `required_healthy_probes`,
`step_numbering`, `mutating_tools`, `verification_reset_rule`, `traffic_rule`,
`cost_rule`, `provider_lookup_quota`, `graded_invariants` (every violation code
with a one-line meaning), `telemetry_rule`, `success_conditions`,
`post_finish_safety_check` and `reward`. The mission ends with
`Budget: N actions.`

Alerts are the structural ones from the easy tier plus, in some cases, one
misleading alert. The contract says so: alerts, logs and metrics are
best-effort and may be stale or wrong; durable business rows and the payment
provider are authoritative. A log line can lie; a 503 burst can come from a
worker that is running; an alert can recommend restarting a payment worker that
holds in-flight captures.

### Episode rules

Each hard step runs in this order: the clock advances; fare holds older than
the clock expire; the provider settles; your action is validated and executed;
a delayed fault arrives if this is its step (2 to 5); traffic runs; then
abandonment is applied; then, if the episode is over, it is scored.

- **Abandonment.** A customer request that was never accepted and has been
  retried four times is given up. It leaves the required set and is counted in
  `abandoned_requests`; it still counts in `incident_requests`.
- **Settlement horizon.** The injection horizon is complete only at step 6 or
  later, after any delayed fault, and once no payment is still `pending` at the
  provider. A `provider_lookup` that answers `pending` means the horizon is not
  yet complete. Only the provider decides this: a local charge can stay
  `submitted` after the provider has answered. Finishing before the horizon
  completes scores 0, even with everything else in order.
- **Traps.** A booking whose customer cancelled must be voided, not completed. A
  customer who sent two requests for the same trip has one `client_reference`
  on both bookings; only one may be confirmed. A booking that was voided
  although its customer never cancelled stays incomplete, so voiding everything
  never succeeds.
- **Post-finish safety check.** Three hazards, chosen by the seed from: lost
  acknowledgement then retry, fare change, cancel then retry, retry with an
  expired key, duplicate event delivery.

### Success and reward

Success requires: every required request resolved (confirmed with a seat hold,
exactly one provider-captured charge, a check-in and a delivered valid event;
or cancelled with a matching cancel event and, if it was charged, a refund); no
violation at any point including the safety check; availability 1 over the last
three windows; two counted probes since the last reset; the settlement horizon
complete; and `finish` within budget.

```text
reward = 0.2 * availability + 0.45 * incident_recovery + 0.15 * verified
         + 0.10 * revenue_retained + 0.10 * success - cost_penalty
revenue_retained = 1 - abandoned_requests / max(1, incident_requests)
cost_penalty = min(0.25, 0.004 * reads + 0.02 * mutations + 0.03 * provider_lookups)
```

It is 0 after any violation or before the horizon, and at most 1.0. The success
term means a verified, finished recovery always outscores the same work left
unfinished; pass rates still use `success` itself. `reads` are successful non-mutating tool calls
other than `probe`, `finish` and `provider_lookup` (lookups are priced separately). Score `details` adds `tier`, `level`, `budget`,
`provider_lookups`, `abandoned_requests`, `revenue_retained`, `cost_penalty`,
`settlement_horizon_step` and `trap_outcomes` (`cancelled_voided`,
`cancelled_completed`, `duplicate_voided`, `duplicate_completed`,
`unknown_resolved_by_lookup`, `unknown_resolved_by_settlement`,
`payment_restarted_with_inflight`, `fare_hold_broken`), which describe what
happened to each trap and are not part of the reward.

### Hard-tier violation codes

In addition to the easy-tier codes:

| Code | Suffix | Trigger |
|---|---|---|
| `cancelled_request_fulfilled` | booking ID | A booking was confirmed although its customer had cancelled at or before confirmation |
| `duplicate_sale` | client reference | Two or more confirmed bookings share a `client_reference` |
| `unfunded_confirmation` | booking ID | A confirmed booking does not have exactly one provider-captured charge |
| `refund_missing` | booking ID | A cancelled booking has a captured charge and no refund of equal amount |
| `refund_unwarranted` | booking ID | A refund exists for a booking that is not cancelled |
| `cancelled_booking_backed` | booking ID | A cancelled booking still has a seat hold or a check-in |

`duplicate_charge` counts provider-captured charges when the provider has a
record of the booking. A booking accepted at a fare other than its live hold
trips the existing `accepted_request_changed`.

### Generator pools

A case draws at most one fault from each of pools A to D; E and F modify it.

| Pool | Faults |
|---|---|
| A payment | `lost-ack-mixed` (4–10 bookings with lost acknowledgements whose outcomes are a mix of captured, declined and pending); `key-migration-live` (both key prefixes in use, at least one declined); `payment-degraded-inflight` (a latency storm with in-flight captures and an alert that recommends a restart); `expired-key` modifier (the chosen keys are backdated past the idempotency window, so they are expired at reset and stay expired; the window itself exceeds the budget, so no other key expires during the episode) |
| B events | `poison` (three malformed shapes plus one valid-looking decoy); `schema-mixed` (schema 1 and 2 interleaved); `breaker-paused` (circuit breaker armed with a poison head event) |
| C pricing | `stale-cache`; `stale-cache+fare-hold` (a hold on the other flight, so only a scoped invalidation is safe) |
| D process | `pricing-down`; `inventory-down-then-up` (pending bookings without seat holds); `checkin-down` |
| E traps | `cancelled-pending` (1–3 cancellations at reset or at the delayed step); `duplicate-client-reference`; `restart-bait` (with a degraded payment worker) |
| F noise (level 3) | `log-retention`; `misleading-alert`; `lying-log-line`; `self-healed-transient` |

Constraints: cancellations need a payment fault; duplicates need a payment or
inventory fault; restart bait needs the degraded payment fault; a fare hold
needs the stale cache; the number of unknown outcomes that settle later than
eight steps before the budget never exceeds the lookup quota. Case generation
is deterministic from `(level, slot, seed)` and independent of the episode's
own random stream, so the structure of a case can be reproduced without
starting workers. As on the easy tier, nothing observable names the fault.

The pool names appear here because the source is public; agents see rows,
logs and alerts, never these labels.
