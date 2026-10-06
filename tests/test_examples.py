"""The model-API example is exercised with a stub client; no network or API key."""
import json
import types
import unittest

from examples.llm_agent import ClaudeAgent
from airline_recovery.live.environment import LiveAirlineEnv
from airline_recovery.live.policies import ReferencePolicy


class StubMessages:
    """Answers like a model that follows the reference policy, two tool calls per turn at first."""
    def __init__(self):
        self.policy, self.requests, self.observation = ReferencePolicy(), [], None

    def create(self, **request):
        self.requests.append(request)
        last = request["messages"][-1]["content"]
        if isinstance(last, str):
            observations = [{**json.loads(last), "result": None}]
        else:
            observations = [json.loads(block["content"]) for block in last]
        actions = [self.policy(observation) for observation in observations[-1:]]
        if len(self.requests) == 1:
            # The first five reference actions are read-only inspections; issue two at once.
            actions.append(self.policy({"step": 1, "result": {"tool": actions[0]["tool"], "ok": True, "data": {}}}))
        content = [types.SimpleNamespace(type="thinking", thinking="")]
        content += [types.SimpleNamespace(type="tool_use", id=f"toolu_{len(self.requests)}_{i}",
                                          name=a["tool"], input=a["arguments"]) for i, a in enumerate(actions)]
        return types.SimpleNamespace(content=content, stop_reason="tool_use")


class LlmAgentExampleTests(unittest.TestCase):
    def test_tool_calling_loop_matches_the_api_contract(self):
        agent = ClaudeAgent()
        agent.client = types.SimpleNamespace(messages=StubMessages())
        with LiveAirlineEnv() as env:
            observation, _ = env.reset(seed=1, options={"split": "train", "index": 2})
            done, steps = False, 0
            while not done:
                observation, _, terminated, truncated, info = env.step(agent(observation))
                done, steps = terminated or truncated, steps + 1
        requests = agent.client.messages.requests
        self.assertTrue(terminated)
        self.assertEqual(steps, len(requests) + 1)  # one turn carried two tool calls
        first = requests[0]
        self.assertEqual({tool["name"] for tool in first["tools"]}, {tool["name"] for tool in LiveAirlineEnv.tools()})
        self.assertTrue(all(set(tool) == {"name", "description", "input_schema"} for tool in first["tools"]))
        self.assertIn("Mission:", first["system"])
        # Every tool_use of an assistant turn is answered in the next user message, in order.
        transcript = requests[-1]["messages"]
        for assistant, user in zip(transcript[1::2], transcript[2::2]):
            issued = [block.id for block in assistant["content"] if block.type == "tool_use"]
            self.assertEqual(issued, [block["tool_use_id"] for block in user["content"]])
            self.assertEqual(assistant["content"][0].type, "thinking")  # replayed unchanged

    def test_model_that_stops_calling_tools_finishes_the_episode(self):
        agent = ClaudeAgent()
        agent.client = types.SimpleNamespace(messages=types.SimpleNamespace(
            create=lambda **request: types.SimpleNamespace(content=[], stop_reason="end_turn")))
        action = agent({"step": 0, "alerts": [], "summary": {}, "available_tools": [], "mission": "",
                        "episode_contract": {}, "configuration_contracts": {}})
        self.assertEqual(action, {"tool": "finish", "arguments": {}})


if __name__ == "__main__":
    unittest.main()


class OpenAICompatibleStubTests(unittest.TestCase):
    """A stub chat-completions server answers like the reference policy."""

    def test_tool_calls_round_trip_through_the_chat_completions_shape(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from examples.openai_compatible_agent import OpenAICompatibleAgent

        policy, requests = ReferencePolicy(), []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                last = body["messages"][-1]
                observation = {**json.loads(last["content"]), "result": None} if last["role"] == "user" \
                    else json.loads(last["content"])
                action = policy(observation)
                reply = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call_{len(requests)}", "type": "function",
                     "function": {"name": action["tool"], "arguments": json.dumps(action["arguments"])}}]}}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)  # cleanups run last-in first-out: shut down, then close the socket
        self.addCleanup(server.shutdown)
        agent = OpenAICompatibleAgent(model="stub", base_url=f"http://127.0.0.1:{server.server_address[1]}/v1", api_key="x")
        with LiveAirlineEnv() as env:
            observation, _ = env.reset(seed=1, options={"split": "train", "index": 2})
            done = False
            while not done:
                observation, _, terminated, truncated, info = env.step(agent(observation))
                done = terminated or truncated
        self.assertTrue(info["score"]["success"])
        self.assertEqual(agent.usage["requests"], len(requests))
        transcript = requests[-1]["messages"]
        self.assertEqual(transcript[0]["role"], "system")
        self.assertTrue(all(tool["type"] == "function" for tool in requests[0]["tools"]))
        for assistant, tool in zip(transcript[2::2], transcript[3::2]):
            self.assertEqual(assistant["tool_calls"][0]["id"], tool["tool_call_id"])
