# Limitations

This page states what a score on Airline Recovery Env 0.5.0 does and does not show, what the
environment does not model, and how the benchmark's own reviews have gone. The
mechanics referred to here are specified in the
[environment reference](ENVIRONMENT.md).

## What a score means

`success` on an episode means that, in one synthetic incident, an agent using
twelve tools (fourteen on the hard tier) left every customer request completed with exactly one correct
charge, a seat and a check-in, did so without ever creating an inconsistent
record, kept fresh traffic working, and left settings deployed under which a
retried payment and a fare change do no harm.

That is evidence of **transaction-safe tool use on these cases**. It is not
evidence of the following.

- **Deep diagnosis.** On the easy tier, a hand-written rule that reads the ledger
  and applies a fixed set of checks (the bundled `reference` policy) solves every
  case on every measured seed; on the hard tier the bundled `oracle` does. A control that never reads the charge table
  (`source-aware`) solves most of them. Their measured results are in the
  results table of the [README](../README.md). A model that matches `reference`
  has shown it can follow the same procedure, not that it can reason about
  unfamiliar failures.
- **Understanding of the system from evidence alone.** The tool descriptions
  and the `episode_contract` in the reset observation explain the mechanisms an
  agent needs: how the payment key version relates to duplicate charges, which
  schema setting accepts which events, when probes count. Part of the task is
  therefore reading comprehension of those descriptions.
- **Generalisation.** The `eval` and `test` splits use the same services,
  settings, tools and fault families as `train`. Each of the four compound
  cases combines two fault families that also appear on their own in the suite.
  Held-out here means a different case, not a different application.
- **Model capability.** Beyond three coding-agent CLIs measured on a few seeds
  per task (see the README tables), no model has been evaluated or trained here.
- **Production readiness.** Everything is synthetic and local.

A partial reward is not partial success. Compare runs on `success` and on
`tasks_solved_on_every_seed`.

## The hard tier

The [hard tier](ENVIRONMENT.md#hard-tier) exists because the easy tier was being
solved by reading the tool descriptions. It changes four things and leaves
others alone; be clear about which is which before quoting a hard-tier number.

What it does fix:

- **Hints.** Hard tool descriptions say what a call does to which rows, not
  when to use it. The mission states the goal and the budget. Reading
  comprehension of a runbook is no longer most of the task.
- **Universal safety of fixes.** Restarting the payment worker, invalidating
  the whole cache, retrying every pending booking and quarantining every odd
  event each break something on some generated cases. The `blanket-hard`
  control exists to show that.
- **Complete, local truth.** The payment provider's state is private and
  rationed. Some of it can only be learned by waiting, and waiting costs
  budget and loses customers.
- **Hand-authored cases.** Cases are sampled from pools, so a policy has to
  cope with structures it has not seen rather than eleven known incidents.

What it does not fix:

- **The source is still public,** including the generator, the pools and the
  oracle. Anyone can read how every trap is built and how the oracle resolves
  it. The claim is "solvable from public evidence inside the episode", not
  "resistant to a policy written with the source open".
- **A hand-written rule still solves every case.** The `oracle` is required to
  pass 100% of generated instances; a model that matches it has shown it can
  follow that decision table under a budget, which is more than the easy tier
  showed and still not open-ended diagnosis.
- **Same services, same mechanisms.** Levels differ in how many faults, traps
  and decoys are combined and how tight the budget is, not in what the system
  is. Held-out still means a different draw, not a different application.
- **Settlement is step-based.** Provider outcomes settle at a seeded step
  number, lookups are rationed by a quota, and customers abandon after a fixed
  number of retries. These are legible rules chosen to make waiting a decision
  with a price, not a model of a real provider or of real customers.
- **Budgets come from the oracle.** Level budgets (32, 36 and 34 actions) were
  calibrated on 2026-10-03 from the oracle's step distribution over 72
  episodes. Compare hard-tier numbers only against the version that produced them.
- **Model results are a first calibration.** Hard-tier agent results cover a
  few seeds per slot; the README's hard-tier table says exactly what was measured.

## Known limitations

**Eleven hand-authored easy cases.** The easy tier has seven fault families and
eleven cases. This is a small suite. Passing all of it is a low bar and failing part
of it is informative mainly about which mechanism was mishandled.

**Seeds are not independent samples.** A seed changes a handful of fault
parameters: latency and deadline values, the fare increase, how many bookings a
payment migration interrupts and in which state, which malformed-event shape
appears, and when a delayed fault arrives. Seeds of one task are closely related
episodes. A confidence interval over episodes describes that run and no wider
population.

**The source is public.** Case definitions, the injector, the verifier and the
ID derivations are all in the repository. Within an episode, a booking's charge
ID can be computed from its booking ID. This is not a hidden benchmark and makes
no claim to resist contamination.

**Local evaluation is not tamper-evident.** A policy run through
`python -m airline_recovery.live.evaluate` shares an interpreter with the grader and could
read or change anything in it. Such results are marked
`"trust_boundary": "in-process"` and are self-reported. The OpenEnv server and
the Harbor tasks put a process or container boundary between agent and grader.

**Harbor receipts have limits.** The receipt keys in the committed
`live-tasks/` directory are public, so receipts for those tasks can be forged;
generate a private set for results you report. A receipt is bound to a task and
its key, not to one run, so a receipt from an earlier run of the same generated
task verifies again. The sidecar's HTTP port is unauthenticated within the
task's network. See the [threat model](AGENTS.md#threat-model).

**The post-finish safety check covers two hazards** on the easy tier (a retried
lost acknowledgement and a booking across a fare change) and three of five on
the hard tier. It is not a general audit of the settings an agent leaves behind.

**The SQL tool is a bounded diagnostic interface.** It uses a read-only
connection, an authorizer and a computation budget. It has not been audited as
a sandbox for hostile queries, and the project has had no external security
review.

**Demonstration export is a format sample.** Exported trajectories come from a
scripted policy on five training tasks. No model has been trained on them here.

## Scope

What is executed: five HTTP worker processes for pricing, inventory, payment,
booking and check-in, one SQLite database in WAL mode, and a trusted traffic
generator and verifier.

What is not:

- No PostgreSQL, Redis, Kafka or Kubernetes. The five services share one SQLite
  file; they do not have separately deployed databases. The "queue" is an outbox
  table with a single dispatcher.
- The payment service is a local ledger. Its lost-acknowledgement fault commits
  the charge and then returns HTTP 504 when the configured provider latency
  exceeds the caller's deadline. No time passes and no socket times out; the
  deadline is injected semantics, not measured timing.
- Customer traffic advances one window per agent action. It is not a wall-clock
  load generator, and an agent is never racing real time.
- Refunds and cancellations exist only on the hard tier and only as rows: a
  refund is a full-amount record against a captured charge, a cancellation is
  a customer event with a step number. No authentication, multi-region
  behaviour or concurrent dispatchers. No real airline, passenger or payment
  data.

## Review history

The benchmark has been broken by its own reviews three times. The versions
below, including 0.4.0, were internal builds; 0.5.0 is the first intended for publication. Full
entries are in the [changelog](../CHANGELOG.md).

**0.2.0** was the first version with executed services. It had eight cases.
Its reference policy solved 24 of 24 task-seed episodes and its no-repair
control solved none, which was taken as evidence that the cases needed
diagnosis. A review then wrote a policy that ignores every observation and
emits the same sixteen actions each time, with guessed booking and event IDs.
It also solved 24 of 24. Entity IDs were identical across episodes and several
repairs were safe to apply everywhere.

**0.2.1** made request and booking IDs depend on the seed, stopped labelling
the malformed event, and made provider latency read-only. The unchanged blind
script fell to 12 of 24. A slightly stronger script, the same blanket repairs
plus one SQL query to look up IDs, solved 24 of 24.

**0.3.0** added three payment-migration cases, in which a default retry charges
a customer twice, bringing the suite to eleven. The blanket script failed
those. A second review found the release was still solvable without reading
anything: IDs were a function of the seed, so one constant action list that
never read an observation solved 33 of 33 episodes. It found three further
holes:

- The Harbor verifier replayed an agent-written action list in a fresh world
  with a fixed seed, so a constant 19-action file scored 11 of 11, and an agent
  could damage the live world, learn from it, and submit a clean list.
- An agent could switch idempotency and price validation off after recovering
  and still score 1.0.
- The recovery term of the reward was diluted by healthy traffic, so stalling
  raised the reward and a do-nothing policy averaged 0.34.

**0.4.0** closes those: entity IDs are salted per episode; malformed events
come in three shapes; a safety check exercises the deployed settings after
`finish`; the recovery term uses a frozen denominator; and Harbor grades one
authoritative episode through a signed receipt.

The 0.4.0 easy tier was then measured with coding agents through the Harbor
control script: Codex (gpt-6-astra) solved 33 of 33 episodes, Claude Code with
Sonnet 5.5 32 of 33 and with Haiku 4.5 30 of 33, with no integrity violation in
99 episodes; every failure was a skipped verification step or an episode the
agent never finished, not a wrong repair.
The tier is saturated for frontier agents.

**0.5.0** adds the hard tier for that reason: an ambiguous payment ledger behind
a quota-limited provider lookup, traps that punish blanket repairs, bounded
noise, tighter budgets and a procedural generator. On it the shipped oracle
solves every generated instance it has been run on, while the easy-tier
reference policy and every one-branch rule fail most instances with integrity
violations. On five seeds per slot, Codex (gpt-6-astra) solved 46 of 60 hard
episodes and Claude Code 10–14 of 60, almost always failing with an integrity
violation; the README has the table.

What the hard tier does not change is listed above: the cases are generated
from a public source, a careful procedure still solves them, and that is the
main thing to keep in mind when reading any score on this environment.
