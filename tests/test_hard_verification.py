"""Hard-tier trap states asserted directly on fabricated business records.

For every trap the wrong resolution yields its violation code and the correct
resolution is clean and complete. Snapshots without the new tables must keep
yielding exactly the easy tier's codes.
"""
import copy
import json
import unittest

from airline_recovery.live.verification import assess
from tests import test_live_verification as easy


def ledger(charge, state="captured", final=None, settles=None):
    return {"attempt_id": charge["charge_id"], "idempotency_key": charge["idempotency_key"],
            "booking_id": charge["booking_id"], "amount_cents": charge["amount_cents"],
            "state": state, "final_state": final, "settles_at_step": settles, "created_step": 0}


def hard_clean():
    """The easy clean state with ledger rows and the new tables present and empty."""
    snapshot, requirements, rejected, anchors = easy.clean()
    for row in snapshot["bookings"]:
        row.update(client_reference=None, confirmed_at=1)
    for charge in snapshot["charges"]:
        charge.update(state="captured", created_step=0)
    snapshot.update(provider_ledger=[ledger(c) for c in snapshot["charges"]],
                    customer_events=[], refunds=[], fare_holds=[])
    return snapshot, requirements, rejected, anchors


def add_pending(snapshot, requirements, n, **fields):
    row = easy.booking(n, status="pending")
    row.update({"client_reference": None, "confirmed_at": None, **fields})
    snapshot["flights"][0]["capacity"] += 1
    snapshot["bookings"].append(row)
    requirements[row["request_id"]] = {k: row[k] for k in ("request_id", "flight_id", "passenger_id", "amount_cents")}
    return row


def charge_for(snapshot, row, n, state="captured", truth="captured", key=None):
    charge = {"charge_id": f"ch{n}", "booking_id": row["booking_id"], "idempotency_key": key or f"booking:{row['booking_id']}",
              "amount_cents": row["amount_cents"], "state": state, "created_step": 0}
    snapshot["charges"].append(charge)
    snapshot["provider_ledger"].append(ledger(charge, truth))
    return charge


def confirm(snapshot, row, n, at=5):
    """Confirm with hold, delivered valid event and check-in; charges are the caller's."""
    row.update(status="confirmed", confirmed_at=at)
    for table, rows in easy.backed(row, n).items():
        if table != "charges":
            snapshot[table].extend(rows)


def cancel(snapshot, row, refund=True):
    row.update(status="cancelled")
    if refund:
        for charge in snapshot["charges"]:
            if charge["booking_id"] == row["booking_id"]:
                snapshot["refunds"].append({"refund_id": "r" + charge["charge_id"], "charge_id": charge["charge_id"],
                                            "booking_id": row["booking_id"], "amount_cents": charge["amount_cents"], "created_step": 6})


class TrapStateTests(unittest.TestCase):
    def assertClean(self, state):
        result = assess(*state)
        self.assertEqual(result["violations"], [])
        self.assertTrue(result["integrity"])
        self.assertEqual(result["recovery"], 1.0, result["incomplete_request_ids"])

    def test_cancelled_request(self):
        for wrong in (True, False):
            with self.subTest(wrong=wrong):
                snapshot, requirements, rejected, anchors = hard_clean()
                row = add_pending(snapshot, requirements, 3)
                charge_for(snapshot, row, 3)
                snapshot["customer_events"].append({"event_id": "cev1", "request_id": row["request_id"], "kind": "cancel", "step": 2})
                if wrong:
                    confirm(snapshot, row, 3, at=7)
                    result = assess(snapshot, requirements, rejected, anchors)
                    self.assertEqual(result["violations"], ["cancelled_request_fulfilled:bk3"])
                else:
                    cancel(snapshot, row)
                    self.assertClean((snapshot, requirements, rejected, anchors))

    def test_cancellation_after_confirmation_is_not_a_violation(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        charge_for(snapshot, row, 3)
        confirm(snapshot, row, 3, at=4)
        snapshot["customer_events"].append({"event_id": "cev1", "request_id": row["request_id"], "kind": "cancel", "step": 5})
        self.assertClean((snapshot, requirements, rejected, anchors))

    def test_duplicate_client_reference(self):
        for wrong in (True, False):
            with self.subTest(wrong=wrong):
                snapshot, requirements, rejected, anchors = hard_clean()
                older = add_pending(snapshot, requirements, 3, client_reference="CR-1")
                newer = add_pending(snapshot, requirements, 4, client_reference="CR-1")
                del requirements[newer["request_id"]]  # the repeated sale is a trap, not a requirement
                charge_for(snapshot, older, 3)
                charge_for(snapshot, newer, 4)
                confirm(snapshot, older, 3)
                if wrong:
                    confirm(snapshot, newer, 4)
                    result = assess(snapshot, requirements, rejected, anchors)
                    self.assertEqual(result["violations"], ["duplicate_sale:CR-1"])
                else:
                    cancel(snapshot, newer)
                    self.assertClean((snapshot, requirements, rejected, anchors))

    def test_unfunded_confirmation_uses_ledger_truth(self):
        for wrong in (True, False):
            with self.subTest(wrong=wrong):
                snapshot, requirements, rejected, anchors = hard_clean()
                row = add_pending(snapshot, requirements, 3)
                charge_for(snapshot, row, 3, state="declined", truth="declined")
                if wrong:
                    confirm(snapshot, row, 3)
                    result = assess(snapshot, requirements, rejected, anchors)
                    self.assertEqual(result["violations"], ["unfunded_confirmation:bk3"])
                else:
                    charge_for(snapshot, row, "3b", key=f"booking:{row['booking_id']}")
                    confirm(snapshot, row, 3)
                    self.assertClean((snapshot, requirements, rejected, anchors))

    def test_local_submitted_charge_with_ledger_capture_is_funded(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        charge_for(snapshot, row, 3, state="submitted", truth="captured")
        confirm(snapshot, row, 3)
        self.assertClean((snapshot, requirements, rejected, anchors))

    def test_retry_of_an_expired_key_is_a_duplicate_charge(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        charge_for(snapshot, row, 3, state="submitted", truth="captured")
        charge_for(snapshot, row, "3b")
        confirm(snapshot, row, 3)
        result = assess(snapshot, requirements, rejected, anchors)
        self.assertEqual(result["violations"], ["duplicate_charge:bk3", "unfunded_confirmation:bk3"])
        self.assertEqual(result["business_impact"]["excess_capture_cents"], 20000)

    def test_declined_then_captured_is_one_charge(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        charge_for(snapshot, row, 3, state="declined", truth="declined")
        charge_for(snapshot, row, "3b", key=f"booking:{row['booking_id']}")
        confirm(snapshot, row, 3)
        self.assertClean((snapshot, requirements, rejected, anchors))

    def test_refund_missing(self):
        for wrong in (True, False):
            with self.subTest(wrong=wrong):
                snapshot, requirements, rejected, anchors = hard_clean()
                row = add_pending(snapshot, requirements, 3)
                charge_for(snapshot, row, 3)
                snapshot["customer_events"].append({"event_id": "cev1", "request_id": row["request_id"], "kind": "cancel", "step": 2})
                cancel(snapshot, row, refund=not wrong)
                result = assess(snapshot, requirements, rejected, anchors)
                if wrong:
                    self.assertEqual(result["violations"], ["refund_missing:bk3"])
                    self.assertIn(row["request_id"], result["incomplete_request_ids"])
                else:
                    self.assertClean((snapshot, requirements, rejected, anchors))

    def test_declined_charge_needs_no_refund(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        charge_for(snapshot, row, 3, state="declined", truth="declined")
        snapshot["customer_events"].append({"event_id": "cev1", "request_id": row["request_id"], "kind": "cancel", "step": 2})
        cancel(snapshot, row, refund=False)
        self.assertClean((snapshot, requirements, rejected, anchors))

    def test_refund_unwarranted(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        snapshot["refunds"].append({"refund_id": "r1", "charge_id": "ch2", "booking_id": "bk2", "amount_cents": 20000, "created_step": 3})
        result = assess(snapshot, requirements, rejected, anchors)
        self.assertEqual(result["violations"], ["refund_unwarranted:bk2"])

    def test_refund_on_a_pending_booking_is_a_void_in_progress(self):
        # The void refunds first and cancels second; between the two the booking is still pending.
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        snapshot["refunds"].append({"refund_id": "r3", "charge_id": "ch3", "booking_id": row["booking_id"],
                                    "amount_cents": row["amount_cents"], "created_step": 3})
        result = assess(snapshot, requirements, rejected, anchors)
        self.assertNotIn(f"refund_unwarranted:{row['booking_id']}", result["violations"])

    def test_cancelled_booking_backed(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        snapshot["customer_events"].append({"event_id": "cev1", "request_id": row["request_id"], "kind": "cancel", "step": 2})
        cancel(snapshot, row)
        snapshot["holds"].append({k: row[k] for k in ("booking_id", "flight_id", "passenger_id")})
        result = assess(snapshot, requirements, rejected, anchors)
        self.assertEqual(result["violations"], ["cancelled_booking_backed:bk3"])

    def test_voiding_without_a_cancel_event_is_incomplete_not_a_violation(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        charge_for(snapshot, row, 3)
        cancel(snapshot, row)
        result = assess(snapshot, requirements, rejected, anchors)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["incomplete_request_ids"], [row["request_id"]])

    def test_fare_hold_broken_trips_accepted_request_changed(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        snapshot["fare_holds"].append({"hold_id": "h1", "flight_id": "F100", "price_cents": 20000, "until_step": 40})
        row = add_pending(snapshot, requirements, 3)
        snapshot["flights"][0].update(price_cents=22300, version=2)
        row["amount_cents"] = 22300  # accepted at the new fare while the customer held 20000
        charge_for(snapshot, row, 3)
        confirm(snapshot, row, 3)
        result = assess(snapshot, requirements, rejected, anchors)
        self.assertEqual(result["violations"], ["accepted_request_changed:bk3"])

    def test_lost_charge_whose_ledger_captured_still_counts(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        row = add_pending(snapshot, requirements, 3)
        charge_for(snapshot, row, 3, state="lost", truth="captured")
        confirm(snapshot, row, 3)
        self.assertClean((snapshot, requirements, rejected, anchors))


class EasySnapshotCompatibilityTests(unittest.TestCase):
    def test_snapshots_without_new_tables_yield_exactly_the_easy_codes(self):
        cases = [
            (easy.extra_charge, ["duplicate_charge:bk2", "unbacked_confirmation:bk2"]),
            (easy.orphan_charge, ["orphan_charge:missing"]),
            (easy.undercharge, ["incorrect_charge:bk2"]),
            (easy.oversell, ["oversold:F100"]),
            (easy.changed_fare, ["accepted_request_changed:bk2"]),
            (easy.sold_out_fulfilled, ["rejected_request_fulfilled:bk3"]),
            (easy.confirmed_without_hold, ["unbacked_confirmation:bk2"]),
            (easy.hold_for_other_passenger, ["invalid_hold:bk2"]),
            (easy.checkin_for_pending, ["ineligible_checkin:bk2"]),
            (easy.anchor_rewritten, ["preincident_booking_modified:bk1"]),
            (easy.valid_event_quarantined, ["valid_event_discarded:evt2"]),
        ]
        for corrupt, codes in cases:
            with self.subTest(codes=codes):
                state = easy.clean()
                corrupt(*state)
                before = assess(*copy.deepcopy(state))
                self.assertEqual(before["violations"], codes)
                snapshot = {**state[0], "provider_ledger": [], "customer_events": [], "refunds": [], "fare_holds": []}
                self.assertEqual(assess(snapshot, *state[1:]), before)

    def test_easy_charges_without_state_column_are_captured(self):
        state = easy.clean()
        result = assess(*state)
        self.assertEqual(result["recovery"], 1.0)
        self.assertEqual(result["violations"], [])

    def test_ledger_rows_do_not_change_a_clean_easy_outcome(self):
        snapshot, requirements, rejected, anchors = hard_clean()
        easy_result = assess(*easy.clean())
        hard_result = assess(snapshot, requirements, rejected, anchors)
        self.assertEqual(hard_result["violations"], easy_result["violations"])
        self.assertEqual(hard_result["recovery"], easy_result["recovery"])
        self.assertEqual(json.dumps(hard_result["business_impact"]), json.dumps(easy_result["business_impact"]))


if __name__ == "__main__":
    unittest.main()
