r"""Drive the environment with a Claude model through native tool calling.

    pip install anthropic
    export ANTHROPIC_API_KEY=...
    python -m airline_recovery.live.evaluate --policy examples.llm_agent:agent \
        --split train --index 1 --seeds 1 --output runs/llm-agent

The evaluator owns the episode loop and calls the policy once per step, so the
conversation is kept here: each call returns the previous tool result to the
model and asks for the next tool call. This spends API credits: one episode
resends a growing transcript for up to 48 steps. Start with a single task.

Set AIRLINE_RECOVERY_MODEL or AIRLINE_RECOVERY_EFFORT to try another model or reasoning effort.
"""
from __future__ import annotations

import json
import os
from collections import deque
from typing import Any

MODEL = os.environ.get("AIRLINE_RECOVERY_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("AIRLINE_RECOVERY_EFFORT", "medium")
FINISH = {"tool": "finish", "arguments": {}}


class ClaudeAgent:
    """Policy callable: public observation in, one ``{"tool", "arguments"}`` action out."""

    def __init__(self, model: str = MODEL, effort: str = EFFORT):
        self.model, self.effort = model, effort
        self.client = None
        self.reset()

    def reset(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.system = ""
        self.pending: deque[Any] = deque()   # tool_use blocks not yet executed
        self.current = None                  # tool_use block whose result arrives next
        self.results: list[dict[str, Any]] = []

    def __call__(self, observation: dict[str, Any]) -> dict[str, Any]:
        if observation.get("step") == 0:
            self.reset()
            self._start(observation)
        else:
            # The observation is the result of the tool call returned last time.
            self.results.append({
                "type": "tool_result", "tool_use_id": self.current.id,
                "content": json.dumps({k: observation[k] for k in ("step", "alerts", "summary", "result")}),
                "is_error": not (observation.get("result") or {}).get("ok", False),
            })
        if not self.pending:
            if self.results:
                # Results for every tool call of one assistant turn go back together.
                self.messages.append({"role": "user", "content": self.results})
                self.results = []
            self._ask()
        if not self.pending:
            return FINISH  # the model stopped calling tools
        self.current = self.pending.popleft()
        return {"tool": self.current.name, "arguments": dict(self.current.input)}

    def _start(self, observation: dict[str, Any]) -> None:
        if self.client is None:
            import anthropic
            self.client = anthropic.Anthropic()
        # The reset observation carries the tool schemas and the rules of the episode.
        self.tools = [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
                      for tool in observation["available_tools"]]
        self.system = (
            "You are the on-call engineer for a synthetic airline booking system.\n\n"
            f"Mission: {observation['mission']}\n\n"
            f"Episode rules: {json.dumps(observation['episode_contract'])}\n\n"
            f"Configuration fields: {json.dumps(observation['configuration_contracts'])}\n\n"
            "Work only through the tools. Investigate before you change anything, and call finish when verified."
        )
        self.messages = [{"role": "user", "content": json.dumps(
            {k: observation[k] for k in ("step", "alerts", "summary")})}]

    def _ask(self) -> None:
        response = self.client.messages.create(
            model=self.model, max_tokens=16000,
            system=self.system, tools=self.tools, messages=self.messages,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # the transcript prefix repeats every step
        )
        # Keep the full content: thinking blocks must be replayed unchanged.
        self.messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "tool_use":
            self.pending.extend(block for block in response.content if block.type == "tool_use")
        # end_turn, refusal or max_tokens: no tool call follows, so the episode is finished.


agent = ClaudeAgent()


if __name__ == "__main__":
    print(__doc__)
