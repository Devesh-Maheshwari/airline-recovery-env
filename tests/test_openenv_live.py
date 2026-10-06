"""Native OpenEnv integration: real uvicorn, sockets, workers, and typed client.

Run with: python -m unittest discover -s tests -p test_openenv_live.py -v
These tests skip only when the optional OpenEnv dependency is not installed.
"""

import asyncio
import importlib.util
import json
import os
import socket
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


OPENENV_INSTALLED = importlib.util.find_spec("openenv") is not None
if OPENENV_INSTALLED:
    import uvicorn
    from pydantic import ValidationError

    from airline_recovery.openenv_adapter import AirlineAction, AirlineEnv, AirlineState
    from airline_recovery.openenv_adapter.environment import AirlineEnvironment
    from airline_recovery.openenv_adapter.server import build_app


@unittest.skipUnless(OPENENV_INSTALLED, "Install airline-recovery-env[openenv] to test the native integration")
class OpenEnvLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_web = os.environ.get("ENABLE_WEB_INTERFACE")
        cls._old_analytics = os.environ.get("GRADIO_ANALYTICS_ENABLED")
        os.environ["ENABLE_WEB_INTERFACE"] = "true"
        os.environ["GRADIO_ANALYTICS_ENABLED"] = "false"
        cls.app = build_app(max_sessions=4, max_steps=12)
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(128)
        cls.port = cls.sock.getsockname()[1]
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.server = uvicorn.Server(uvicorn.Config(cls.app, log_level="error", lifespan="on"))
        cls.thread = threading.Thread(
            target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True
        )
        cls.thread.start()
        deadline = time.monotonic() + 30
        while not cls.server.started and time.monotonic() < deadline and cls.thread.is_alive():
            time.sleep(0.02)
        if not cls.server.started:
            cls.server.should_exit = True
            cls.thread.join(timeout=5)
            cls.sock.close()
            raise RuntimeError("The native OpenEnv server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=20)
        cls.sock.close()
        for name, value in (
            ("ENABLE_WEB_INTERFACE", cls._old_web),
            ("GRADIO_ANALYTICS_ENABLED", cls._old_analytics),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if cls.thread.is_alive():
            raise RuntimeError("The OpenEnv server did not cleanly shut down")
        if any(not environment._closed for environment in cls.app.state.airline_environments):
            raise AssertionError("Server shutdown left a live environment open")

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(
            self.url + path, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def test_native_schema_discovery_and_web_playground(self):
        schema = self.request("/schema")
        self.assertIn("tool", schema["action"]["properties"])
        self.assertIn("alerts", schema["observation"]["properties"])
        self.assertEqual(set(schema["state"]["properties"]), {"episode_id", "step_count", "done"})
        self.assertEqual(self.request("/list_environments"), ["airline_recovery"])
        splits = self.request("/airline_recovery/splits")
        self.assertEqual({item["name"] for item in splits}, {"train", "eval", "test"})
        for split in ("train", "eval", "test"):
            count = self.request("/airline_recovery/num_tasks", {"split": split})["num_tasks"]
            self.assertGreater(count, 0)
            task = self.request("/airline_recovery/task", {"split": split, "index": 0})["task"]
            self.assertEqual(task["index"], 0)
            self.assertTrue({"fault", "solution", "baseline", "private_seed"}.isdisjoint(task))
            tasks = self.request("/airline_recovery/tasks", {"split": split})["tasks"]
            self.assertEqual(len(tasks), count)
        with self.assertRaises(HTTPError) as raised:
            self.request("/airline_recovery/task", {"split": "train", "index": 999999})
        self.assertEqual(raised.exception.code, 400)
        with urlopen(self.url + "/web/", timeout=15) as response:
            self.assertEqual(response.status, 200)
            playground = response.read().decode()
            self.assertIn("gradio", playground.lower())
            self.assertIn("from airline_recovery.openenv_adapter import AirlineAction, AirlineEnv", playground)
            self.assertNotIn("from airline_recovery import", playground)
        # Exercise the native playground's own environment, so server shutdown
        # is also required to clean up its workers rather than an unused shell.
        reset = self.request("/web/reset", {"seed": 19, "split": "train", "index": 0})
        self.assertTrue(reset["observation"]["episode_id"])
        observed = self.request("/web/step", {"action": {"tool": "get_metrics", "arguments": {}}})
        self.assertTrue(observed["observation"]["result"]["ok"])

    def test_real_websocket_isolation_reward_state_and_cleanup(self):
        async def exercise():
            async with AirlineEnv(base_url=self.url) as first, AirlineEnv(base_url=self.url) as second:
                a, b = await asyncio.gather(
                    first.reset(seed=13, split="train", index=0),
                    second.reset(seed=13, options={"split": "train", "index": 0}),
                )
                self.assertNotEqual(a.observation.episode_id, b.observation.episode_id)
                self.assertTrue(a.observation.available_tools)
                self.assertEqual(a.observation.episode_contract["verification_eligible_from_step"], 6)
                self.assertTrue(a.observation.configuration_contracts["payment"]["observed_fields"]["provider_latency_ms"]["readOnly"])
                self.assertFalse(a.done)
                live = [
                    environment for environment in self.app.state.airline_environments
                    if environment.state.episode_id in {
                        a.observation.episode_id, b.observation.episode_id
                    }
                ]
                self.assertEqual(len(live), 2)
                before = await second.step(AirlineAction(tool="get_config", arguments={"service": "booking"}))
                changed = await first.step(AirlineAction(
                    tool="patch_config",
                    arguments={"service": "booking", "values": {"payment_timeout_ms": 111}},
                ))
                self.assertTrue(changed.observation.result["ok"])
                after = await second.step(AirlineAction(tool="get_config", arguments={"service": "booking"}))
                self.assertEqual(before.observation.result["data"], after.observation.result["data"])
                first_state, second_state = await asyncio.gather(first.state(), second.state())
                self.assertEqual(first_state.step_count, 1)
                self.assertEqual(second_state.step_count, 2)
                self.assertEqual(set(first_state.model_dump()), {"episode_id", "step_count", "done"})
                final = await first.step(AirlineAction(tool="finish"))
                self.assertTrue(final.done)
                self.assertTrue(final.observation.done)
                self.assertTrue(final.observation.terminated)
                self.assertIsInstance(final.reward, float)
                self.assertIn("score", final.metadata)
                self.assertEqual(final.observation.metadata, final.metadata)
                self.assertTrue((await first.state()).done)
            deadline = time.monotonic() + 10
            while any(not environment._closed for environment in live) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            self.assertTrue(all(environment._closed for environment in live))

        asyncio.run(exercise())

    def test_sync_client_and_reset_validation(self):
        with AirlineEnv(base_url=self.url).sync() as client:
            initial = client.reset(seed=7, split="eval", index=0)
            self.assertFalse(initial.done)
            observed = client.step(AirlineAction(tool="get_metrics"))
            self.assertTrue(observed.observation.result["ok"])
            self.assertEqual(client.state().step_count, 1)
            self.assertTrue(client.step(AirlineAction(tool="finish")).done)
        with self.assertRaises(ValidationError):
            AirlineAction(tool="delete_database")
        with self.assertRaises(ValidationError):
            AirlineState(secret_fault="hidden")
        environment = AirlineEnvironment()
        try:
            from airline_recovery import __version__
            self.assertEqual(environment.get_metadata().version, __version__)
            with self.assertRaises(ValidationError):
                environment.reset(options={"split": "train", "index": -1})
            with self.assertRaises(ValidationError):
                environment.reset(seed=True)
            with self.assertRaises(ValueError):
                environment.reset(unknown_option="typo")
        finally:
            environment.close()

    def test_native_browser_form_parses_json_arguments(self):
        from gradio_client import Client

        client = Client(self.url + "/web/", verbose=False)
        self.addCleanup(client.close)
        reset = client.predict(api_name="/reset_env")
        self.assertEqual(reset[2], "Environment reset successfully.")
        observed = client.predict("get_config", '{"service":"booking"}', api_name="/step_form")
        self.assertEqual(observed[2], "Step complete.")
        self.assertTrue(json.loads(observed[1])["observation"]["result"]["ok"])
        before = json.loads(client.predict(api_name="/get_state_sync"))
        invalid = client.predict("get_metrics", "[]", api_name="/step_form")
        self.assertIn("Arguments must be a JSON object", invalid[2])
        self.assertEqual(json.loads(client.predict(api_name="/get_state_sync")), before)
        metrics = client.predict("get_metrics", "{}", api_name="/step_form")
        self.assertEqual(metrics[2], "Step complete.")
        self.assertTrue(json.loads(metrics[1])["observation"]["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
