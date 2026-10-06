"""Release-review regressions exercising real workers and hostile tool inputs.

Trusted database edits below inject faults for the independent reviewer. They
are not capabilities exposed through the agent's read-only SQL tool.
"""
import json
import unittest

from airline_recovery.live.environment import LiveAirlineEnv
from airline_recovery.live.runtime import LiveStack
from airline_recovery.live.verification import assess


def act(environment, tool, **arguments):
    return environment.step({"tool": tool, "arguments": arguments})


class LiveAdversarialBackendTests(unittest.TestCase):
    def setUp(self):
        self.stack = LiveStack().start(seed=31)
        self.addCleanup(self.stack.close)

    def book(self, request_id="review-request"):
        return self.stack.request("booking", "POST", "/book", {
            "request_id": request_id, "flight_id": "F100", "passenger_id": "review-passenger",
        })

    def test_sql_cannot_reach_private_endpoints_or_files_through_alternate_forms(self):
        blocked = (
            "WITH private AS (SELECT * FROM service_endpoints) SELECT * FROM private",
            "SELECT * FROM pragma_database_list",
            "SELECT load_extension('/tmp/not-an-extension')",
            "SELECT readfile('/etc/passwd')",
            "CREATE TEMP TABLE copied AS SELECT * FROM bookings",
            "WITH n AS (SELECT 1) DELETE FROM flights WHERE flight_id='F100'",
        )
        for query in blocked:
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.stack.query(query)
        self.assertEqual(len(self.stack.query("SELECT * FROM flights")), 2)

    def test_sql_bounds_scalar_allocations_not_only_instruction_count(self):
        # Two MiB is a safe regression payload, not an attempt to exhaust RAM.
        for query in (
            "SELECT zeroblob(2097152) AS payload",
            "SELECT printf('%2097152s', 'x') AS payload",
        ):
            with self.subTest(query=query):
                try:
                    rows = self.stack.query(query)
                except ValueError:
                    continue
                # SQLite may return NULL for a format exceeding its length
                # limit. A bounded JSON result is as safe as rejection.
                self.assertLessEqual(len(json.dumps(rows).encode()), 262144)
        self.assertEqual(self.stack.query("SELECT COUNT(*) AS n FROM flights"), [{"n": 2}])

    def test_sql_binary_values_do_not_escape_as_unserializable_python_bytes(self):
        try:
            rows = self.stack.query("SELECT X'535741' AS value")
        except ValueError:
            return  # Explicitly rejecting binary diagnostics is also valid.
        json.dumps(rows)

    def test_sql_nonfinite_numbers_are_rejected_or_valid_json(self):
        try:
            rows = self.stack.query("SELECT 1e999 AS overflow")
        except ValueError:
            return
        json.dumps(rows, allow_nan=False)

    def test_sql_bounds_total_output_across_individually_small_cells(self):
        with self.assertRaises(ValueError):
            self.stack.query("SELECT printf('%65536s', 'x') AS payload "
                             "FROM flights a, flights b, flights c, flights d")
        self.assertEqual(self.stack.query("SELECT 1 AS alive"), [{"alive": 1}])

    def test_sql_computation_budget_aborts_expensive_cross_join_and_recovers(self):
        aliases = ", ".join(f"flights f{i}" for i in range(22))
        with self.assertRaises(ValueError):
            self.stack.query(f"SELECT COUNT(*) AS total FROM {aliases}")
        self.assertEqual(self.stack.query("SELECT 1 AS alive"), [{"alive": 1}])

    def test_unavailable_upstream_produces_failed_caller_trace_without_inventing_worker_logs(self):
        self.stack.stop_service("payment")
        result = self.book()
        self.assertEqual(result["status"], 503)
        matching = [row for row in self.stack.logs(limit=100)
                    if row["trace_id"] == result["trace_id"]]
        self.assertEqual({row["service"] for row in matching}, {"booking", "pricing", "inventory"})
        booking = next(row for row in matching if row["service"] == "booking")
        self.assertEqual(booking["status"], 503)
        self.assertNotEqual(booking["message"], "ok")
        self.assertFalse(self.stack.metrics("payment")["services"]["payment"]["running"])
        self.assertEqual(self.stack.inspect()["charges"], [])

    def test_accepted_fare_survives_later_price_change_in_outcome_grader(self):
        self.stack.patch_config("booking", {"payment_timeout_ms": 1})
        self.assertEqual(self.book()["status"], 504)
        row = self.stack.inspect()["bookings"][0]
        requirement = {key: row[key] for key in ("request_id", "flight_id", "passenger_id", "amount_cents")}
        self.stack.admin_execute("UPDATE flights SET price_cents=price_cents+7300,version=version+1 WHERE flight_id='F100'")
        self.stack.patch_config("booking", {"payment_timeout_ms": 200})
        self.assertEqual(self.stack.reconcile(row["booking_id"])["status"], 200)
        self.assertEqual(self.stack.pump()["delivered"], 1)
        state = self.stack.inspect()
        outcome = assess(state, {row["request_id"]: requirement}, set(), {})
        self.assertTrue(outcome["integrity"], outcome)
        self.assertEqual(outcome["recovery"], 1.0)
        self.assertEqual(state["charges"][0]["amount_cents"], requirement["amount_cents"])

    def test_boolean_schema_is_malformed_and_can_be_quarantined_without_integrity_penalty(self):
        self.assertEqual(self.book()["status"], 200)
        original = self.stack.inspect()["outbox"][0]
        malformed = json.loads(original["payload"])
        malformed["schema_version"] = True
        self.stack.admin_execute("INSERT INTO outbox VALUES(?,?,?,'pending',0)",
                                 ("000-review-bool", original["booking_id"], json.dumps(malformed)))
        blocked = self.stack.pump()
        self.assertEqual(blocked["blocked_event_id"], "000-review-bool")
        self.assertIn("schema_version", blocked["error"])
        self.stack.quarantine("000-review-bool")
        self.assertEqual(self.stack.pump()["delivered"], 1)
        outcome = assess(self.stack.inspect(), {}, set(), {})
        self.assertTrue(outcome["integrity"], outcome)


class LiveAdversarialEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.environment = LiveAirlineEnv()
        self.addCleanup(self.environment.close)

    def test_stopped_projection_cannot_succeed_by_accepting_only_new_sales(self):
        self.environment.reset(seed=31, options={"split": "train", "index": 1})
        for _ in range(7):
            act(self.environment, "probe")
        _, reward, _, _, info = act(self.environment, "finish")
        score = info["score"]
        self.assertFalse(score["success"])
        self.assertFalse(score["verified"])
        self.assertLess(score["recovery"], 1)
        self.assertLess(reward, 1)
        self.assertTrue(score["details"]["incomplete_request_ids"])

    def test_disabling_capacity_really_oversells_and_blocks_reward(self):
        self.environment.reset(seed=31, options={"split": "train", "index": 1})
        act(self.environment, "patch_config", service="inventory", values={"enforce_capacity": False})
        self.assertGreater(len(self.environment.stack.query("SELECT * FROM holds WHERE flight_id='F900'")), 1)
        for _ in range(5):
            act(self.environment, "probe")
        _, reward, _, _, info = act(self.environment, "finish")
        self.assertEqual(reward, 0)
        self.assertFalse(info["score"]["integrity"])
        self.assertIn("oversold:F900", info["score"]["details"]["violations"])
        self.assertTrue(any(value.startswith("rejected_request_fulfilled:")
                            for value in info["score"]["details"]["violations"]))

    def test_orphan_payment_evidence_blocks_reward_even_after_service_recovery(self):
        self.environment.reset(seed=31, options={"split": "train", "index": 1})
        self.environment.stack.admin_execute("INSERT INTO charges VALUES(?,?,?,?)",
                                              ("review-charge", "nonexistent-booking", "review-key", 20000))
        act(self.environment, "patch_config", service="checkin", values={"consumer_enabled": True})
        for _ in range(7):
            act(self.environment, "probe")
        _, reward, _, _, info = act(self.environment, "finish")
        self.assertEqual(reward, 0)
        self.assertIn("orphan_charge:nonexistent-booking", info["score"]["details"]["violations"])

    def test_reset_terminates_old_workers_removes_storage_and_clears_violations(self):
        initial, _ = self.environment.reset(seed=31, options={"split": "train", "index": 1})
        old_stack = self.environment.stack
        processes = list(old_stack._processes.values())
        directory = old_stack._database().parent
        marker = old_stack.request("booking", "POST", "/book", {
            "request_id": "review-previous-episode-only", "flight_id": "F200", "passenger_id": "review-only",
        })
        self.assertEqual(marker["status"], 200)
        act(self.environment, "patch_config", service="inventory", values={"enforce_capacity": False})
        self.assertTrue(self.environment.violations_seen)
        reset, _ = self.environment.reset(seed=31, options={"split": "train", "index": 1})
        self.assertNotEqual(initial["episode_id"], reset["episode_id"])
        self.assertEqual(initial["summary"], reset["summary"])
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertFalse(directory.exists())
        self.assertFalse(self.environment.violations_seen)
        self.assertNotIn(marker["body"]["booking_id"],
                         {row["booking_id"] for row in self.environment.stack.inspect()["bookings"]})

    def test_largest_supported_horizon_has_room_for_required_new_customers(self):
        self.environment.close()
        self.environment = LiveAirlineEnv(max_steps=256)
        self.addCleanup(self.environment.close)
        self.environment.reset(seed=31, options={"split": "train", "index": 1})
        state = self.environment.stack.inspect()
        for flight in state["flights"]:
            if flight["flight_id"] in {"F100", "F200"}:
                occupied = sum(row["flight_id"] == flight["flight_id"] for row in state["holds"])
                self.assertGreaterEqual(flight["capacity"] - occupied, self.environment.max_steps)
        sold_out = next(row for row in state["flights"] if row["flight_id"] == "F900")
        self.assertEqual(sold_out["capacity"], 1)


if __name__ == "__main__":
    unittest.main()
