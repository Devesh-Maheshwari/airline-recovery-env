# Hard-tier ablations: clearer instructions and a larger budget

**Question.** When coding agents fail the hard tier, is it because the task is
hard, or because the environment holds them back (terse instructions, a tight
action budget)? We reran two agents on the same instances under three
conditions and compared them episode by episode.

**Result.** Neither change moves the outcome much. With the integrity rules
spelled out in plain words, or with twice as many actions, Claude Sonnet 5.5
solves 5 to 7 of 24 episodes (6 with the standard text) and Claude Haiku 4.5
solves 5 of 24 (4 standard). Level 2 stays at 0 of 8 for both models in every
condition. Most failures in every condition are integrity harm: the agent's own
action broke a customer rule.

## Setup

- Agents: Claude Code with `claude-sonnet-5-5` and `claude-haiku-4-5`, run
  through `python -m airline_recovery.live.external` (see
  [docs/AGENTS.md](../../../../docs/AGENTS.md)).
- Instances: all 12 hard task slots × seeds 1 and 2 = 24 episodes per model and
  condition, the same instances in every condition. One attempt each.
- Conditions:
  - `standard`: the published hard task text.
  - `explicit` (`--explicit-instructions`): the same text plus the integrity
    rules in plain words and a general order of work
    (`EXPLICIT_HARD_RULES` in `airline_recovery/live/harbor.py`). It names what
    must not happen; it does not say how to solve any incident.
  - `budget2` (`--budget-scale 2`): twice each case's action budget (64, 72 or 68
    actions instead of 32, 36 or 34). Nothing else changes.
- Recorded 2026-10-07. Each run folder has `provenance.json` (command,
  instruction text, SHA-256 of every source file that ran),
  `episodes.jsonl`, `summary.json`, and per episode the world's full trace
  (`traces/*.jsonl.gz`: the reset observation and every action with the reply
  the agent saw), the agent's action list (`actions/`) and the CLI's final
  message and usage (`final/`).

## Results

`report.json` holds these numbers; `python scripts/ablation_report.py` rebuilds it.

| Model | Condition | Solved | Harmed a customer | No receipt | Other failure | Level 1 / 2 / 3 solved | Median share of budget used | Paired vs standard: solved only here / only in standard |
|---|---|---:|---:|---:|---:|---|---:|---|
| Sonnet 5.5 | standard | 6/24 | 17 | 0 | 1 | 5/8 · 0/8 · 1/8 | 0.96 | — |
| Sonnet 5.5 | explicit | 7/24 | 13 | 2 | 2 | 6/8 · 0/8 · 1/8 | 0.97 | 3 / 2 |
| Sonnet 5.5 | budget2 | 5/24 | 16 | 3 | 0 | 5/8 · 0/8 · 0/8 | 0.54 | 1 / 2 |
| Haiku 4.5 | standard | 4/24 | 16 | 0 | 4 | 4/8 · 0/8 · 0/8 | 0.96 | — |
| Haiku 4.5 | explicit | 5/24 | 15 | 0 | 4 | 5/8 · 0/8 · 0/8 | 1.00 | 1 / 0 |
| Haiku 4.5 | budget2 | 5/24 | 18 | 0 | 1 | 5/8 · 0/8 · 0/8 | 0.69 | 1 / 0 |

"No receipt" episodes ended without `finish`: in the Sonnet ones we read, the
agent's final message says it had already made an irreversible mistake (for
example, confirming a booking whose customer had cancelled) and stopped.
"Other failure" covers finished episodes with no integrity violation that did
not meet every success condition.

Episodes with each integrity violation (an episode can have several):

| Model / condition | Cancelled request fulfilled | Accepted fare changed | Duplicate sale | Duplicate charge | Oversold | Read `customer_events` at least once |
|---|---:|---:|---:|---:|---:|---:|
| Sonnet standard | 13 | 7 | 7 | 4 | 0 | 10/24 |
| Sonnet explicit | 8 | 5 | 4 | 2 | 0 | 12/24 |
| Sonnet budget2 | 14 | 7 | 7 | 2 | 0 | 18/24 |
| Haiku standard | 11 | 7 | 14 | 4 | 1 | 3/24 |
| Haiku explicit | 13 | 4 | 12 | 1 | 0 | 7/24 |
| Haiku budget2 | 13 | 6 | 14 | 5 | 3 | 2/24 |

## Reading

- **Instruction clarity is not the main obstacle.** Stating the rules reduced
  some harm for Sonnet (cancelled requests fulfilled 13 → 8, duplicate sales
  7 → 4) but converted only one more episode into a full solve. Haiku fulfilled
  cancelled requests as often with the rules stated as without them.
- **The budget is not the main obstacle.** With twice the actions, both models
  used about half to two thirds of them and solved the same number or fewer. In
  `budget2` Sonnet queried `customer_events` in 18 of 24 episodes and still
  confirmed a cancelled request in 14: seeing the data was not enough, it had
  to connect a late cancellation to the booking it was about to complete.
- **Level 2 is the wall.** No condition solved a level-2 episode. Level 2 adds a
  second fault, a second trap and a delayed fault to level 1.
- This agrees with the failure analysis of the original 180 episodes
  ([failure-analysis](../failure-analysis/README.md)): 96 of 110 failures were
  integrity harm, with the first harm at a median of 36% of the budget.

## Limits

- 24 episodes per cell and one attempt per instance: differences of one or two
  episodes are noise. The claim supported here is the absence of a large effect.
- Two models from one provider. Codex was not rerun; its 13 integrity failures
  in the published results all share one mechanism, a fare hold lost when the
  price cache is turned off, which [docs/SCENARIOS.md](../../../../docs/SCENARIOS.md)
  discusses as an open validity question.
- The `standard` reruns are a fresh sample, not a copy of the published
  results; they agree with them (Sonnet 6/24 here against 14/60 published,
  Haiku 4/24 against 10/60).
