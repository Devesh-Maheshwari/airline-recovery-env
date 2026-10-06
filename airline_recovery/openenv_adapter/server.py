"""Run ``python -m airline_recovery.openenv_adapter.server`` for the native OpenEnv API/UI."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from weakref import WeakSet

from openenv.core.env_server import create_app
from openenv.core.env_server.types import ConcurrencyConfig

from .environment import AirlineEnvironment
from .models import AirlineAction, AirlineObservation, AirlineState


class PlaygroundManager:
    """Convert native textbox JSON without relaxing the typed remote API."""

    def __init__(self, manager):
        self.manager = manager

    async def reset_environment(self):
        return await self.manager.reset_environment()

    def get_state(self):
        return self.manager.get_state()

    async def step_environment(self, action):
        action = dict(action)
        arguments = action.get("arguments", {})
        if isinstance(arguments, str):
            if len(arguments) > 20000:
                raise ValueError("Arguments JSON exceeds 20000 characters")
            arguments = json.loads(arguments or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("Arguments must be a JSON object")
        action["arguments"] = arguments
        return await self.manager.step_environment(action)


def build_playground(web_manager, action_fields, metadata, is_chat_env, title, quick_start_md):
    """Keep the native controls while supplying an executable Airline Recovery Env example."""
    from openenv.core.env_server.gradio_ui import build_gradio_app

    instructions = """### Connect to Airline Recovery Env
Use the URL of this server as `base_url` (the local default is shown below).

```python
from airline_recovery.openenv_adapter import AirlineAction, AirlineEnv

with AirlineEnv(base_url="http://127.0.0.1:8000").sync() as env:
    env.reset(seed=42, split="train", index=0)
    result = env.step(AirlineAction(tool="get_metrics", arguments={}))
    print(result.observation.result)
```

In the playground, click **Reset**, choose a tool, enter `{}` in Arguments,
then click **Step**. Reset starts the default training case. The initial JSON
observation lists each tool's argument schema. Use the Python client to select
other cases, seeds and the hard tier (`env.reset(seed=42, split="train", index=0, tier="hard")`).
"""
    return build_gradio_app(
        PlaygroundManager(web_manager), action_fields, metadata, is_chat_env,
        title=title, quick_start_md=instructions,
    )


def build_app(max_sessions: int = 4, max_steps: int | None = None):
    instances: WeakSet[AirlineEnvironment] = WeakSet()

    def environment_factory():
        environment = AirlineEnvironment(max_steps=max_steps)
        instances.add(environment)
        return environment

    environment_factory.SUPPORTS_CONCURRENT_SESSIONS = True
    app = create_app(
        environment_factory,
        AirlineAction,
        AirlineObservation,
        state_cls=AirlineState,
        env_name="airline_recovery",
        gradio_builder=build_playground,
        show_default_tab=False,
        title_override="Airline Recovery Env",
        concurrency_config=ConcurrencyConfig(
            max_concurrent_envs=max_sessions, session_timeout=600.0
        ),
    )
    # Native OpenEnv closes every disconnected WebSocket environment. Its
    # optional UI also owns a persistent debugging environment; explicitly
    # close that instance on shutdown, preserving the framework's lifespan.
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application) as state:
                yield state
        finally:
            for instance in list(instances):
                await asyncio.to_thread(instance.close)

    app.router.lifespan_context = lifespan
    app.state.airline_environments = instances
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-sessions", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=None,
                        help="action budget per episode (default: 48 on the easy tier, the case's budget on the hard tier)")
    parser.add_argument("--no-web", action="store_true", help="Disable the native OpenEnv playground")
    args = parser.parse_args()
    os.environ["ENABLE_WEB_INTERFACE"] = "false" if args.no_web else "true"
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "false")
    import uvicorn

    uvicorn.run(
        build_app(args.max_sessions, args.max_steps),
        host=args.host, port=args.port, log_level="info",
    )


if __name__ == "__main__":
    main()
