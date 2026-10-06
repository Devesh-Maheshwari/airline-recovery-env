"""Integration checks run real loopback servers in separate operating-system processes."""
from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from airline_recovery.live.runtime import LiveStack


class LiveBackendTests(unittest.TestCase):
    def setUp(self):
        self.stack = LiveStack().start(seed=17)
        self.addCleanup(self.stack.close)

    def book(self, request_id="request-1", **extra):
        return self.stack.request("booking", "POST", "/book", {"request_id": request_id, "flight_id": "F100", "passenger_id": "P1", **extra})

    def test_end_to_end_is_durable_and_traced_across_processes(self):
        result = self.book()
        self.assertEqual(result["status"], 200, result)
        self.assertEqual(self.stack.pump()["delivered"], 1)
        self.stack.restart("booking")
        self.stack.restart("checkin")
        self.assertEqual(self.book()["body"], result["body"])
        state = self.stack.inspect()
        for table in ("bookings", "holds", "charges", "outbox", "checkins"):
            self.assertEqual(len(state[table]), 1, table)
        self.assertEqual(len({process.pid for process in self.stack._processes.values()}), 5)
        trace_services = {row["service"] for row in self.stack.logs(limit=100) if row["trace_id"] == result["trace_id"]}
        self.assertEqual(trace_services, {"booking", "pricing", "inventory", "payment"})

    def test_timeout_commits_charge_and_retry_reuses_it(self):
        self.stack.patch_config("booking", {"payment_timeout_ms": 10})
        first, retry = self.book(), self.book()
        self.assertEqual([first["status"], retry["status"]], [504, 504])
        state = self.stack.inspect()
        self.assertEqual(len(state["charges"]), 1)
        self.assertEqual(len(state["holds"]), 1)
        self.assertEqual(state["bookings"][0]["status"], "pending")
        self.assertEqual(state["outbox"], [])
        self.stack.patch_config("booking", {"payment_timeout_ms": 200})
        self.assertEqual(self.stack.reconcile(state["bookings"][0]["booking_id"])["status"], 200)
        self.assertEqual(self.stack.pump()["delivered"], 1)
        self.assertEqual(len(self.stack.inspect()["charges"]), 1)

    def test_pending_retry_preserves_original_accepted_fare(self):
        self.stack.patch_config("booking", {"payment_timeout_ms": 10})
        self.assertEqual(self.book()["status"], 504)
        self.stack.admin_execute("UPDATE flights SET price_cents=price_cents+1000,version=version+1 WHERE flight_id='F100'")
        self.stack.patch_config("booking", {"payment_timeout_ms": 200})
        self.assertEqual(self.book()["status"], 200)
        state = self.stack.inspect()
        self.assertEqual(state["bookings"][0]["amount_cents"], 20000)
        self.assertEqual(state["charges"][0]["amount_cents"], 20000)
        self.assertEqual(len(state["charges"]), 1)
        self.assertEqual(self.book("new-request")["status"], 409)

    def test_unsafe_idempotency_options_cause_actual_duplicate_charges(self):
        self.stack.patch_config("booking", {"payment_timeout_ms": 10, "payment_idempotency_enabled": False})
        self.book()
        self.book()
        self.assertEqual(len(self.stack.inspect()["charges"]), 2)
        self.stack.patch_config("booking", {"payment_idempotency_enabled": True})
        self.stack.patch_config("payment", {"idempotency_enabled": False})
        self.book("another-request")
        self.book("another-request")
        self.assertEqual(len(self.stack.inspect()["charges"]), 4)

    def test_concurrent_booking_retries_cannot_duplicate_side_effects(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.book(), range(8)))
        self.assertTrue(all(result["status"] == 200 for result in responses), responses)
        self.assertEqual(len({result["body"]["booking_id"] for result in responses}), 1)
        state = self.stack.inspect()
        for table in ("bookings", "holds", "charges", "outbox"):
            self.assertEqual(len(state[table]), 1, table)

    def test_capacity_guard_is_transactional_and_unsafe_switch_has_effect(self):
        self.stack.admin_execute("UPDATE flights SET capacity=1 WHERE flight_id='F100'")
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda i: self.book(f"r-{i}", passenger_id=f"P{i}"), range(4)))
        self.assertEqual(sorted(result["status"] for result in responses), [200, 409, 409, 409])
        state = self.stack.inspect()
        self.assertEqual(len(state["holds"]), 1)
        self.assertEqual(len(state["charges"]), 1)
        self.assertEqual(len(state["bookings"]), 1)
        self.stack.patch_config("inventory", {"enforce_capacity": False})
        self.assertEqual(self.book("oversell", passenger_id="P-extra")["status"], 200)
        self.assertEqual(len(self.stack.inspect()["holds"]), 2)

    def test_stale_quote_requires_cache_repair_and_bypass_really_undercharges(self):
        self.assertEqual(self.stack.request("pricing", "GET", "/quote?flight_id=F100")["status"], 200)
        self.stack.admin_execute("UPDATE flights SET price_cents=21000,version=2 WHERE flight_id='F100'")
        self.assertEqual(self.book()["status"], 409)
        self.assertEqual(self.stack.inspect()["charges"], [])
        self.stack.patch_config("booking", {"validate_price": False})
        self.assertEqual(self.book("unsafe")["status"], 200)
        self.assertEqual(self.stack.inspect()["charges"][0]["amount_cents"], 20000)
        self.stack.patch_config("booking", {"validate_price": True})
        self.assertEqual(self.stack.invalidate_cache("pricing")["cleared"], 1)
        self.assertEqual(self.book()["status"], 200)
        self.assertEqual(self.stack.inspect()["charges"][1]["amount_cents"], 21000)

    def test_poison_event_blocks_backlog_until_targeted_quarantine(self):
        result = self.book()
        booking_id = result["body"]["booking_id"]
        self.stack.admin_execute("INSERT INTO outbox VALUES (?,?,?,'pending',0)", ("000-poison", booking_id, "{unparseable"))
        pump = self.stack.pump()
        self.assertEqual(pump["blocked_event_id"], "000-poison")
        self.assertEqual(pump["delivered"], 0)
        self.assertEqual(self.stack.inspect()["checkins"], [])
        self.stack.quarantine("000-poison")
        self.assertEqual(self.stack.pump()["delivered"], 1)
        state = self.stack.inspect()
        poison = next(row for row in state["outbox"] if row["event_id"] == "000-poison")
        self.assertEqual(poison["payload"], "{unparseable")
        self.assertEqual(poison["attempts"], 1)
        self.assertEqual(len(state["checkins"]), 1)

    def test_schema_compatibility_and_disabled_consumer(self):
        self.book()
        event = self.stack.inspect()["outbox"][0]
        payload = json.loads(event["payload"])
        payload["schema_version"] = 2
        self.stack.admin_execute("UPDATE outbox SET payload=?", (json.dumps(payload),))
        self.assertEqual(self.stack.pump()["delivered"], 0)
        self.stack.patch_config("checkin", {"accepted_schema": 2, "consumer_enabled": False})
        self.assertEqual(self.stack.pump()["delivered"], 0)
        self.stack.patch_config("checkin", {"consumer_enabled": True})
        self.assertEqual(self.stack.pump()["delivered"], 1)
        self.stack.admin_execute("UPDATE outbox SET status='pending'")
        self.assertEqual(self.stack.pump()["delivered"], 1)
        self.assertEqual(len(self.stack.inspect()["checkins"]), 1)

    def test_service_crash_causes_real_transport_failure_and_recovers(self):
        self.stack.stop_service("payment")
        self.assertEqual(self.stack.request("payment", "GET", "/health")["status"], 503)
        self.assertEqual(self.book()["status"], 503)
        self.assertFalse(self.stack.metrics()["services"]["payment"]["running"])
        self.stack.restart("payment")
        self.assertEqual(self.book()["status"], 200)
        self.assertEqual(len(self.stack.inspect()["charges"]), 1)

    def test_configuration_validation_is_atomic(self):
        original = self.stack.get_config("booking")
        for values in ({}, {"payment_timeout_ms": 400, "validate_price": 1}, {"payment_timeout_ms": -1}, {"payment_timeout_ms": 10001}, {"no_such_field": True}):
            with self.assertRaises(ValueError):
                self.stack.patch_config("booking", values)
            self.assertEqual(self.stack.get_config("booking"), original)
        with self.assertRaises(ValueError):
            self.stack.patch_config("checkin", {"batch_size": True})

    def test_sql_diagnostics_are_bounded_and_cannot_mutate_or_read_internal_urls(self):
        self.assertEqual(self.stack.query("SELECT COUNT(*) AS total FROM flights"), [{"total": 2}])
        metadata = self.stack.query("SELECT name,sql FROM sqlite_master WHERE type='table'")
        self.assertIn("flights", {row["name"] for row in metadata})
        self.assertTrue(all(isinstance(row["sql"], str) for row in metadata))
        bounded = self.stack.query("SELECT a.flight_id FROM flights a, flights b, flights c, flights d, flights e, flights f, flights g, flights h, flights i, flights j")
        self.assertEqual(len(bounded), 500)
        for query in ("DELETE FROM flights", "PRAGMA database_list", "ATTACH DATABASE ':memory:' AS extra", "SELECT * FROM service_endpoints", "SELECT 1; SELECT 2"):
            with self.assertRaises(ValueError, msg=query):
                self.stack.query(query)
        self.assertEqual(len(self.stack.query("SELECT * FROM flights")), 2)

    def test_identity_conflicts_reject_without_new_work(self):
        self.assertEqual(self.book()["status"], 200)
        self.assertEqual(self.book(passenger_id="someone-else")["status"], 409)
        state = self.stack.inspect()
        self.assertEqual(len(state["charges"]), 1)
        charge = state["charges"][0]
        result = self.stack.request("payment", "POST", "/capture", {"booking_id": charge["booking_id"], "idempotency_key": charge["idempotency_key"], "amount_cents": charge["amount_cents"] + 1, "timeout_ms": 200})
        self.assertEqual(result["status"], 409)
        self.assertEqual(len(self.stack.inspect()["charges"]), 1)


if __name__ == "__main__":
    unittest.main()
