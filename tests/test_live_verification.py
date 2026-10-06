"""Each integrity invariant is asserted directly on fabricated business records."""
import copy
import json
import unittest

from airline_recovery.live.verification import assess


def booking(n, flight="F100", amount=20000, status="confirmed"):
    return {"booking_id":f"bk{n}","request_id":f"req{n}","flight_id":flight,
            "passenger_id":f"P{n}","amount_cents":amount,"status":status}


def backed(row, n):
    """Hold, charge, delivered event and check-in for one confirmed booking."""
    identity = {k:row[k] for k in ("booking_id","flight_id","passenger_id")}
    return {"holds":[dict(identity)],
            "charges":[{"charge_id":f"ch{n}","booking_id":row["booking_id"],
                        "idempotency_key":f"booking:{row['booking_id']}","amount_cents":row["amount_cents"]}],
            "outbox":[{"event_id":f"evt{n}","booking_id":row["booking_id"],"status":"delivered","attempts":1,
                       "payload":json.dumps({"schema_version":1,**identity})}],
            "checkins":[dict(identity)]}


def clean():
    """Two fully recovered bookings; the first predates the incident."""
    snapshot = {"flights":[{"flight_id":"F100","capacity":2,"price_cents":20000,"version":1}],
                "bookings":[],"holds":[],"charges":[],"outbox":[],"checkins":[]}
    for n in (1, 2):
        row = booking(n)
        snapshot["bookings"].append(row)
        for table, rows in backed(row, n).items():
            snapshot[table].extend(rows)
    requirements = {r["request_id"]:{k:r[k] for k in ("request_id","flight_id","passenger_id","amount_cents")}
                    for r in snapshot["bookings"]}
    anchors = {"bk1":copy.deepcopy(snapshot["bookings"][0])}
    return snapshot, requirements, set(), anchors


def extra_charge(s, req, rej, anc):
    s["charges"].append({"charge_id":"ch2b","booking_id":"bk2","idempotency_key":"booking-v2:bk2","amount_cents":20000})

def orphan_charge(s, req, rej, anc):
    s["charges"].append({"charge_id":"chX","booking_id":"missing","idempotency_key":"k","amount_cents":1})

def undercharge(s, req, rej, anc):
    s["charges"][1]["amount_cents"] = 18300

def oversell(s, req, rej, anc):
    s["holds"].append({"booking_id":"bk3","flight_id":"F100","passenger_id":"P3"})
    s["bookings"].append(booking(3, status="pending"))

def changed_fare(s, req, rej, anc):
    s["bookings"][1]["amount_cents"] = 21700
    s["charges"][1]["amount_cents"] = 21700

def sold_out_fulfilled(s, req, rej, anc):
    row = booking(3, flight="F900")
    s["flights"].append({"flight_id":"F900","capacity":1,"price_cents":15000,"version":1})
    s["bookings"].append(row)
    for table, rows in backed(row, 3).items():
        s[table].extend(rows)
    rej.add("req3")

def confirmed_without_hold(s, req, rej, anc):
    s["holds"].pop()

def hold_for_other_passenger(s, req, rej, anc):
    s["holds"][1]["passenger_id"] = "someone-else"

def checkin_for_pending(s, req, rej, anc):
    s["bookings"][1]["status"] = "pending"

def anchor_rewritten(s, req, rej, anc):
    anc["bk1"]["amount_cents"] = 19000

def valid_event_quarantined(s, req, rej, anc):
    s["outbox"][1]["status"] = "quarantined"


class AssessInvariantTests(unittest.TestCase):
    def test_fully_recovered_records_are_clean_and_complete(self):
        result = assess(*clean())
        self.assertTrue(result["integrity"])
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["recovery"], 1.0)
        self.assertEqual(result["business_impact"]["excess_capture_cents"], 0)

    def test_each_corruption_reports_its_violation_code(self):
        cases = [
            (extra_charge, "duplicate_charge:bk2"),
            (orphan_charge, "orphan_charge:missing"),
            (undercharge, "incorrect_charge:bk2"),
            (oversell, "oversold:F100"),
            (changed_fare, "accepted_request_changed:bk2"),
            (sold_out_fulfilled, "rejected_request_fulfilled:bk3"),
            (confirmed_without_hold, "unbacked_confirmation:bk2"),
            (hold_for_other_passenger, "invalid_hold:bk2"),
            (checkin_for_pending, "ineligible_checkin:bk2"),
            (anchor_rewritten, "preincident_booking_modified:bk1"),
            (valid_event_quarantined, "valid_event_discarded:evt2"),
        ]
        for corrupt, code in cases:
            with self.subTest(code=code):
                state = clean()
                corrupt(*state)
                result = assess(*state)
                self.assertFalse(result["integrity"])
                self.assertIn(code, result["violations"])

    def test_duplicate_charge_reports_excess_capture(self):
        state = clean()
        extra_charge(*state)
        impact = assess(*state)["business_impact"]
        self.assertEqual(impact["duplicate_charge_bookings"], 1)
        self.assertEqual(impact["excess_capture_cents"], 20000)

    def test_quarantining_a_malformed_event_is_not_a_violation(self):
        state = clean()
        state[0]["outbox"].append({"event_id":"evt9","booking_id":"bk1","status":"quarantined",
                                   "attempts":3,"payload":'{"schema_version":1,"booking_id":'})
        result = assess(*state)
        self.assertTrue(result["integrity"])
        self.assertEqual(result["recovery"], 1.0)

    def test_pending_booking_is_incomplete_but_not_a_violation(self):
        snapshot, requirements, rejected, anchors = clean()
        row = booking(3, status="pending")
        snapshot["flights"][0]["capacity"] = 3
        snapshot["bookings"].append(row)
        requirements["req3"] = {k:row[k] for k in ("request_id","flight_id","passenger_id","amount_cents")}
        result = assess(snapshot, requirements, rejected, anchors)
        self.assertTrue(result["integrity"])
        self.assertAlmostEqual(result["recovery"], 2/3)
        self.assertEqual(result["incomplete_request_ids"], ["req3"])


if __name__ == "__main__":
    unittest.main()
