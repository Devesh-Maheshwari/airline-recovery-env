r"""Drive the environment with any OpenAI-compatible chat-completions endpoint.

Works with Together AI, OpenRouter, vLLM, Ollama and similar servers that
implement ``POST /v1/chat/completions`` with ``tools``. Standard library only.

    export OPENAI_BASE_URL=https://api.together.xyz/v1
    export OPENAI_API_KEY=...
    export AGENT_MODEL=meta-llama/Llama-3.3-70B-Instruct-Turbo
    python -m airline_recovery.live.evaluate --policy examples.openai_compatible_agent:agent \
        --split train --index 1 --seeds 1 --output runs/llama-70b

The evaluator owns the episode loop and calls the policy once per step, so the
conversation is kept here. Each step resends the growing transcript, so one
episode can cost several hundred thousand input tokens; start with one task.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import deque
from typing import Any

FINISH = {"tool": "finish", "arguments": {}}


class OpenAICompatibleAgent:
    """Policy callable: public observation in, one ``{"tool", "arguments"}`` action out."""

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 max_tokens: int = 2048, temperature: float = 0.0):
        self.model = model or os.environ.get("AGENT_MODEL", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.max_tokens, self.temperature = max_tokens, temperature
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}
        self.reset()

    def reset(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.pending: deque[dict[str, Any]] = deque()
        self.current: dict[str, Any] | None = None
        self.results: list[dict[str, Any]] = []

    def __call__(self, observation: dict[str, Any]) -> dict[str, Any]:
        if observation.get("step") == 0:
            self.reset()
            self._start(observation)
        else:
            self.results.append({
                "role": "tool", "tool_call_id": self.current["id"],
                "content": json.dumps({k: observation[k] for k in ("step", "alerts", "summary", "result")}),
            })
        if not self.pending:
            self.messages.extend(self.results)
            self.results = []
            self._ask()
        if not self.pending:
            return FINISH
        self.current = self.pending.popleft()
        try:
            arguments = json.loads(self.current["function"].get("arguments") or "{}")
        except ValueError:
            arguments = {}
        return {"tool": self.current["function"]["name"], "arguments": arguments if isinstance(arguments, dict) else {}}

    def _start(self, observation: dict[str, Any]) -> None:
        if not self.model:
            raise RuntimeError("set AGENT_MODEL to the model name your endpoint serves")
        self.tools = [{"type": "function", "function": {
            "name": tool["name"], "description": tool["description"], "parameters": tool["parameters"]}}
            for tool in observation["available_tools"]]
        self.messages = [
            {"role": "system", "content": (
                "You are the on-call engineer for a synthetic airline booking system.\n\n"
                f"Mission: {observation['mission']}\n\n"
                f"Episode rules: {json.dumps(observation['episode_contract'])}\n\n"
                f"Configuration fields: {json.dumps(observation['configuration_contracts'])}\n\n"
                "Work only through the tools. Investigate before you change anything, and call finish when verified.")},
            {"role": "user", "content": json.dumps({k: observation[k] for k in ("step", "alerts", "summary")})},
        ]

    def _ask(self) -> None:
        body = {"model": self.model, "messages": self.messages, "tools": self.tools, "tool_choice": "auto",
                "max_tokens": self.max_tokens, "temperature": self.temperature}
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}",
                     # Some gateways reject the default urllib signature.
                     "User-Agent": "airline-recovery-env/0.5 (openai-compatible agent)"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                reply = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"model endpoint returned HTTP {error.code}: {error.read().decode(errors='replace')[:500]}")
        usage = reply.get("usage") or {}
        self.usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.usage["completion_tokens"] += usage.get("completion_tokens", 0)
        self.usage["requests"] += 1
        message = reply["choices"][0]["message"]
        # Replay the assistant turn exactly as returned so tool_call ids line up.
        self.messages.append({k: message[k] for k in ("role", "content", "tool_calls") if k in message})
        for call in message.get("tool_calls") or []:
            if call.get("type", "function") == "function":
                self.pending.append(call)


agent = OpenAICompatibleAgent()


if __name__ == "__main__":
    print(__doc__)
