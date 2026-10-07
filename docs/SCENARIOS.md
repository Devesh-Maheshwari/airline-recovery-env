# Hard-tier scenarios

This page covers the hard tier of version 0.5.0. For every fault and trap in
the generator it gives the real failure it stands for, what a correct recovery
must do, which graded invariants catch a wrong recovery, and what the
simulation simplifies. It also shows what the 12 task slots contain and how
much they vary beyond the seed. Where this page and the code disagree, the code
is right. The relevant files are `airline_recovery/live/hardcases.py`
(generator), `hard_injector.py` (fault execution), `worker.py` (service
behaviour), `verification.py` (integrity codes) and `environment.py` (episode
rules). The mechanics are specified in the
[environment reference](ENVIRONMENT.md#hard-tier) and the
[hard tier specification](HARD_TIER_SPEC.md).

## 1. Summary

A **task slot** is a `(split, index)` pair, such as `train` 2, whose task ID is
`airline-recovery-hard-train-002`. There are 12 slots: six in `train` (levels
1, 1, 2, 2, 3, 3) and three each in `eval` and `test` (levels 1, 2, 3), so four
slots per level. The **level** sets the action budget (32, 36 or 34) and the
rules for how many faults, traps and decoys are combined. The **seed** is
passed to `generate_case(level, slot, seed, split)`. It picks which members of
the fault pools are combined and draws their parameters: cohort sizes,
provider outcomes, settlement steps, latencies, fare changes, the step at which
the delayed fault lands, and the lookup quota. Booking, charge and event IDs
are also salted per episode. An **episode** is one run of one slot with one
seed. Each coding agent was measured on 60 episodes (12 slots × seeds 1–5). The
oracle ran 72 (12 slots × seeds 1–6) and each scripted control 36 (12 slots ×
seeds 1–3). The pools hold 11 fault kinds (payment 3, events 3, pricing 2,
process 3), one expired-key modifier on the payment faults, 3 traps and 4
telemetry decoys. That makes 18 named kinds plus one modifier, and several of
them share an underlying mechanism. Counting only which kinds are combined,
there are 17 possible case structures at level 1 (4 of them alert-only), 600 at
level 2 and 6,000 at level 3, about 6,600 in all. Counting the expired-key
modifier and the timing of cancellations as well gives about 23,800 (24, 2,160
and 21,600). These counts are derived from the pool rules. A sample of 200,000
seeds per level found 17, 600 and 5,873 of them. The 60 agent episodes contain
50 distinct kind-level structures. The 72 oracle episodes contain 59. Slots at
the same level draw from the same distribution, and only their random stream
differs. **Seeds vary parameters and combinations within the same mechanisms,
so more seeds are not more mechanisms.** Every hard-tier result in this
repository comes from the 18 kinds and one modifier listed in section 2.

To see the structure of any case without starting the services:

```python
from airline_recovery.live import hardcases
case = hardcases.generate_case(level=2, slot=2, seed=1, split="train")
print([f.kind for f in case.initial], [f.kind for f in case.delayed], [t.kind for t in case.traps])
```

## 2. Fault mechanisms and traps

Agents never see these names. They see rows, logs, alerts and HTTP responses.
A case has at most one fault from each of pools A to D. Pool E holds the traps.
Pool F holds decoys, which appear at level 3 and in the alert-only level-1
instances. The graded invariants are the 17 codes in `GRADED_INVARIANTS`
(`environment.py`), and the
[violation code tables](ENVIRONMENT.md#hard-tier-violation-codes) explain them.
A code fires at any step, the post-finish check included, and it permanently
sets the reward to 0. "No code" in the table means a wrong recovery only fails
`success`, through incomplete requests, failing traffic, missing verification
or an incomplete settlement horizon. The last column counts how many of the 60
agent episodes and the 72 oracle episodes contain the kind, either initially or
as the delayed fault.

| Kind (pool) | What breaks in the simulated system | Real-world failure it represents | What a correct recovery must do | What catches a wrong recovery | Simplified relative to production | Episodes (60 / 72) |
|---|---|---|---|---|---|---|
| `lost-ack-mixed` (A) | 4–10 new bookings get HTTP 504 after the provider has already decided. The outcomes are a mix of captured, declined and pending, with at least one of each. Local charges show `submitted` and the bookings stay `pending`. | A payment is captured or declined, but the response or webhook never reaches the merchant. A PSP call times out with an unknown outcome, and some payments are still processing. | Learn each outcome before acting. Either re-present the live key or use `provider_lookup`. A re-presented key confirms a captured charge without charging again, makes a fresh capture after a decline, and is refused without any write while the outcome is pending. Wait for pending outcomes. Finish only when nothing is pending at the provider. | On its own, nothing: with a live key a default retry gets the provider's outcome. Finishing while an outcome is pending fails the horizon (score 0, no code). | The injector plans the outcomes. A pending outcome settles at a seeded step between 4 and budget − 8. There is no webhook or notification path: the truth comes only from a lookup, a re-presented key or settlement. | 13 / 14 |
| `key-migration-live` (A) | 3–5 bookings were interrupted around a change of `booking.payment_key_version`. Some were captured under `booking:<id>` and some under `booking-v2:<id>`, and at least one was declined. Any booking sent while payment was down has no charge. | Idempotency key misuse. The key format changed in a deploy while requests were in flight, so a retry computes a different key and the provider takes a second payment. | Adopt a captured charge (`existing_charge_id`) or retry its exact key. Retry a declined key. Use a default retry only for bookings with no charge. | `duplicate_charge` and `unfunded_confirmation` when a default retry captures again under the deployed prefix. | There are two fixed key formats, the deployed version is a readable setting, and a key is just a prefix plus the booking ID. | 23 / 26 |
| `payment-degraded-inflight` (A) | Provider latency (320–650 ms) is set above the booking deadline (60–150 ms), so every capture commits and then returns 504. Two to four captures are pending at the provider. The fault also adds 18–30 more 504 log rows. Payment `health` reads `degraded`, and a critical alert recommends a restart. | A PSP latency spike longer than the client timeout. Payments succeed but the callers record failures, and alerts push operators to restart the payment service. | Raise `booking.payment_timeout_ms` above `payment.provider_latency_ms`, which is read-only. Keep idempotency on. Wait for the pending outcomes. Do not restart payment while any charge is `submitted`. | Leaving the deadline low keeps new bookings pending, so traffic and probes fail (no code). Turning idempotency off gives `duplicate_charge` when a lost acknowledgement is retried, by the agent or by the post-finish `lost_ack_retry` hazard. | Latency and deadline are configuration numbers. No time passes and no socket times out: the 504 is returned after the commit. | 13 / 18 |
| expired-key modifier (`expired_count` on an A fault; 40% of cases with one) | Between one key and half of the incident's charged keys are backdated past `payment.idempotency_window_steps`. The window is longer than the budget, so no other key expires. The lookup quota is raised to at least the number of expired keys plus one. | A retry after the provider has pruned the idempotency key, which the provider treats as a new payment. [Stripe][stripe-idem] documents that keys may be removed once they are at least 24 hours old, and that reusing a pruned key makes a new request. | Spot it by comparing `charges.created_step` with the window. Do not retry the key. Use `provider_lookup`, then adopt a captured charge, retry only a declined one and wait out a pending one. | `duplicate_charge` and `unfunded_confirmation`. | Expiry comes from backdating rows at reset, not from elapsed time. The window is counted in steps. | 17 / 20 |
| `poison` (B) | Four events are added to the outbox: truncated JSON, a payload missing a field, a payload naming a foreign booking, and a valid but oddly formatted event (the decoy). The first of the four is always malformed. Delivery runs in event order and stops at the first rejection, so check-ins stall. | A poison message blocks an ordered queue or outbox relay ([transactional outbox][outbox]). | Quarantine exactly the three malformed events, keep the decoy, then replay. | `valid_event_discarded` if the decoy or any other valid event is quarantined. A blocked head leaves requests incomplete (no code). | One dispatcher, strict ordering, no dead-letter queue. Validity is a fixed public predicate. | 21 / 26 |
| `schema-mixed` (B) | The consumer is paused and 3–5 bookings are made. Every other pending event is rewritten to `schema_version` 2, and the consumer resumes accepting only version 1. The first version-2 event blocks the queue. | A producer moved to a new event schema before the consumer was upgraded. | Set `checkin.accepted_schema` to 2, which accepts both versions, then replay. | `valid_event_discarded` if version-2 events are quarantined. | There are only two versions, and they carry the same fields. | 17 / 21 |
| `breaker-paused` (B) | `checkin.auto_pause_after_attempts` is set to 3. A malformed head event has already failed three times, so the consumer has paused itself and logged that. | A consumer circuit breaker tripped by a poison message. Re-enabling the consumer without removing the cause trips it again ([circuit breaker][breaker]). | Quarantine the malformed head, then re-enable the consumer and replay. | Re-enabling the consumer alone pauses it again at the next failure (no code). `valid_event_discarded` if valid events are quarantined. | The breaker is only open or closed. There is no half-open trial, and the consumer stays paused until someone re-enables it. | 31 / 35 |
| `stale-cache` (C) | One flight's quote is cached. Then its fare rises by 1,700, 2,300 or 3,100 cents and its version goes up by one. New bookings on that flight are refused as stale (409), and customers retry and give up after four tries. | A stale price cache serves a fare that no longer matches the fare system. | Evict only that flight's quote (`invalidate_cache` with `flight_id`). | `accepted_request_changed` if price validation is switched off and bookings are taken at the stale fare. The post-finish `fare_change` hazard catches this too. If the fault is never fixed, abandoned requests lower `revenue_retained`. | One cache table keyed by flight. Staleness is a version mismatch that the agent can read. | 20 / 24 |
| `stale-cache+fare-hold` (C) | Everything in `stale-cache`, plus a fare hold on the other flight. That flight's list fare rises by 1,200, 1,800 or 2,600 cents, while a `fare_holds` row and a cache row keep the old price until after the budget. | A price promised to customers for a set period (a fare lock), lost when a cache is flushed. | Evict only the stale flight's quote. Leave the held flight's quote and the cache setting alone. | `accepted_request_changed` on every booking taken on the held flight at the list fare. This happens after evicting all quotes, evicting the held flight's quote, or turning the cache off. | The held price lives only in the cache row, because the quote path never reads `fare_holds`. Once that row is deleted, the hold cannot be restored in that episode. | 21 / 28 |
| `pricing-down` (D) | The pricing worker is stopped (after any stale quote has been cached). New bookings fail without a quote. | A crashed dependency. | Restart pricing. | No code. Failing traffic blocks verification, and customers abandon their requests. | A restart always works. There are no crash loops or partial failures. | 3 / 4 |
| `inventory-down-then-up` (D) | Inventory is stopped while 2–3 requests arrive, then restarted. Those bookings exist as `pending` with no seat and no charge. | A multi-step booking (a saga) interrupted by a dependency outage, leaving half-created orders. | A default `reconcile_booking` for each one, which reserves the seat and captures. | Voiding them leaves required requests incomplete (no code). | The outage is over before the agent's first action. | 4 / 4 |
| `checkin-down` (D) | The check-in worker is stopped and the event backlog grows. | A queue consumer is down and a backlog builds. | Restart check-in, then replay. | `valid_event_discarded` if backlog events are quarantined. | Same as `pricing-down`. | 4 / 8 |
| `cancelled-pending` (E) | A customer cancel event is written for 1–3 pending required bookings. It arrives at reset or, at levels 2–3 in half the cases, at the delayed step. | A customer cancels while a recovery or retry job is still working on the order: the cancellation races the retry. | `void_booking`, which refunds every provider-captured charge. The void is refused (409) while the outcome is pending at the provider, so wait for settlement and void then. | `cancelled_request_fulfilled` if the booking is confirmed after the cancel. `refund_missing` and `cancelled_booking_backed` check what the void left behind. | A cancellation is always honoured with a full refund. There are no fees, fare rules or partial refunds, and the cancellation is just a row with a step number. | 43 / 49 |
| `duplicate-client-reference` (E) | One customer's trip exists as two requests and two bookings with the same `client_reference`. Either the older one is confirmed and the newer pending, or both are pending with captured charges. Only the older request is a required outcome. | One purchase submitted twice under two request IDs, for example a double click or an app retrying without an idempotency key. | Void the newer booking, which refunds its capture, and complete the older one. | `duplicate_sale` if both are confirmed. Voiding the older one leaves a required request incomplete (no code). | The duplicates share an explicit reference. Keeping the older booking is a fixed rule that the grader enforces. | 40 / 48 |
| `restart-bait` (E) | Appears only with `payment-degraded-inflight`. It adds two 500 log rows saying that a process restart is required, plus 6–12 more 504 rows. | Logs and alerts that urge a restart of a service holding in-flight work. The restart throws away in-memory state. | Leave payment running and fix the deadline. | No code. Restarting payment marks every `submitted` charge `lost`. Lost charges never settle locally, so the outcome must then be learned by a lookup or a re-presented key. The restart is recorded as `trap_outcomes.payment_restarted_with_inflight`. | The only state lost is the local `submitted` marker. The provider's record survives. | 7 / 10 |
| `log-retention` (F) | An insert trigger keeps only the newest 300, 400 or 500 `request_logs` rows, so older evidence disappears as traffic runs. | Log retention or rotation discarding the evidence of an incident. | Rely on the durable tables. | No code. | Only request logs are trimmed. | 10 / 11 |
| `misleading-alert` (F) | One wrong alert, from four fixed texts: evict every pricing quote, quarantine the pending backlog, turn off `enforce_capacity`, or turn off payment idempotency. With the degraded-payment fault, that fault's restart alert takes the single alert slot instead. | Stale or wrong alert and runbook advice. | Ignore it and act on durable rows. | If the advice is followed: `accepted_request_changed` (when a hold exists), `valid_event_discarded`, `oversold` and `rejected_request_fulfilled`, or `duplicate_charge` (in the post-finish check). | Five fixed alert texts, the payment restart alert included. | 10 / 10 |
| `lying-log-line` (F) | One fabricated log row, from three fixed lies: a successful void, a successful refund, or a sold-out reservation. | A misleading log line. | Check the durable rows: no cancelled booking, no refund row, seats still available. | No code. | Three fixed lies. | 10 / 12 |
| `self-healed-transient` (F) | 8–15 fake 503 rows for inventory, check-in or pricing, while that worker is running. | An error burst left behind by a transient failure that has already recovered. | Check `running` in the metrics and leave the worker alone. | No code. A needless restart is a mutation, so it costs reward and resets verification. | One fixed message. | 9 / 12 |
| alert-only instance (level 1) | About one level-1 case in six. Nothing is broken, and one to three decoys appear, always including a misleading alert. | A false alarm. | Inspect, probe twice from step 6, then finish. | Whatever the followed advice breaks. | Nothing is broken, so only the decoys are tested. | 4 / 4 |

**Post-finish hazards.** Once every other success condition holds, trusted
traffic replays three hazards against the settings the agent left deployed.
The seed picks the three from these five:

- `lost_ack_retry`: a lost acknowledgement, then the customer's retry.
- `fare_change`: a cached quote, then a fare rise, then a new booking.
- `cancel_then_retry`: a lost-acknowledgement booking is cancelled and voided,
  then the same customer books again under a new request.
- `expired_key_retry`: a declined capture whose key is backdated past the
  window, then retried.
- `duplicate_event_delivery`: an already-delivered check-in event is sent
  again.

Only an integrity code from this traffic fails the episode.

**External practice referred to.** Each of these pages was read while writing
this document:

- [Stripe idempotent requests][stripe-idem]. Stripe saves the first result for
  a key and replays it to later requests with that key, errors included. A
  reused key with different parameters is an error. Keys may be pruned after
  24 hours, and a reused pruned key is processed as a new request.
- [Stripe webhooks][stripe-webhooks]. In live mode, undelivered events are
  retried for up to three days. An endpoint can receive the same event more
  than once, and delivery order is not guaranteed.
- [Transactional outbox][outbox]. The relay can publish a message more than
  once if it crashes before recording completion, so consumers must be
  idempotent.
- [Circuit breaker][breaker]. The breaker trips after a failure threshold, has
  a half-open trial state, and should raise an alert when it trips.

## 3. The 12 task slots

The slot fixes only the level. The level decides what the generator combines:

| Level | Slots | Budget | What the generator combines |
|---|---|---:|---|
| 1 | `train` 0, `train` 1, `eval` 0, `test` 0 | 32 | One case in six is alert-only. Every other case gets one fault: payment 45%, events 35%, process 20%. A payment fault always comes with one trap: a cancellation or a duplicate, or restart bait, which is possible only with degraded payment. The inventory outage comes with the duplicate trap. Event faults and the pricing and check-in outages come with no trap. Cancellations always arrive at reset. |
| 2 | `train` 2, `train` 3, `eval` 1, `test` 1 | 36 | One payment fault and one event fault, plus a pricing fault 50% of the time and a process fault 30% of the time. Two traps. One delayed event or pricing fault of a kind not already drawn, arriving at step 2–5. |
| 3 | `train` 4, `train` 5, `eval` 2, `test` 2 | 34 | Everything in level 2, plus one or two decoys and 300–400 historical confirmed bookings. The initial event fault is the circuit breaker about three times in four. |

Two rules apply at every level. The expired-key modifier applies to 40% of the
cases that have a payment fault. The lookup quota is 2–4, raised when keys have
expired. By the code, about 43% of level-1 instances contain a trap, 40% have a
single fault and no trap, and 17% are alert-only. The shorter level-1
description in `ENVIRONMENT.md` ("one payment or event fault plus one trap")
covers neither the process faults nor the cases without a trap.

Here is what each slot drew on seeds 1–6. The agents ran seeds 1–5, and the
oracle ran all six.

Legend. Payment: `LA` lost-ack-mixed, `KM` key-migration-live, `PD`
payment-degraded-inflight, `+x` expired keys. Events: `PO` poison, `SM`
schema-mixed, `BR` breaker-paused. Pricing: `SC` stale-cache, `FH`
stale-cache+fare-hold. Process: `PR` pricing-down, `IN`
inventory-down-then-up, `CK` checkin-down. `→` marks the delayed fault. Traps:
`CX` cancelled-pending (`CX*` arrives at the delayed step), `DU`
duplicate-client-reference, `RB` restart-bait. Decoys: `LR` log-retention, `MA`
misleading-alert, `LL` lying-log-line, `SH` self-healed-transient.

| Split | Index | Level | Budget | Seed 1 | Seed 2 | Seed 3 | Seed 4 | Seed 5 | Seed 6 (oracle only) |
|---|---:|---:|---:|---|---|---|---|---|---|
| `train` | 0 | 1 | 32 | KM · DU | KM+x · CX | alert-only · SH MA | KM · CX | PD · CX | PO |
| `train` | 1 | 1 | 32 | alert-only · MA | alert-only · LL MA | BR | IN · DU | alert-only · MA LL | CK |
| `train` | 2 | 2 | 36 | PD BR CK →FH · DU CX* | LA+x PO SC →FH · CX DU | LA SM SC IN →BR · CX* DU | LA+x PO SC →BR · DU CX* | PD PO SC →SM · RB DU | PD PO FH CK →SC · RB DU |
| `train` | 3 | 2 | 36 | LA PO →SC · DU CX | KM+x PO FH PR →BR · CX DU | KM+x BR FH →SC · CX DU | KM BR FH →PO · DU CX* | KM+x PO →BR · DU CX | PD SM FH PR →SC · DU RB |
| `train` | 4 | 3 | 34 | PD+x SM →FH · RB DU · MA LR | PD+x PO →BR · DU CX* · LR | PD BR FH IN →SM · RB CX · LL LR | KM BR SC IN →SM · CX* DU · MA SH | LA+x BR SC →PO · CX DU · MA SH | KM SM →SC · CX* DU · LL SH |
| `train` | 5 | 3 | 34 | KM BR →PO · DU CX · LR | KM BR SC CK →FH · CX DU · LL | KM+x BR SC →FH · DU CX · LL | PD BR FH →SM · DU CX · LR | KM SM SC →FH · DU CX* · MA SH | PD BR SC →FH · DU CX* · SH LL |
| `eval` | 0 | 1 | 32 | LA · CX | PO | PD · CX | BR | PR | PD+x · RB |
| `eval` | 1 | 2 | 36 | KM PO SC →FH · CX DU | LA+x SM →PO · DU CX* | KM BR FH →SM · CX* DU | LA BR CK →FH · DU CX* | KM BR →SM · DU CX | KM PO FH →SM · DU CX |
| `eval` | 2 | 3 | 34 | PD PO SC →FH · DU RB · SH LR | KM BR SC →SM · DU CX* · LR | KM BR SC CK →FH · DU CX* · LL MA | KM BR FH →SM · DU CX* · SH | KM+x BR SC →PO · CX* DU · SH LL | PD BR FH →PO · CX DU · SH |
| `test` | 0 | 1 | 32 | PD+x · CX | BR | LA · DU | LA · DU | BR | CK |
| `test` | 1 | 2 | 36 | LA PO SC →BR · DU CX* | LA+x PO →SM · CX DU | PD SM →PO · RB CX | KM+x SM →FH · DU CX | LA+x PO SC →FH · CX* DU | KM+x BR FH →PO · DU CX |
| `test` | 2 | 3 | 34 | KM BR →SM · DU CX · LR LL | KM+x BR →FH · CX DU · LL MA | PD BR FH PR →SM · CX RB · LL SH | PD PO →SC · RB CX* · SH LR | KM BR SC →PO · CX DU · LR | LA+x BR FH CK →SM · CX DU · LR |

How a seed varies a slot:

- **Which kinds appear.** This is the visible difference between cells in a
  row. All 18 kinds are reused across slots, and the `eval` and `test` slots
  draw from exactly the same pools and rules as `train`.
- **Parameters within a kind.** Each kind has its own draws. For
  `lost-ack-mixed`: cohort size and outcome mix. For pending captures: when
  they settle, and to what. For degraded payment: latency and deadline. For the
  pricing faults: the fare deltas and which flight is hit. Then the number of
  cancellations (1–3), the duplicate's shape, and which malformed shape heads
  the breaker queue. The lookup quota, the delayed step (2–5), the number of
  historical rows and three of five post-finish hazards are drawn too.
- **Identities.** The episode salts booking, charge, request and event IDs,
  so two runs of the same seed share a structure but not their IDs.

Read the 60-episode results as 20 draws from each of three level
distributions, not as 60 different problems. Mechanisms also appear unevenly
in that sample. `pricing-down` appears in 3 of the 60 episodes, `restart-bait`
in 7 and `cancelled-pending` in 43.

## 4. Observed agent failures by mechanism

**Source.** Per-episode scores and violation codes come from
`evidence/v0.5.0/hard/agents/<agent>/episodes.jsonl`, where `<agent>` is
`codex-gpt-6-astra`, `claude-sonnet-5-5` or `claude-haiku-4-5`. Each episode's
action list is in the `actions/` folder beside it. Each episode's case structure was
regenerated with `generate_case`. The provenance files of those runs record
the same SHA-256 for `hardcases.py` and `hard_injector.py` as the shipped
source, so these are the structures the agents faced. The agent runs predate
later fixes to `environment.py` and `verification.py` (see the
[changelog](../CHANGELOG.md)). The only grading-rule change the changelog
lists is that a refund on a still-pending booking is no longer
`refund_unwarranted`, and no agent episode triggered that code. Two Haiku
episodes ended without a receipt and have no score details.

Each cell gives the number of episodes in which the code fired, with the
number of distinct bookings (or client references) flagged in brackets. An
episode counts under every code it triggered; the
[failure analysis](../evidence/v0.5.0/hard/failure-analysis/README.md) instead
gives each failed episode one primary cause, so its per-cause counts differ
slightly. The
mechanism column is **inferred** from two things: which mechanisms the
episode contained, and which actions the agent took on the flagged booking.

| Violation code | Codex gpt-6-astra | Sonnet 5.5 | Haiku 4.5 | Likely source mechanism | Basis for the inference |
|---|---:|---:|---:|---|---|
| `accepted_request_changed` | 13 (276) | 19 (252) | 17 (308) | Codex and Sonnet: likely `stale-cache+fare-hold`, with the hold lost to a disabled cache or a full eviction. Haiku: likely `stale-cache` with price validation turned off (12 episodes) and broken fare holds (5). | All 13 Codex episodes contained a fare hold, and in all 13 Codex turned `pricing.cache_enabled` off. All 19 Sonnet episodes contained a hold and evicted every quote, and in 18 Sonnet also turned the cache off. In 12 Haiku episodes Haiku set `booking.validate_price` to false. In the other 5, Haiku evicted every quote while a hold existed. |
| `cancelled_request_fulfilled` | 0 | 35 (54) | 27 (48) | `cancelled-pending` | Present in every flagged episode. Every flagged booking was one the agent itself reconciled, mostly with a default retry (Sonnet 39 of 54, Haiku 30 of 48). |
| `duplicate_sale` | 0 | 10 | 27 | `duplicate-client-reference` | Present in every flagged episode, in both shapes. |
| `duplicate_charge` with `unfunded_confirmation` on the same bookings | 0 | 6 (7) | 9 (13) | Likely the expired-key modifier (Sonnet 6 of 6, Haiku 6 of 9). Likely `key-migration-live` without expiry (Haiku 2). A misleading alert on an alert-only instance (Haiku 1). | In the first two groups, the agent retried the flagged booking after its key had expired, or retried it under the deployed key prefix. In the last episode, Haiku turned payment idempotency off as the alert advised, and the post-finish `lost_ack_retry` hazard then charged a synthetic booking twice. |
| `oversold` with `rejected_request_fulfilled` | 0 | 0 | 2 (2 and 62) | `misleading-alert` (turn off `enforce_capacity`) | Both episodes showed that alert, and in both Haiku turned capacity enforcement off. From then on, requests on the sold-out canary flight were given seats. |
| Episodes with any code | 13 | 41 | 42 | | |

The other failures had no integrity code: 1 for Codex, 5 for Sonnet, and 6 for
Haiku plus the 2 episodes without a receipt. In the 12 that were scored, the
episode ended without two counted healthy probes, and 11 of them also left a
required request incomplete. None failed only because of the settlement
horizon.

Some trap outcomes are recorded but not graded. Payment was restarted while
captures were in flight in 15 Haiku episodes, 2 Sonnet episodes and no Codex
episode. `trap_outcomes.fare_hold_broken` is set only by `invalidate_cache`.
It misses holds broken by turning the cache off, so it under-counts the Codex
failures above (2 flagged out of 13).

The scripted controls (seeds 1–3, 36 episodes each) fail with the same codes.
For every control except `nop`, `cancelled_request_fulfilled` fires in 23–26
episodes and `duplicate_sale` in 23. `duplicate_charge` fires in 2–13, and
`accepted_request_changed` in 11–14 for `blanket`, `blanket-hard` and
`source-aware`. The oracle has no code in 72 of 72 episodes.

## 5. Simplifications and what they mean for validity

| Simplification | What the code does | What it means for validity |
|---|---|---|
| Single currency, capture only | Amounts are integer cents with no currency field. A payment is one capture, with no separate authorisation. Refunds are always for the full amount. There are no fees, partial captures, chargebacks or currency conversion. | The tier tests "exactly one capture per sale, refunded if cancelled". It says nothing about partial refunds, amount reconciliation or multi-currency handling. |
| Synthetic payment provider | The provider ledger lives in the same SQLite file. The injector plans outcomes before the request is sent. A capture has three states (captured, declined, pending). The truth comes from a rationed `provider_lookup` (2–4 calls, more when keys have expired), from re-presenting a live key, or from settlement at a seeded step. There are no webhooks. | Choosing between waiting and looking up is a priced decision inside legible rules, not a model of a real PSP. A real PSP would usually push a webhook, retried for days and possibly duplicated ([Stripe webhooks][stripe-webhooks]). |
| Simulated idempotency rules | With idempotency on, a booking's key is a prefix plus its booking ID. A declined attempt frees its key for a fresh capture under the same key. A key reused with a different booking or amount gets a 409. Keys expire by step count, and only when backdated at reset. | These rules belong to this provider. Stripe's documented layer replays the first result for a key, errors included, and prunes keys after 24 hours ([Stripe idempotency][stripe-idem]). An agent following one real provider's semantics could reasonably act differently. |
| Deterministic time | The clock is the action counter. Settlement steps, the delayed fault (step 2–5) and the horizon are all step-based. One traffic window runs per action, and customers give up after exactly four retries. | The tier measures sequencing under an action budget, not speed or behaviour under real concurrency. |
| No real airline rules | There are no fare classes, fare rules, cancellation fees, refund policies, rebooking, schedule changes, ticketing or overbooking. A cancel event always means void plus a full refund. A fare hold is honoured exactly until its step. Between duplicates, the older booking always stays. | The correct outcome encodes one convention per situation. A domain expert might choose differently in some cases (see section 6). |
| Fare hold stored in the cache | The quote path reads the cache and the flight row but never `fare_holds`. Turning the cache off hides the held price. Deleting its cache row loses it for the rest of the episode. Booking validation does honour a live hold. | The hold is visible as a `fare_holds` row and as `fare_holds_active` in the summary, so the trap can be avoided. In many production designs, though, bypassing or flushing a cache would not drop a price guarantee. All 13 Codex integrity failures and all 19 Sonnet `accepted_request_changed` episodes involve this design. |
| One database, five services | Five HTTP worker processes (pricing, inventory, payment, booking, check-in) share one SQLite file in WAL mode. There are no per-service databases, replicas or network partitions. | Every inconsistency can be seen with one SQL query, which is easier than diagnosis across real service boundaries. |
| Outbox and consumer | A single dispatcher delivers in strict event order and stops at the first rejection. There is no dead-letter queue. The consumer is idempotent: a repeated event for a checked-in booking is ignored. | Head-of-line blocking is guaranteed, so a poison event is always visible. Real brokers can hide one behind partitions, retries or dead-letter queues. |
| Circuit breaker | The breaker is either open or closed. When open, the consumer stays paused until the agent re-enables it. | This is simpler than the common three-state breaker ([circuit breaker][breaker]). Once the head is fixed, nothing recovers on its own. |
| Restarts | Workers keep no state outside the database. A payment restart only marks `submitted` charges `lost`, and a restart always succeeds. | "Restart loses in-memory state" is modelled as losing one local marker, not as corrupted or partial state. |
| Traffic | Two bookable flights (F100, F200) plus a one-seat sold-out canary (F900). Each action brings retries of unaccepted requests, one new request per flight, one repeat of the first, and one sold-out attempt. Capacity is raised so that valid traffic never sells out. | Load, contention and capacity planning are out of scope. |
| Telemetry decoys | Five alert texts, three fixed lying log lines, fixed 503 bursts and a log-retention trigger. | Anyone who has read the source can recognise every decoy. This tests whether an agent checks durable rows, not resistance to realistic noise. |
| Generator rules | Each case has at most one fault per pool and at most one delayed fault. Slots at the same level are draws from one distribution, and `eval` and `test` use the `train` pools. | "Held out" means a different draw from the same mechanisms, not a different system (see [limitations](LIMITATIONS.md#the-hard-tier)). |
| Grading by row predicates | The grader checks business rows after every step and after the post-finish check. Any action sequence that leaves the rows correct passes, but a code fired at any step cannot be repaired. Payment truth comes from the private provider ledger. | Grading does not depend on the recipe, but it is strict about permanence. A transient wrong state that a real system might later reconcile fails the episode. |
| Public source | The generator, injector, verifier and oracle are all in the repository. | This is not a hidden benchmark, and it makes no claim to resist a policy written with the source open. |

Taken together, the hard tier measures whether an agent establishes payment
truth before retrying, respects cancellations and duplicate requests, avoids
blanket fixes, and verifies its work within a budget, all in one synthetic
system with stated rules. It does not measure knowledge of real airline or
payment policy, and a hand-written procedure (the bundled `oracle`) solves
every recorded case. The [limitations page](LIMITATIONS.md) says what a score
does and does not show.

## 6. Open validation items

None of these has been done.

- **Domain-expert review.** An independent payment and airline-operations
  expert should review the payment, cancellation, refund, fare-hold and
  duplicate rules that define the correct outcomes.
- **Alternative correct solutions.** Test systematically whether the grader
  accepts other defensible recoveries, such as keeping the other duplicate,
  bypassing a cache when no hold exists, or waiting instead of looking up.
- **Human agreement.** Measure whether experienced on-call or payments
  engineers, given the same observations, agree with the grader's verdict
  episode by episode.
- **Independent solver.** Have a solver written without access to the bundled
  oracle confirm that every level can be solved from public evidence alone.

[stripe-idem]: https://docs.stripe.com/api/idempotent_requests
[stripe-webhooks]: https://docs.stripe.com/webhooks
[outbox]: https://microservices.io/patterns/data/transactional-outbox.html
[breaker]: https://martinfowler.com/bliki/CircuitBreaker.html
