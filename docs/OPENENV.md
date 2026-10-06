# OpenEnv integration

Airline Recovery Env exposes the environment through OpenEnv's `Environment`, `Action`,
`Observation`, `State`, `EnvClient` and `create_app` APIs
(`airline_recovery/openenv_adapter/`). The server speaks OpenEnv's WebSocket session protocol
and can serve OpenEnv's Gradio playground. The server computes the score, so an
agent connecting as a client is in a separate process from the grader. No model
provider or API key is involved.

The tools, observations, rules and scoring are the same as for the local
environment and are specified in the [environment reference](ENVIRONMENT.md).

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[openenv]'
python -m airline_recovery.openenv_adapter.server
```

The `openenv` extra installs `openenv>=0.7.0,<0.8` from PyPI. The base package
has no third-party dependencies; only this adapter needs OpenEnv.

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Port |
| `--max-sessions` | `4` | Concurrent WebSocket sessions |
| `--max-steps` | 48 on the easy tier, the case's budget on the hard tier | Action budget per episode; an explicit value applies to both tiers |
| `--no-web` | off | Do not serve the playground |

Open <http://127.0.0.1:8000/web/> for the playground, or
<http://127.0.0.1:8000/docs> for the generated API documentation.

In the playground, click **Reset**, choose `get_metrics`, enter `{}` as the
arguments and click **Step**. Reset there always starts the default training
case (`train`, index 0, seed 0); use the Python client to select other cases and
seeds. The reset observation lists every tool's schema. `finish` returns the
final score.

In another terminal, run a short client episode:

```bash
python examples/openenv_client.py --split train --index 0 --seed 7
```

It reads metrics and logs, then finishes without repairing anything, so a reward
of 0 is expected.

## Episodes run over WebSocket

An episode must be driven through the WebSocket client (`AirlineEnv`). The
stateless HTTP routes `POST /reset` and `POST /step` create a throwaway
environment for each request, so they cannot carry an episode: a `/step` sent
after a `/reset` reaches a new environment that has not been reset, and fails.

```python
import asyncio
from airline_recovery.openenv_adapter import AirlineAction, AirlineEnv

async def rollout(policy):
    async with AirlineEnv(base_url="http://127.0.0.1:8000") as env:
        transition = await env.reset(seed=42, split="train", index=0)
        while not transition.done:
            # The policy receives only public observations and returns
            # {"tool": ..., "arguments": {...}}.
            observation = transition.observation.model_dump()
            action = policy(observation)
            transition = await env.step(AirlineAction(**action))
        return transition.reward, transition.metadata["score"]
```

For synchronous code:

```python
from airline_recovery.openenv_adapter import AirlineAction, AirlineEnv

with AirlineEnv(base_url="http://127.0.0.1:8000").sync() as env:
    result = env.reset(seed=42, split="train", index=0)
    result = env.step(AirlineAction(tool="get_metrics", arguments={}))
    print(result.observation.result)
```

- `reset` accepts `seed` (an integer of 0 or more; 0 when omitted), and either
  `split`, `index` and `tier` keywords or
  `options={"split": ..., "index": ..., "tier": ...}`. `tier` is `easy`
  (default) or `hard`; a hard reset returns an observation with `tier` and
  `level` set and takes its action budget from the case unless the server was
  started with an explicit `--max-steps`. Supplying `episode_id` is an error;
  each reset generates its own.
- Each `StepResult` has `reward`, `done`, `observation` and `metadata`.
  `metadata` is the environment's `info`: after reset it is
  `{synthetic, backend, action_budget}`, after a step it has `action_cost` and
  `requests`, and on the final step it also has `score`.
- `observation` has the fields listed in the
  [environment reference](ENVIRONMENT.md#reset-observation), plus `reward`,
  `done`, `terminated` and `truncated`. The previous tool's response is
  `observation.result`, as `{tool, ok, data}` with `error` when `ok` is false.
- `await env.state()` returns `episode_id`, `step_count` and `done`.

## Sessions

Each WebSocket client owns a separate database and five worker processes,
started on reset. One client cannot see or change another client's services.
Resetting replaces the client's episode. Disconnecting, or leaving the context
manager, closes its workers and deletes its database.

The server allows four concurrent sessions by default, with a ten-minute
inactivity timeout. Every active session runs five worker processes, so size
`--max-sessions` to the machine.

The playground is a single shared debugging episode inside the server process.
Use WebSocket clients for parallel rollouts.

## Discover tasks and schemas

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/schema
curl http://127.0.0.1:8000/metadata
curl http://127.0.0.1:8000/list_environments
curl http://127.0.0.1:8000/airline_recovery/splits
curl -X POST http://127.0.0.1:8000/airline_recovery/tasks \
  -H 'Content-Type: application/json' -d '{"split":"train"}'
curl -X POST http://127.0.0.1:8000/airline_recovery/task \
  -H 'Content-Type: application/json' -d '{"split":"eval","index":0}'
```

`POST /airline_recovery/num_tasks` (body `{"split": ...}`) and
`POST /airline_recovery/task_range` (body `{"split": ..., "start": ..., "stop": ...}`)
are also available. An unknown split returns HTTP 400. These are OpenEnv's task
routes; they return metadata and do not start an episode.

Each task is listed as `{id, index, description}`. All eleven share one
description, and nothing in the listing identifies the fault, a reference
setting or a solution. The splits are `train` (5 tasks), `eval` (3) and
`test` (3); the [case table](ENVIRONMENT.md#cases) describes them. The task
routes list the easy tier. In Python, `list_tasks(split, tier="hard")`,
`num_tasks` and `get_task` take the same `tier` argument; hard tasks are
selected by `tier="hard"` on reset and also listed by
`python -m airline_recovery list --tier hard`.

## Container

```bash
docker build -t airline-recovery-env .
docker run --rm -p 127.0.0.1:8000:8000 airline-recovery-env
```

The image installs the pinned dependency snapshot in
`requirements-openenv.lock` (OpenEnv 0.7.0), runs as an unprivileged user, and
serves the playground. The repository `README.md` front matter and the
`Dockerfile` are laid out for a Hugging Face Docker Space.

`openenv.yaml` names the ASGI application (`airline_recovery.openenv_adapter.app:app`) and
carries a `validation:` block declaring the reward range, resource limits, the
fourteen tool names (the twelve easy-tier tools plus the hard tier's
`provider_lookup` and `void_booking`) and the easy-tier task count per split.

To run the ASGI application directly, set `ENABLE_WEB_INTERFACE=true` if you
want the playground:

```bash
ENABLE_WEB_INTERFACE=true uvicorn airline_recovery.openenv_adapter.app:app --port 8000
```

## Tests

```bash
python -m unittest discover -s tests -p test_openenv_live.py -v
```

The tests start a real uvicorn server and connect real OpenEnv WebSocket
clients. They are skipped when the `openenv` extra is not installed.

## Scope

The server has no authentication, quotas or network controls; see
[SECURITY.md](../SECURITY.md). Bind it to `127.0.0.1` unless you put those
controls in front of it.

The environment supplies rollouts and a terminal reward. Nothing here trains a
model. What the scores do and do not show is covered in
[LIMITATIONS.md](LIMITATIONS.md).
