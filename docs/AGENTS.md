# Bringing an agent

There are three ways to run an agent against Airline Recovery Env 0.5.0. They use the same
environment and the same tools; they differ in where the agent runs relative to
the grader.

| Route | Agent runs | Score is produced by | Use it for |
|---|---|---|---|
| [Local evaluator](#local-evaluator) | In the grader's Python process | The same process | Development, baselines, exporting demonstrations |
| [OpenEnv server](OPENENV.md) | In a separate client process | The server | Training loops, remote or untrusted policies |
| [Harbor tasks](#harbor) | In its own container | A sidecar container, checked by a verifier container | Containerised agent harnesses |

The tools, observations, rules and scoring are specified in the
[environment reference](ENVIRONMENT.md). Read [limitations](LIMITATIONS.md)
before interpreting a score.

## The policy contract

A policy is a synchronous Python callable. It receives one public observation
and returns one action:

```python
def agent(observation: dict) -> dict:
    return {"tool": "get_logs", "arguments": {"service": "booking"}}
```

- The observation is a deep copy. The first one (`step` 0) contains
  `available_tools`, `configuration_contracts`, `episode_contract` and `mission`.
  Later ones contain only `episode_id`, `step`, `alerts`, `summary` and `result`,
  so keep whatever history you need inside the callable.
- The return value must be a JSON-serialisable dict with exactly the keys
  `tool` and `arguments`. A non-dict return value, or an exception, ends the
  episode and records it as failed with an `error`. A dict that the environment
  rejects (unknown tool, bad arguments) consumes a step and returns an error in
  `observation["result"]`.
- If the callable has a zero-argument `reset` attribute, the evaluator calls it
  before every episode. Stateless functions need none.
- The callable is not given the environment object, the reset `info`, the case
  definition or the grader's state.

The example above only reads logs and will run out of actions. Return
`{"tool": "finish", "arguments": {}}` once two healthy probes have been counted.

## Local evaluator

Requires Python 3.11 or later and permission to open loopback sockets. The base
package has no third-party dependencies.

```bash
python -m pip install -e .
python -m airline_recovery.live.evaluate --policy reference --split train --index 1 --seeds 1 --output runs/smoke
python -m airline_recovery.live.evaluate --policy my_module:agent --split all --seeds 1,2,3 --output runs/my-agent
```

`my_module` must be importable from the directory you run the command in. The
installed console script `airline-recovery-eval` is the same program.

| Flag | Default | Meaning |
|---|---|---|
| `--policy` | `reference` | `nop`, `blanket`, `blanket-hard`, `source-aware`, `reference`, `reference-adopt`, `oracle`, `adopt-else-reconcile`, `wait-then-reconcile`, or `importable.module:callable` |
| `--split` | `train` | `train`, `eval`, `test` or `all` |
| `--tier` | `easy` | `easy` or `hard`. Hard episodes get run IDs prefixed `hard:` and carry the case's `level` |
| `--seeds` | `1,2,3` | Comma-separated unique integers. Every selected task is run once per seed |
| `--index` | all tasks | Run only this task index in each selected split. With `--split all`, an index that does not exist in every split is an error |
| `--max-steps` | 48 on the easy tier, the case's budget on the hard tier | Action budget per episode (8 to 256). Giving it explicitly overrides a hard case's budget |
| `--output` | `runs/eval` | Output directory |
| `--overwrite` | off | Without it, the run refuses to start if result files already exist in `--output` |
| `--quiet` | off | Suppress the per-episode progress line |
| `--export-sft JSONL` | off | After the run, export successful train episodes to this file |
| `--sft-format` | `json-actions` | `json-actions` or `trl-tools` |

The same `--split`, `--index`, `--seeds` and `--max-steps` produce the same
grid of episode keys (`split:index:seed`) for every policy, so runs can be
compared episode by episode. The seed fixes the incident, not the identifiers:
see [what the seed controls](ENVIRONMENT.md#what-the-seed-controls).

Each episode gets its own worker processes and database, closed after the
episode whether it ends normally, is truncated, or the policy raises. The
command prints the summary as JSON and exits with status 1 if any episode
recorded an error.

### Output files

| File | Contents |
|---|---|
| `trajectories.jsonl` | One line per event: `reset` (observation and info), `step` (action, observation, reward, flags, `decision_seconds`, info), `error`, and `episode_end` |
| `episodes.jsonl` | One record per episode, including failed ones |
| `summary.json` | Aggregates for the whole run, per split and per task |
| `provenance.json` | Start and completion times, Python and platform, run arguments, SHA-256 of every file in `airline_recovery/live/` and of the policy's source file, and `trust_boundary` |

Episode record fields: `run_id`, `policy`, `tier`, `level` (hard tier only,
otherwise `null`), `split`, `task_index`, `task_id`, `seed`, `success`,
`reward`, `score` (the full [score object](ENVIRONMENT.md#reward-and-score)),
`steps`, `tool_calls` (actions other than `finish`), `mutations` (actions
naming a mutating tool, whether or not they were accepted), `requests`,
`integrity`, `incident_recovery`, `excess_capture_cents`, `cost`,
`wall_seconds`, `terminated`, `truncated`, `error`.

### Summary fields

`summary.json` has `schema_version` (2), `policy`, `seeds`, `max_steps`
(`null` when hard cases supplied their own budgets), `tier`, `trust_boundary`,
`wall_seconds`, `interpretation`, `matched_episode_keys`,
`source_stable_through_completion`, and three aggregate blocks: `all`,
`by_split` and `by_task`. Each aggregate block contains:

| Field | Meaning |
|---|---|
| `episodes`, `successes`, `pass_rate` | Episode counts and their ratio |
| `pass_rate_95pct_wilson` | Two-sided 95% Wilson interval over episodes |
| `by_level` | On the hard tier, `episodes`, `successes` and `pass_rate` per generator level; empty on the easy tier |
| `tasks` | Distinct tasks in the block |
| `tasks_solved_on_every_seed` | Tasks whose every episode succeeded |
| `integrity_violation_episodes` | Episodes whose score has `integrity: false` |
| `excess_capture_cents` | Sum over episodes of charges beyond the accepted amount, taken from `score.details.business_impact` (which includes anything the post-finish safety check uncovered) |
| `mean_incident_recovery` | Mean of the `incident_recovery` reward term |
| `mean_reward`, `mean_steps`, `mean_tool_calls`, `mean_cost`, `mean_requests`, `mean_wall_seconds`, `total_wall_seconds` | Means and totals over episodes |
| `terminated`, `truncated`, `errors` | Episodes that ended by `finish`, by exhausting the budget, or with an evaluator-level error |

`source_stable_through_completion` is false if any file in `airline_recovery/live/` changed
while the run was in progress.

Seeds of one task are related samples, and the eleven tasks share mechanisms.
The Wilson interval describes the episodes in that run and nothing wider. Read
`tasks_solved_on_every_seed` next to `pass_rate`, report the whole grid, and
keep the failed trajectories.

### Trust boundary

`summary.json` and `provenance.json` both carry `"trust_boundary": "in-process"`.
A `module:function` policy runs in the same interpreter as the environment and
the verifier. Nothing stops such code from importing the case definitions,
reaching the environment object, or editing the score. The evaluator passes the
policy only public observations, which keeps honest code honest; it is not a
sandbox. Scores from the local evaluator are therefore self-reported and are not
tamper-evident.

To put a process boundary between agent and grader, use the
[OpenEnv server](OPENENV.md) or [Harbor](#harbor).

## Bundled policies

All of these are hand-written scripts in `airline_recovery/live/policies.py` and
`airline_recovery/live/oracle.py`. None is a trained model. Measured results for them are in
the results tables of the [README](../README.md).

Easy tier:

| Name | What it does | What it is for |
|---|---|---|
| `reference` | Reads metrics, logs, settings, pending bookings joined to their charges, and pending events. Then restarts stopped workers, fixes the payment deadline, re-enables guards and the consumer, widens the accepted schema when it sees version-2 events, quarantines events whose payload is unparseable, lacks identity fields or names another booking, invalidates the fare cache when logs mention a stale quote, and reconciles each pending booking with the idempotency key of its existing charge. Probes, and repeats the inspection up to three times if a probe is unhealthy | Shows every case has a repair through the public tools. Used as the Harbor oracle and as the source of exported demonstrations |
| `reference-adopt` | The same, except that a pending booking with exactly one charge is completed by adopting that charge (`existing_charge_id`) instead of retrying its key | Shows a second valid recovery path receives the same credit |
| `source-aware` | Applies the blanket repairs below, then for each pending booking computes the first charge ID from the booking ID using the public derivation, tries adoption, and falls back to a default retry. Never reads `charges` | Control: recovers payment-migration bookings without reading the ledger. Any claim that a case needs ledger diagnosis has to survive this policy |
| `blanket` | Sets the payment deadline to its maximum, restarts `pricing` and `payment`, invalidates the fare cache, enables the consumer with schema 2 and batch size 50, then runs one SQL query for pending bookings and for events failing `json_valid`, reconciles each booking with the deployed key and quarantines each such event | Adverse control: one runbook applied to everything, with no diagnosis. The default retry writes a second charge for a booking charged under the other key version |
| `nop` | Probes until step 8, then finishes | No-repair control. It waits past the injection horizon so that delayed faults arrive |

Hard tier (see the [hard tier reference](ENVIRONMENT.md#hard-tier)):

| Name | What it does | What it is for |
|---|---|---|
| `oracle` | Five reads (settings, metrics, pending bookings with their holds, charge states, cancellations and sibling bookings; pending events with validity and attempts; fare holds, cache rows and 409 counts), then one decision per pending booking: void a cancelled or duplicate request, adopt a captured charge, retry a declined key, look up an unknown outcome while quota remains and otherwise wait for settlement, reconcile the rest. Fixes settings from evidence, quarantines only events that fail the public validity rule, invalidates only stale flights without a live hold, restarts only stopped workers and never the payment worker while a charge is in flight. Probes twice and finishes; repeats the reads up to three times after an unhealthy probe | Shows every generated case has a repair through the public tools. It imports nothing from the environment, generator or verifier, and a test checks that. Used as the Harbor oracle for hard tasks |
| `blanket-hard` | The easy `blanket` runbook plus restart every worker, invalidate the whole cache and reconcile every pending booking with the deployed key | Adverse control: the symptoms-only runbook. Restarting the payment worker loses in-flight captures, the unscoped invalidation breaks fare holds, and reconciling cancelled bookings fulfils cancelled requests |
| `adopt-else-reconcile` | Adopts a captured charge where one exists, otherwise reconciles with the deployed key; never looks up or waits | One-branch control: ignores unknown and declined outcomes |
| `wait-then-reconcile` | Waits for settlement, then reconciles everything still pending | One-branch control: ignores cancellations, duplicates and expired keys |

The easy policies run on hard tasks, and the hard ones on easy tasks, but they
are calibrated for their own tier. Each control is meant to fail for a
designed reason. On seeds 1–3 none solves more than 6 of 36 episodes (the three
alert-only instances plus level-1 cases whose trap the control happens not to
trip), and none solves more than 1 of 12 slots on every seed.

`python -m airline_recovery demo` prints the `reference` policy's steps on one task
(`--split`, `--index`, `--seed`, `--delay`, `--json`); with `--tier hard` it runs
the `oracle` on a hard task. `python -m airline_recovery list` prints the public
task manifest; `--tier hard` lists the hard tasks and `--tier all` both. The
four-strategy comparison is described in [SHOWCASE.md](SHOWCASE.md).

## Examples

| File | Contents |
|---|---|
| [`examples/custom_agent.py`](../examples/custom_agent.py) | Minimal plugin with a `reset` hook; delegates to the reference policy. Start here and replace the body |
| [`examples/llm_agent.py`](../examples/llm_agent.py) | Drives the environment with a Claude model through the Anthropic API's native tool calling, keeping the conversation inside the callable. Needs `pip install anthropic` and an API key, and spends API credits |
| [`examples/openai_compatible_agent.py`](../examples/openai_compatible_agent.py) | The same loop against any OpenAI-compatible `chat/completions` endpoint with tools (Together AI, OpenRouter, vLLM, Ollama). Standard library only; configured by `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `AGENT_MODEL` |
| [`examples/blanket_baseline.py`](../examples/blanket_baseline.py) | The blanket runbook as a standalone plugin |
| [`examples/openenv_client.py`](../examples/openenv_client.py) | A short episode over the OpenEnv WebSocket client |

```bash
python -m airline_recovery.live.evaluate --policy examples.custom_agent:agent --split train --seeds 1 --output runs/custom-agent
```

## Exporting demonstrations

```bash
python -m airline_recovery.live.evaluate --policy reference --split train --seeds 1,2,3 \
  --output runs/train-demo --export-sft runs/train-demo/sft.jsonl
```

To convert an existing run without starting services:

```python
from airline_recovery.live.evaluate import export_sft

export_sft("runs/train-demo/trajectories.jsonl", "runs/train-demo/tools.jsonl", format="trl-tools")
```

`export_sft` returns `{"episodes_read", "successful_train_episodes_exported"}`.

| Format | Shape of each row |
|---|---|
| `json-actions` (default) | `messages`: a system message, then alternating `user` (the observation as JSON) and `assistant` (the action as JSON). The row ends with the final action |
| `trl-tools` | `tools`: one function schema per tool. `messages`: a system message, a `user` message with the reset observation, then alternating `assistant` messages with one `tool_calls` entry and `tool` messages with the next observation. Arguments are JSON objects. Call IDs are `call_0001`, `call_0002`, … and each tool message carries the matching `tool_call_id` and `name` |

Both rows also have `metadata`: `{"split": "train", "run_id", "policy"}`.

What the exporter guarantees:

- Only episodes that succeeded, ran entirely on the `train` split, and recorded
  no error are exported. Eval and test episodes are never written, whatever the
  run contained.
- Messages contain only public observation fields (`episode_id`, `step`,
  `alerts`, `summary`, `result`, and on reset the tool schemas, contracts and
  mission). Rewards, `info`, scores and violation lists are not exported. In
  `trl-tools` rows the tool schemas appear in `tools`, not in the messages.
- Every eligible episode is checked for consecutive step numbers, well-formed
  actions, matching tool results and consistent terminal flags before the
  output file is opened. A malformed episode raises an error and leaves an
  existing output file untouched.
- The output path may not be the trajectory file itself.

What it does not do: it loads no tokenizer, model or trainer. Whether a given
chat template renders tool calls correctly, and whether a row fits a context
window, has to be checked with the tokenizer you train with. The `trl-tools`
layout follows the
[TRL tool-calling dataset format](https://huggingface.co/docs/trl/dataset_formats#tool-calling).

The demonstrations come from a scripted policy on five training tasks. They are
a format sample, not a training corpus, and no model has been trained on them in
this project.

## Coding-agent CLIs (Claude Code, Codex)

`airline_recovery.live.external` evaluates an installed coding-agent CLI the way
Harbor does, without Docker: for every episode it starts the world as a sidecar
process with a private signing key, gives the agent a scratch directory holding
only `control.py` and `episode.json`, hands it the Harbor task text, and grades
the signed receipt. The CLI runs under whatever account it is already logged
into; the runner never reads or stores credentials and strips `ANTHROPIC_*` and
`OPENAI_*` variables from the agent's environment.

```bash
python -m airline_recovery.live.external --agent claude-code --model claude-sonnet-5-5 \
    --split all --seeds 1,2,3 --output runs/claude-sonnet
python -m airline_recovery.live.external --agent codex --model gpt-6-astra \
    --split all --seeds 1,2,3 --output runs/codex
```

| Flag | Meaning |
|---|---|
| `--agent` | `claude-code` (runs `claude -p` restricted to `Bash(python control.py:*)`) or `codex` (runs `codex exec` in its workspace sandbox with network access) |
| `--model` | Model name passed to the CLI |
| `--split`, `--index`, `--seeds` | Same grid selection as the local evaluator |
| `--tier` | `easy` (default) or `hard`. On the hard tier the task text states the case's budget, read from the sidecar's reset observation, and omits the easy tier's hints about what to inspect and preserve; records carry `level` and `budget`, and run IDs are prefixed `hard:` |
| `--timeout` | Seconds per episode before the agent process is killed (default 1800); a killed agent that never finished scores 0 |
| `--max-turns` | Turn cap passed to CLIs that support one |
| `--keep-world` | Keep each episode's sidecar key and log |

Output: `episodes.jsonl` (one record per episode with `reward`, `success`,
`finished`, `steps`, the CLI's token usage and cost where it reports them, and
the agent's exit code), `summary.json`, `provenance.json`
(`trust_boundary: external-process`), and `episodes/<task>-seed<n>/agent/` with
the agent's stdout, stderr and action log.

Isolation is weaker than Harbor's. The agent process runs on the same machine as
the environment source and could read it if it went looking; Claude Code is
limited to the control script, Codex is not. Nothing in the task text points at
the source. Treat these as model baselines on the published tasks, not as
evidence of adversarial robustness.

## Harbor

Each case is also packaged as a [Harbor](https://www.harborframework.com/docs/tasks)
task. The commands below were checked with Harbor 0.23.0 and Docker.

### Generate tasks

```bash
python -m airline_recovery build-harbor --output my-tasks
```

This writes eleven tasks under `my-tasks/{train,eval,test}/` and a
`manifest.json`. Each generation draws a new random receipt key for every task.
The command refuses a non-empty `--output` unless it is a previous output of
this command. `--seed N` pins every task to one incident seed, for debugging;
without it, each container start draws a fresh seed.

`--tier hard` writes the twelve hard tasks under `my-tasks/hard/{train,eval,test}/`
instead, and `--tier all` writes both sets. A single-tier run leaves the other
tier's folders untouched, keys included; use `--tier all` to rotate every key. A hard task's `task_spec.json`
carries `"tier": "hard"`, its `task.toml` has `difficulty = "hard-<level>"`,
its instruction states the case's budget, its sidecar image also contains the
case generator and injector, and its solution runs the `oracle` policy. The
`manifest.json` lists the tasks of the latest generation with their `tier`.

The repository ships a generated set in `live-tasks/`. Its receipt keys are in
the public repository, so anyone can sign a receipt for those tasks. Use them to
try the flow. For results you intend to report, generate your own set and keep
it private.

### Task layout

```text
airline-recovery-train-000/
  instruction.md            what the agent is told
  task.toml                 Harbor metadata; the artifact is /app/episode.json
  environment/
    Dockerfile              agent image: Python plus control.py and episode.json
    control.py              the agent's only interface
    episode.json            starts as {"schema_version": 3, "actions": []}
    docker-compose.yaml     adds the "world" sidecar and waits for its health check
    world-<task id>/        sidecar image: the environment code, task_spec.json, receipt.key
  tests/                    verifier image: grade.py, test.sh, <task id>.spec.json, <task id>.receipt.key
  solution/                 oracle: solve.sh, solve.py, policies.py, oracle.py
```

The sidecar folder and the verifier's spec and key carry the task id because
Docker BuildKit can reuse a cached build context whose folder name, file paths,
sizes and modification times match. A source archive gives every file the same
modification time, so fixed names could build one task's spec and key into
another task's images.

The agent image contains no environment code, no case definition, no database
and no key. The `world` sidecar runs `airline_recovery.live.bridge`, which resets one episode
when the container starts and serves it on port 8081.

### How an agent acts

Inside the agent container:

```bash
python /app/control.py
python /app/control.py action '{"tool":"get_metrics","arguments":{}}'
```

The first form prints the current transition, a JSON object with `observation`,
`reward`, `terminated`, `truncated` and `info`. Before any action that is the
reset observation, including the tool schemas. The second form sends one action
and prints the resulting transition. The budget is 48 actions on easy tasks; a hard task states its own budget in its
instruction.

There is exactly one episode per container start. It cannot be reset, and
actions cannot be undone. After the episode ends, further actions are refused:
`control.py` exits with `World rejected the request: …`.

### Receipts

When the episode ends, by `finish` or by exhausting the budget, the sidecar adds
a `receipt` to that final response:

```text
receipt.body       schema_version (3 for easy tasks, 4 for hard tasks), task {split, index},
                   tier, seed, steps, actions_sha256, terminated, truncated, score
receipt.signature  HMAC-SHA256 of the canonical JSON of body, under the task's key
```

The verifier checks the schema version and the tier as well as the task, so an
easy receipt cannot be presented for a hard task of the same split and index.

`control.py` stores the receipt in `/app/episode.json`, which Harbor collects as
the task artifact. It does so from any response that contains one, so if the
reply to the final action is lost, running `python /app/control.py` again
recovers the receipt. The verifier (`tests/grade.py`) reads only that receipt. It
recomputes the signature with its copy of the key, checks that the receipt names
this task and that the reward is a finite number between 0 and 1, and writes
`reward` and `success` to `/logs/verifier/reward.json`. The full receipt body is
written to `/logs/verifier/details.json`.

The reward is 0 when there is no receipt, the signature does not verify, the
receipt is for another task, or the artifact is not a regular JSON file of at
most 1 MB. An agent that never finishes and never exhausts its budget has no
receipt and scores 0.

### Threat model

| The design defends against | How |
|---|---|
| An agent writing its own score or editing `/app/episode.json` | The verifier accepts only a body signed with a key the agent image never contains |
| An agent submitting a clean action list after damaging the world | The action list in `episode.json` is not read. The score is the outcome of the one episode the sidecar ran |
| A constant action script prepared in advance | The sidecar draws a fresh seed per container start, and entity IDs are salted per episode |
| Retrying until a good outcome | One episode per container start, with no reset route |

What it does not defend against:

- Keys that are public. This applies to the committed `live-tasks/`.
- An agent that can read the sidecar's or verifier's filesystem. Isolation
  between containers is Harbor's and Docker's, not this project's.
- Replay of an old receipt. A receipt is bound to the task and its key, not to
  a particular run. Anyone who holds a valid receipt from an earlier run of the
  same generated task can present it again.
- The sidecar's HTTP port has no authentication. Anything on the task's network
  that can reach `world:8081` can act on the episode.

### Run

```bash
harbor run -p live-tasks/train -a oracle
```

The oracle runs the `reference` policy through `control.py` on easy tasks and
the `oracle` policy on hard tasks. Replace `oracle` with your agent, and
`live-tasks/train` with your generated set (`my-tasks/hard/train` for hard
tasks).
