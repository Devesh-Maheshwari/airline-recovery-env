"""Run a policy through the real OpenEnv WebSocket transport (no API key needed)."""

import argparse
import asyncio
import json

try:
    from airline_recovery.openenv_adapter import AirlineAction, AirlineEnv
except ModuleNotFoundError as error:
    raise SystemExit(f"This example needs the OpenEnv extra ({error.name} is not installed): pip install -e '.[openenv]'")


async def run(base_url: str, seed: int, split: str, index: int):
    async with AirlineEnv(base_url=base_url) as client:
        result = await client.reset(seed=seed, split=split, index=index)
        print(json.dumps({"event": "reset", "observation": result.observation.model_dump()}))
        # Replace this short exploration with your model/tool-calling loop.
        # For a full reference rollout, see docs/OPENENV.md and docs/AGENTS.md.
        for tool in ("get_metrics", "get_logs", "finish"):
            result = await client.step(AirlineAction(tool=tool, arguments={}))
            print(json.dumps({
                "event": "step", "action": tool,
                "observation": result.observation.model_dump(),
                "reward": result.reward, "done": result.done,
                "metadata": result.metadata,
            }))
            if result.done:
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--split", choices=["train", "eval", "test"], default="train")
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run(args.url, args.seed, args.split, args.index))
