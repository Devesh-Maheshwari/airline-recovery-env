"""Process lifecycle and scoring edge cases found in code review."""
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from airline_recovery.live.environment import LiveAirlineEnv
from airline_recovery.live.policies import ReferencePolicy
from airline_recovery.live.runtime import LiveStack

ROOT = Path(__file__).resolve().parents[1]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class LiveRobustnessTests(unittest.TestCase):
    def test_terminal_score_is_stable_and_leaves_no_backlog(self):
        policy = ReferencePolicy()
        with LiveAirlineEnv() as env:
            observation, _ = env.reset(seed=1, options={"split":"train", "index":1})
            while not env.done:
                observation, _, _, _, info = env.step(policy(observation))
            self.assertTrue(info["score"]["success"])
            self.assertTrue(info["score"]["details"]["safety_check"]["ran"])
            self.assertEqual(env._score(), info["score"])
            self.assertEqual(observation["summary"]["outbox_pending"], 0)
            self.assertEqual(observation["alerts"], [])

    @unittest.skipIf(os.name == "nt", "POSIX process signals")
    def test_workers_exit_when_their_parent_is_killed(self):
        script = ("import json,sys,time\nfrom airline_recovery.live.runtime import LiveStack\ns=LiveStack().start()\n"
                  "print(json.dumps([p.pid for p in s._processes.values()]),flush=True)\ntime.sleep(60)\n")
        parent = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                                  env={**os.environ, "PYTHONPATH":str(ROOT)})
        try:
            workers = json.loads(parent.stdout.readline())
            self.assertEqual(len(workers), 5)
            parent.kill()
            parent.wait(timeout=10)
            deadline = time.monotonic() + 10
            while any(alive(pid) for pid in workers) and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertFalse([pid for pid in workers if alive(pid)])
        finally:
            parent.kill()
            parent.stdout.close()

    def test_stack_dropped_without_close_stops_its_workers(self):
        stack = LiveStack().start()
        processes = list(stack._processes.values())
        temporary = stack._temporary
        del stack
        self.addCleanup(temporary.cleanup)
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_workers_reject_requests_from_outside_their_episode(self):
        with LiveStack() as stack:
            url = stack._urls["pricing"] + "/quote?flight_id=F100"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with self.assertRaises(urllib.error.HTTPError) as raised:
                opener.open(url, timeout=5)
            self.assertEqual(raised.exception.code, 403)
            self.assertEqual(stack.request("pricing", "GET", "/quote?flight_id=F100")["status"], 200)

    def test_sql_tool_works_from_a_temp_directory_with_uri_characters(self):
        with tempfile.TemporaryDirectory(prefix="odd#dir?%41-") as odd:
            previous = tempfile.tempdir
            tempfile.tempdir = odd
            try:
                with LiveStack() as stack:
                    self.assertEqual(len(stack.query("SELECT flight_id FROM flights")), 2)
            finally:
                tempfile.tempdir = previous

    def test_unparseably_deep_action_is_an_ordinary_rejected_step(self):
        deep = {}
        for _ in range(100000):
            deep = {"a":deep}
        with LiveAirlineEnv() as env:
            env.reset(seed=1, options={"split":"train", "index":1})
            observation, _, terminated, truncated, _ = env.step({"tool":"patch_config", "arguments":{"service":"booking", "values":deep}})
            self.assertFalse(observation["result"]["ok"])
            self.assertEqual(observation["step"], 1)
            self.assertFalse(terminated or truncated)

    def test_unrepaired_outage_keeps_step_time_bounded(self):
        with LiveAirlineEnv() as env:
            env.reset(seed=1, options={"split":"train", "index":3})
            durations = []
            for _ in range(30):
                started = time.monotonic()
                env.step({"tool":"get_metrics", "arguments":{}})
                durations.append(time.monotonic() - started)
            # The median of the last five steps: one step stalled by a busy machine is not unbounded growth.
            self.assertLess(statistics.median(durations[-5:]), 2.0, durations)

    def test_a_worker_stalled_past_five_seconds_still_answers(self):
        # Reset-time calibration expects one exact status; a 5 s socket timeout turned a stalled
        # (not failed) worker into a 503 and a random reset error. SQLite alone may wait 10 s.
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from airline_recovery.live import store, worker
        with tempfile.TemporaryDirectory() as directory, store.connect(Path(directory, "busy.sqlite3")) as db:
            busy_ms = db.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertGreater(worker.LOOPBACK_TIMEOUT_S * 1000, busy_ms)

        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                time.sleep(5.5)
                data = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status, body = worker.http_request(f"http://127.0.0.1:{server.server_address[1]}/slow", "GET", None, "trace")
        self.assertEqual((status, body), (200, {"ok": True}))


class WorkspaceGuardTests(unittest.TestCase):
    def test_step_fails_clearly_when_the_episode_database_is_gone(self):
        with LiveAirlineEnv() as env:
            env.reset(seed=1, options={"split": "train", "index": 1})
            env.stack._db_path.unlink()
            with self.assertRaisesRegex(RuntimeError, "workspace unavailable"):
                env.step({"tool": "get_metrics", "arguments": {}})
            self.assertEqual(env.step_count, 0)


if __name__ == "__main__":
    unittest.main()
