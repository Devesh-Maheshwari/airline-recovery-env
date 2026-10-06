"""A runnable observation -> action plugin. No model or credentials required.

Run from the repository root:
  python -m airline_recovery.live.evaluate --policy examples.custom_agent:agent \
      --split train --seeds 1 --output runs/custom-agent

Replace the body of agent() with your model's tool selection. The reset
observation contains JSON schemas in available_tools. The evaluator never passes
the simulator, its database, fault identity, or grader state to your callable.
"""
from airline_recovery.live.policies import ReferencePolicy

_workflow = ReferencePolicy()


def agent(observation: dict) -> dict:
    # This starter delegates to the scripted baseline. It is not a trained model.
    # Your integration can retain conversation state and use public tool results.
    # Always return: {"tool": "get_logs", "arguments": {"service": "booking"}}
    return _workflow(observation)


def reset() -> None:
    """Clear episode memory; the evaluator invokes a callable's optional reset."""
    _workflow.reset()


agent.reset = reset


if __name__ == "__main__":
    print("This module is a policy plugin. Evaluate it from the repository root with:\n"
          "  python -m airline_recovery.live.evaluate --policy examples.custom_agent:agent "
          "--split train --seeds 1 --output runs/custom-agent")
