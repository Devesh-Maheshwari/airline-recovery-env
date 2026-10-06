"""Hard-tier worker and runtime semantics, exercised on real workers without an episode.

Trusted database edits stand in for the hard-tier injector: provider plans, fare
holds and the episode clock are rows, never a tier flag reaching a worker.
"""
from __future__ import annotations

import json
import unittest
import uuid

from airline_recovery.live.runtime import LiveStack
from airline_recovery.live.store import configuration_contracts


class HardBackendTests(unittest.TestCase):
    def setUp(self):
        self.stack = LiveStack().start(seed=41)
        self.addCleanup(self.stack.close)

    def book(self, request_id="hard-request", flight_id="F100", **extra):
        return self.stack.request("booking", "POST", "/book", {"request_id": request_id, "flight_id": flight_id, "passenger_id": "P1", **extra})

    def capture(self, booking_id, key, amount=20000, timeout_ms=200):
        return self.stack.request("payment", "POST", "/capture", {"booking_id": booking_id, "idempotency_key": key, "amount_cents": amount, "timeout_ms": timeout_ms})

    def plan(self, key, outcome, final_state=None, settles_at_step=None):
        self.stack.admin_execute("INSERT INTO provider_plan(idempotency_key,outcome,final_state,settles_at_step) VALUES (?,?,?,?)", (key, outcome, final_state, settles_at_step))

    def settle(self):
        result = self.stack.request("payment", "POST", "/settle")
        self.assertEqual(result["status"], 200, result)
        return result["body"]["settled"]

    def ledger(self, **where):
        clause = " AND ".join(f"{k}=?" for k in where) or "1"
        return self.stack.lookup(f"SELECT * FROM provider_ledger WHERE {clause} ORDER BY rowid", tuple(where.values()))

    def pending_booking(self, request_id="hard-request"):
        """A booking interrupted the ordinary way: a lost ACK while the provider captured."""
        self.stack.patch_config("booking", {"payment_timeout_ms": 10})
        self.assertEqual(self.book(request_id)["status"], 504)
        self.stack.patch_config("booking", {"payment_timeout_ms": 200})
        return next(row for row in self.stack.inspect()["bookings"] if row["request_id"] == request_id)

    # --- data model -------------------------------------------------------

    def test_default_rows_reproduce_easy_tier_shapes_and_private_tables_are_denied(self):
        self.assertEqual(self.book()["status"], 200)
        state = self.stack.inspect()
        charge, booking = state["charges"][0], state["bookings"][0]
        self.assertEqual((charge["state"], charge["created_step"]), ("captured", 0))
        self.assertEqual((booking["status"], booking["confirmed_at"], booking["client_reference"]), ("confirmed", 0, None))
        self.assertEqual(self.ledger()[0]["state"], "captured")
        for table in ("customer_events", "refunds", "fare_holds"):
            self.assertEqual(self.stack.query(f"SELECT COUNT(*) AS n FROM {table}"), [{"n": 0}])
        for table in ("episode_clock", "provider_ledger", "provider_plan", "service_endpoints"):
            with self.assertRaises(ValueError, msg=table):
                self.stack.query(f"SELECT * FROM {table}")
            with self.assertRaises(ValueError, msg=table):
                self.stack.query(f"WITH x AS (SELECT * FROM {table}) SELECT * FROM x")
        self.assertEqual(self.stack.query("SELECT state FROM charges"), [{"state": "captured"}])

    def test_contracts_expose_new_fields_and_read_only_markers(self):
        contracts = configuration_contracts()
        payment = contracts["payment"]["observed_fields"]
        for field in ("lookup_quota", "idempotency_window_steps", "provider_latency_ms"):
            self.assertTrue(payment[field]["readOnly"], field)
            self.assertNotIn(field, contracts["payment"]["patch_schema"]["properties"])
        self.assertEqual((payment["lookup_quota"]["minimum"], payment["lookup_quota"]["maximum"]), (0, 20))
        self.assertEqual((payment["idempotency_window_steps"]["minimum"], payment["idempotency_window_steps"]["maximum"]), (1, 1000))
        breaker = contracts["checkin"]["patch_schema"]["properties"]["auto_pause_after_attempts"]
        self.assertEqual((breaker["minimum"], breaker["maximum"], breaker["readOnly"]), (0, 50, False))
        self.assertEqual(self.stack.get_config("payment")["lookup_quota"], 3)
        self.assertEqual(self.stack.get_config("payment")["idempotency_window_steps"], 1000)
        self.assertEqual(self.stack.get_config("checkin")["auto_pause_after_attempts"], 0)
        with self.assertRaises(ValueError):
            self.stack.patch_config("checkin", {"auto_pause_after_attempts": 51})

    def test_legacy_positional_inserts_fill_leading_columns(self):
        self.stack.admin_execute("INSERT INTO charges VALUES(?,?,?,?)", ("legacy", "bkg_x", "key", 100))
        self.assertEqual(self.stack.query("SELECT state,created_step FROM charges"), [{"state": "captured", "created_step": 0}])
        with self.assertRaises(Exception):
            self.stack.admin_execute("INSERT INTO charges VALUES(?,?,?,?)", ("legacy", "bkg_x", "key", 100))

    # --- capture plans, settlement, expiry -------------------------------

    def test_planned_pending_capture_is_uncertain_until_its_settlement_step(self):
        self.stack.set_clock(2)
        self.plan("booking:" + self.booking_id_for("hard-request"), "pending", "captured", 5)
        result = self.book()
        self.assertEqual(result["status"], 504)
        self.assertIn("uncertain", result["body"]["error"])
        charge = self.stack.inspect()["charges"][0]
        self.assertEqual((charge["state"], charge["created_step"]), ("submitted", 2))
        self.assertEqual(self.ledger()[0]["state"], "pending")
        self.assertEqual(self.stack.lookup("SELECT COUNT(*) AS n FROM provider_plan"), [{"n": 0}])
        # The retry learns nothing new and writes nothing.
        retry = self.book()
        self.assertEqual((retry["status"], retry["body"]["error"]), (504, "payment: capture outcome pending at provider"))
        direct = self.capture(charge["booking_id"], charge["idempotency_key"])
        self.assertEqual((direct["status"], direct["body"]["error"]), (504, "capture outcome pending at provider"))
        self.assertEqual(len(self.stack.inspect()["charges"]), 1)
        self.stack.set_clock(4)
        self.assertEqual(self.settle(), 0)
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "submitted")
        self.stack.set_clock(5)
        self.assertEqual(self.settle(), 2)
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "captured")
        self.assertEqual(self.ledger()[0]["state"], "captured")
        # Once captured, the ordinary retry adopts the committed charge and confirms.
        self.assertEqual(self.book()["status"], 200)
        self.assertEqual(len(self.stack.inspect()["charges"]), 1)
        self.assertEqual(self.stack.inspect()["bookings"][0]["confirmed_at"], 5)

    def test_planned_decline_allows_a_fresh_legal_attempt_with_the_same_key(self):
        key = "booking:" + self.booking_id_for("hard-request")
        self.plan(key, "declined", "declined", 1)
        self.assertEqual(self.book()["status"], 504)
        self.stack.set_clock(1)
        self.assertEqual(self.settle(), 1)
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "declined")
        self.assertEqual(self.book()["status"], 200)
        charges = self.stack.inspect()["charges"]
        self.assertEqual([row["state"] for row in charges], ["declined", "captured"])
        self.assertEqual({row["idempotency_key"] for row in charges}, {key})
        self.assertEqual([row["state"] for row in self.ledger(idempotency_key=key)], ["declined", "captured"])
        lookup = self.stack.request("payment", "GET", "/provider/lookup?idempotency_key=" + key)
        self.assertEqual(lookup["body"]["state"], "captured")

    def test_planned_capture_with_lost_ack_settles_the_local_row_on_schedule(self):
        self.plan("booking:" + self.booking_id_for("hard-request"), "captured", "captured", 3)
        self.assertEqual(self.book()["status"], 504)
        self.assertEqual(self.ledger()[0]["state"], "captured")
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "submitted")
        self.assertEqual(self.settle(), 0)
        self.stack.set_clock(3)
        self.assertEqual(self.settle(), 1)
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "captured")

    def test_expired_idempotency_key_makes_the_retry_a_second_charge(self):
        booking = self.pending_booking()
        self.stack.admin_execute("UPDATE service_config SET value=json_set(value,'$.idempotency_window_steps',5) WHERE service='payment'")
        self.stack.set_clock(5)
        self.assertEqual(self.book()["status"], 200)
        self.assertEqual(len(self.stack.inspect()["charges"]), 1)
        self.stack.admin_execute("UPDATE bookings SET status='pending' WHERE booking_id=?", (booking["booking_id"],))
        self.stack.set_clock(6)
        self.assertEqual(self.book()["status"], 200)
        charges = self.stack.inspect()["charges"]
        self.assertEqual(len(charges), 2)
        self.assertEqual({row["idempotency_key"] for row in charges}, {"booking:" + booking["booking_id"]})
        self.assertEqual([row["created_step"] for row in charges], [0, 6])
        self.assertEqual([row["state"] for row in self.ledger()], ["captured", "captured"])

    def test_plan_applies_once_and_never_without_idempotency(self):
        self.stack.patch_config("payment", {"idempotency_enabled": False})
        self.plan("booking:" + self.booking_id_for("hard-request"), "pending", "captured", 9)
        self.assertEqual(self.book()["status"], 200)
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "captured")
        self.assertEqual(self.stack.lookup("SELECT COUNT(*) AS n FROM provider_plan"), [{"n": 1}])

    # --- lookup, void, refund -------------------------------------------

    def test_provider_lookup_reveals_ledger_truth_or_404(self):
        booking = self.pending_booking()
        key = "booking:" + booking["booking_id"]
        result = self.stack.request("payment", "GET", "/provider/lookup?idempotency_key=" + key)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], {"idempotency_key": key, "state": "captured", "booking_id": booking["booking_id"], "amount_cents": booking["amount_cents"]})
        self.assertEqual(self.stack.request("payment", "GET", "/provider/lookup?idempotency_key=nope")["status"], 404)

    def test_void_refunds_captured_charge_releases_seat_and_is_idempotent(self):
        booking = self.pending_booking()
        self.stack.set_clock(3)
        result = self.stack.request("booking", "POST", "/void", {"booking_id": booking["booking_id"]})
        self.assertEqual(result["status"], 200, result)
        self.assertEqual(result["body"], {"booking_id": booking["booking_id"], "status": "cancelled", "refunded_cents": booking["amount_cents"]})
        state = self.stack.inspect()
        self.assertEqual(state["bookings"][0]["status"], "cancelled")
        self.assertEqual(state["holds"], [])
        self.assertEqual(len(state["refunds"]), 1)
        self.assertEqual((state["refunds"][0]["amount_cents"], state["refunds"][0]["created_step"]), (booking["amount_cents"], 3))
        again = self.stack.request("booking", "POST", "/void", {"booking_id": booking["booking_id"]})
        self.assertEqual(again["body"]["refunded_cents"], booking["amount_cents"])
        self.assertEqual(len(self.stack.inspect()["refunds"]), 1)
        self.assertEqual(self.stack.request("booking", "POST", "/void", {"booking_id": "bkg_missing"})["status"], 404)
        self.assertEqual(self.book("confirmed-one")["status"], 200)
        confirmed = next(row for row in self.stack.inspect()["bookings"] if row["request_id"] == "confirmed-one")
        self.assertEqual(self.stack.request("booking", "POST", "/void", {"booking_id": confirmed["booking_id"]})["status"], 409)

    def test_void_waits_for_pending_outcome_and_refund_outage_leaves_booking_pending(self):
        self.plan("booking:" + self.booking_id_for("hard-request"), "pending", "captured", 4)
        self.assertEqual(self.book()["status"], 504)
        booking = self.stack.inspect()["bookings"][0]
        result = self.stack.request("booking", "POST", "/void", {"booking_id": booking["booking_id"]})
        self.assertEqual((result["status"], result["body"]["error"]), (409, "payment outcome unresolved"))
        self.stack.set_clock(4)
        self.settle()
        self.stack.stop_service("payment")
        self.assertEqual(self.stack.request("booking", "POST", "/void", {"booking_id": booking["booking_id"]})["status"], 503)
        self.assertEqual(self.stack.inspect()["bookings"][0]["status"], "pending")
        self.assertEqual(len(self.stack.inspect()["holds"]), 1)
        self.stack.restart("payment")
        self.assertEqual(self.stack.request("booking", "POST", "/void", {"booking_id": booking["booking_id"]})["body"]["refunded_cents"], booking["amount_cents"])

    def test_void_of_uncharged_booking_refunds_nothing_and_refund_rejects_declined(self):
        self.stack.stop_service("payment")
        self.assertEqual(self.book()["status"], 503)
        booking = self.stack.inspect()["bookings"][0]
        self.stack.restart("payment")
        result = self.stack.request("booking", "POST", "/void", {"booking_id": booking["booking_id"]})
        self.assertEqual(result["body"]["refunded_cents"], 0)
        self.assertEqual(self.stack.inspect()["refunds"], [])
        self.plan("booking:" + self.booking_id_for("declined"), "declined", "declined", 0)
        self.assertEqual(self.book("declined")["status"], 504)
        self.settle()
        charge = next(row for row in self.stack.inspect()["charges"] if row["state"] == "declined")
        self.assertEqual(self.stack.request("payment", "POST", "/refund", {"charge_id": charge["charge_id"]})["status"], 409)
        self.assertEqual(self.stack.request("payment", "POST", "/refund", {"charge_id": "chg_missing"})["status"], 409)

    # --- adoption -----------------------------------------------------

    def test_adoption_requires_ledger_captured_charge(self):
        self.plan("booking:" + self.booking_id_for("hard-request"), "pending", "captured", 2)
        self.assertEqual(self.book()["status"], 504)
        state = self.stack.inspect()
        booking_id, charge_id = state["bookings"][0]["booking_id"], state["charges"][0]["charge_id"]
        self.assertEqual(self.stack.reconcile(booking_id, existing_charge_id=charge_id)["status"], 409)
        self.stack.set_clock(2)
        self.settle()
        self.assertEqual(self.stack.reconcile(booking_id, existing_charge_id=charge_id)["status"], 200)
        self.assertEqual(self.stack.inspect()["bookings"][0]["confirmed_at"], 2)

    def test_adoption_falls_back_to_local_state_without_ledger_rows(self):
        booking = self.pending_booking()
        self.stack.admin_execute("DELETE FROM provider_ledger")
        charge = self.stack.inspect()["charges"][0]
        self.assertEqual(self.stack.reconcile(booking["booking_id"], existing_charge_id=charge["charge_id"])["status"], 200)
        self.stack.admin_execute("UPDATE bookings SET status='pending'")
        self.stack.admin_execute("UPDATE charges SET state='lost'")
        self.assertEqual(self.stack.reconcile(booking["booking_id"], existing_charge_id=charge["charge_id"])["status"], 409)

    # --- fare holds and cache ----------------------------------------------

    def test_fare_hold_lives_in_the_cache_row_and_validates_until_it_lapses(self):
        # The injector seeds the held price into the cache row; quotes never consult fare_holds themselves.
        self.stack.admin_execute("INSERT INTO fare_holds(hold_id,flight_id,price_cents,until_step) VALUES (?,?,?,?)", ("hold-1", "F200", 23000, 4))
        self.stack.admin_execute("INSERT OR REPLACE INTO cache VALUES (?,?)", ("quote:F200", json.dumps({"flight_id": "F200", "amount_cents": 23000, "version": 1})))
        self.stack.set_clock(3)
        quote = self.stack.request("pricing", "GET", "/quote?flight_id=F200")["body"]
        self.assertEqual((quote["amount_cents"], quote["version"]), (23000, 1))
        self.assertEqual(self.book("held", "F200")["status"], 200)
        self.assertEqual(self.stack.inspect()["bookings"][0]["amount_cents"], 23000)
        self.assertEqual(self.stack.request("pricing", "GET", "/quote?flight_id=F100")["body"]["amount_cents"], 20000)
        # A blanket eviction drops the promise: the regenerated quote drifts to the flight price.
        self.assertEqual(self.stack.invalidate_cache("pricing")["cleared"], 2)
        self.assertEqual(self.stack.request("pricing", "GET", "/quote?flight_id=F200")["body"]["amount_cents"], 25000)
        self.assertEqual(self.book("drifted", "F200")["status"], 200)
        self.assertEqual(next(r["amount_cents"] for r in self.stack.inspect()["bookings"] if r["request_id"] == "drifted"), 25000)
        # A held quote is accepted only while the hold is live.
        self.stack.admin_execute("INSERT OR REPLACE INTO cache VALUES (?,?)", ("quote:F200", json.dumps({"flight_id": "F200", "amount_cents": 23000, "version": 1})))
        self.stack.set_clock(5)
        self.assertEqual(self.book("lapsed", "F200")["status"], 409)
        self.assertEqual(self.stack.invalidate_cache("pricing", flight_id="F200"), {"service": "pricing", "cleared": 1})
        self.assertEqual(self.book("lapsed", "F200")["status"], 200)
        self.assertEqual(next(r["amount_cents"] for r in self.stack.inspect()["bookings"] if r["request_id"] == "lapsed"), 25000)

    def test_retry_of_submitted_charge_the_provider_captured_acknowledges_it(self):
        # Outcome captured at the provider but never scheduled for local settlement: the retry is the acknowledgement.
        key = "booking:" + self.booking_id_for("hard-request")
        self.plan(key, "captured", "captured", None)
        self.assertEqual(self.book()["status"], 504)
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "submitted")
        self.assertEqual(self.settle(), 0)
        retry = self.book(idempotency_key=key)
        self.assertEqual(retry["status"], 200, retry)
        charges = self.stack.inspect()["charges"]
        self.assertEqual([(row["state"], row["idempotency_key"]) for row in charges], [("captured", key)])
        self.assertEqual(self.stack.inspect()["bookings"][0]["status"], "confirmed")

    def test_cancelled_booking_cannot_be_rebooked_by_retry(self):
        booking = self.pending_booking()
        self.assertEqual(self.stack.request("booking", "POST", "/void", {"booking_id": booking["booking_id"]})["status"], 200)
        for extra in ({}, {"idempotency_key": "booking:" + booking["booking_id"]}, {"existing_charge_id": self.stack.inspect()["charges"][0]["charge_id"]}):
            result = self.book(**extra)
            self.assertEqual((result["status"], result["body"]["error"]), (409, "booking is cancelled"), extra)
        state = self.stack.inspect()
        self.assertEqual(state["bookings"][0]["status"], "cancelled")
        self.assertEqual((state["holds"], len(state["charges"]), len(state["outbox"])), ([], 1, 0))

    def test_scoped_invalidation_leaves_other_quotes_cached(self):
        for flight in ("F100", "F200"):
            self.stack.request("pricing", "GET", f"/quote?flight_id={flight}")
        self.assertEqual(self.stack.invalidate_cache("pricing", flight_id="F100")["cleared"], 1)
        self.assertEqual(self.stack.query("SELECT key FROM cache"), [{"key": "quote:F200"}])
        self.assertEqual(self.stack.invalidate_cache("pricing", flight_id="F100")["cleared"], 0)
        self.assertEqual(self.stack.invalidate_cache("pricing")["cleared"], 1)
        with self.assertRaises(ValueError):
            self.stack.invalidate_cache("pricing", flight_id="")

    # --- runtime: breaker, health, restart, retention ----------------------

    def test_circuit_breaker_pauses_consumer_after_repeated_head_failure(self):
        booking_id = self.book()["body"]["booking_id"]
        self.stack.admin_execute("INSERT INTO outbox(event_id,booking_id,payload,status,attempts) VALUES (?,?,?,'pending',0)", ("000-poison", booking_id, "{broken"))
        self.stack.patch_config("checkin", {"auto_pause_after_attempts": 3})
        for _ in range(2):
            self.assertEqual(self.stack.pump()["blocked_event_id"], "000-poison")
            self.assertTrue(self.stack.get_config("checkin")["consumer_enabled"])
        self.assertEqual(self.stack.pump()["blocked_event_id"], "000-poison")
        self.assertFalse(self.stack.get_config("checkin")["consumer_enabled"])
        paused = [row for row in self.stack.logs("checkin", limit=50) if row["message"] == "consumer paused by circuit breaker after repeated delivery failure"]
        self.assertEqual(len(paused), 1)
        # Treating the symptom re-trips the breaker; quarantining the head lets the backlog flow.
        self.stack.patch_config("checkin", {"consumer_enabled": True})
        self.assertEqual(self.stack.pump()["delivered"], 0)
        self.assertFalse(self.stack.get_config("checkin")["consumer_enabled"])
        self.stack.quarantine("000-poison")
        self.stack.patch_config("checkin", {"consumer_enabled": True})
        self.assertEqual(self.stack.pump()["delivered"], 1)
        self.assertTrue(self.stack.get_config("checkin")["consumer_enabled"])

    def test_breaker_off_by_default_never_pauses(self):
        booking_id = self.book()["body"]["booking_id"]
        self.stack.admin_execute("INSERT INTO outbox(event_id,booking_id,payload,status,attempts) VALUES (?,?,?,'pending',0)", ("000-poison", booking_id, "{broken"))
        for _ in range(5):
            self.stack.pump()
        self.assertTrue(self.stack.get_config("checkin")["consumer_enabled"])

    def test_payment_health_reflects_recent_deadline_failures_only(self):
        self.assertEqual({name: s["health"] for name, s in self.stack.metrics()["services"].items()}, dict.fromkeys(self.stack.services, "healthy"))
        self.stack.patch_config("booking", {"payment_timeout_ms": 10})
        for index in range(4):
            self.assertEqual(self.book(f"slow-{index}")["status"], 504)
        self.assertEqual(self.stack.metrics("payment")["services"]["payment"]["health"], "degraded")
        self.stack.patch_config("booking", {"payment_timeout_ms": 200})
        for index in range(12):
            self.assertEqual(self.book(f"fast-{index}")["status"], 200)
        metrics = self.stack.metrics()["services"]
        self.assertEqual(metrics["payment"]["health"], "healthy")
        self.assertEqual(metrics["booking"]["health"], "healthy")

    def test_restart_marks_submitted_charges_lost_while_ledger_keeps_the_truth(self):
        self.plan("booking:" + self.booking_id_for("hard-request"), "captured", "captured", 6)
        self.assertEqual(self.book()["status"], 504)
        self.stack.restart("booking")
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "submitted")
        self.stack.restart("payment")
        charge = self.stack.inspect()["charges"][0]
        self.assertEqual(charge["state"], "lost")
        self.stack.set_clock(6)
        self.assertEqual(self.settle(), 0)
        self.assertEqual(self.stack.inspect()["charges"][0]["state"], "lost")
        lookup = self.stack.request("payment", "GET", "/provider/lookup?idempotency_key=" + charge["idempotency_key"])
        self.assertEqual(lookup["body"]["state"], "captured")
        self.assertEqual(self.stack.reconcile(charge["booking_id"], existing_charge_id=charge["charge_id"])["status"], 200)

    def test_log_retention_trigger_keeps_only_the_newest_rows(self):
        self.stack.install_log_retention(5)
        for index in range(4):
            self.book(f"r-{index}")
        rows = self.stack.logs(limit=500)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1]["id"] - rows[0]["id"], 4)
        self.assertEqual(self.stack.query("SELECT COUNT(*) AS n FROM request_logs"), [{"n": 5}])
        with self.assertRaises(ValueError):
            self.stack.install_log_retention(0)

    @staticmethod
    def booking_id_for(request_id):
        return "bkg_" + uuid.uuid5(uuid.NAMESPACE_URL, "airline-recovery:booking:" + request_id).hex


if __name__ == "__main__":
    unittest.main()
