"""Outcome checks over executed business records, independent of remediation recipe."""
import json
from collections import Counter, defaultdict

TABLES = ("flights", "bookings", "holds", "charges", "outbox", "checkins",
          "provider_ledger", "customer_events", "refunds")


def _captured(local, ledger):
    """Truth for "a charge" per booking.

    When the provider knows a booking, its ledger rows in state captured are the
    charges; the local ``charges`` table only records attempts. Without ledger
    rows (the easy tier, fabricated snapshots) local rows count, and a local row
    whose state column is absent is an ordinary captured charge. A local captured
    row under a key the ledger never saw cannot be anything but a real capture,
    so it counts as well; this keeps a capture made with idempotency disabled
    visible even when the provider was not told about it.
    """
    captured = defaultdict(list)
    for booking_id in set(local) | set(ledger):
        known = {row["idempotency_key"] for row in ledger.get(booking_id, ())}
        for row in ledger.get(booking_id, ()):
            if row["state"] == "captured":
                captured[booking_id].append({"amount_cents": row["amount_cents"], "idempotency_key": row["idempotency_key"]})
        for row in local.get(booking_id, ()):
            if row.get("state", "captured") == "captured" and (not known or row["idempotency_key"] not in known):
                captured[booking_id].append({"amount_cents": row["amount_cents"], "idempotency_key": row["idempotency_key"],
                                             "charge_id": row.get("charge_id")})
    return captured


def _refunded(captured, refunds):
    """Every captured charge of a booking has a refund of equal amount."""
    owed = Counter(row["amount_cents"] for row in captured)
    owed.subtract(Counter(row["amount_cents"] for row in refunds))
    return not any(count > 0 for count in owed.values())


def assess(snapshot, requirements, rejected, anchors):
    tables = {key: snapshot.get(key, []) for key in TABLES}
    bookings = {row["booking_id"]: row for row in tables["bookings"]}
    by_request = {row["request_id"]: row for row in tables["bookings"]}
    flights = {row["flight_id"]: row for row in tables["flights"]}
    holds = {row["booking_id"]: row for row in tables["holds"]}
    checkins = {row["booking_id"]: row for row in tables["checkins"]}
    local, ledger, refunds, cancels = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for charge in tables["charges"]:
        local[charge["booking_id"]].append(charge)
    for row in tables["provider_ledger"]:
        ledger[row["booking_id"]].append(row)
    for row in tables["refunds"]:
        refunds[row["booking_id"]].append(row)
    for event in tables["customer_events"]:
        if event.get("kind", "cancel") == "cancel":
            cancels[event["request_id"]].append(event.get("step", 0))
    charges = _captured(local, ledger)
    violations = []
    for flight_id, count in Counter(r["flight_id"] for r in tables["holds"]).items():
        if flight_id not in flights or count > flights[flight_id]["capacity"]:
            violations.append(f"oversold:{flight_id}")
    for booking_id in set(local) | set(ledger):
        rows = charges[booking_id]
        if booking_id not in bookings:
            violations.append(f"orphan_charge:{booking_id}")
        elif len(rows) > 1:
            violations.append(f"duplicate_charge:{booking_id}")
        elif rows and rows[0]["amount_cents"] != bookings[booking_id]["amount_cents"]:
            violations.append(f"incorrect_charge:{booking_id}")
    for booking_id, row in bookings.items():
        requirement = requirements.get(row["request_id"])
        if requirement and any(row[k] != requirement[k] for k in ("flight_id", "passenger_id", "amount_cents")):
            violations.append(f"accepted_request_changed:{booking_id}")
        if row["request_id"] in rejected and (row["status"] == "confirmed" or local[booking_id] or ledger[booking_id] or booking_id in holds):
            violations.append(f"rejected_request_fulfilled:{booking_id}")
        if row["status"] == "confirmed":
            if booking_id not in holds or (len(charges[booking_id]) != 1 and not ledger[booking_id]):
                violations.append(f"unbacked_confirmation:{booking_id}")
            if ledger[booking_id] and len(charges[booking_id]) != 1:
                violations.append(f"unfunded_confirmation:{booking_id}")
            confirmed_at = row.get("confirmed_at")
            if any(confirmed_at is None or step <= confirmed_at for step in cancels[row["request_id"]]):
                violations.append(f"cancelled_request_fulfilled:{booking_id}")
        if row["status"] == "cancelled":
            if charges[booking_id] and not _refunded(charges[booking_id], refunds[booking_id]):
                violations.append(f"refund_missing:{booking_id}")
            if booking_id in holds or booking_id in checkins:
                violations.append(f"cancelled_booking_backed:{booking_id}")
    for booking_id in refunds:
        # A refund on a still-pending booking is a void whose cancellation has not landed yet.
        if booking_id not in bookings or bookings[booking_id]["status"] == "confirmed":
            violations.append(f"refund_unwarranted:{booking_id}")
    sales = Counter(row.get("client_reference") for row in bookings.values() if row["status"] == "confirmed")
    violations.extend(f"duplicate_sale:{reference}" for reference, count in sales.items() if reference is not None and count > 1)
    for booking_id, row in holds.items():
        booking = bookings.get(booking_id)
        if booking is None or any(row[k] != booking[k] for k in ("flight_id", "passenger_id")):
            violations.append(f"invalid_hold:{booking_id}")
    for booking_id, row in checkins.items():
        booking = bookings.get(booking_id)
        if booking is None or booking["status"] != "confirmed" or any(row[k] != booking[k] for k in ("flight_id", "passenger_id")):
            violations.append(f"ineligible_checkin:{booking_id}")
    for booking_id, accepted in anchors.items():
        current = bookings.get(booking_id)
        if current is None or any(current.get(k) != accepted[k] for k in ("request_id", "flight_id", "passenger_id", "amount_cents", "status")):
            violations.append(f"preincident_booking_modified:{booking_id}")

    delivered = set()
    quarantined_valid = []
    for event in tables["outbox"]:
        try:
            payload = json.loads(event["payload"])
            booking = bookings.get(event["booking_id"])
            valid = (isinstance(payload, dict) and type(payload.get("schema_version")) is int and payload.get("schema_version") in (1,2)
                     and booking is not None and payload.get("booking_id") == booking["booking_id"]
                     and all(payload.get(k) == booking[k] for k in ("flight_id", "passenger_id")))
        except (ValueError, TypeError):
            valid = False
        if valid and event["status"] == "quarantined":
            quarantined_valid.append(event["event_id"])
        if valid and event["status"] == "delivered":
            delivered.add(event["booking_id"])
    if quarantined_valid:
        violations.extend(f"valid_event_discarded:{event_id}" for event_id in quarantined_valid)
    complete, incomplete = 0, []
    for request_id in requirements:
        row = by_request.get(request_id)
        if (row is not None and row["status"] == "confirmed" and row["booking_id"] in checkins
                and row["booking_id"] in delivered and len(charges[row["booking_id"]]) == 1
                and row["booking_id"] in holds):
            complete += 1
        elif (row is not None and row["status"] == "cancelled" and cancels[request_id]
                and _refunded(charges[row["booking_id"]], refunds[row["booking_id"]])):
            # The customer withdrew the request; a voided booking without that
            # evidence stays incomplete, so voiding everything never succeeds.
            complete += 1
        else:
            incomplete.append(request_id)
    recovery = complete / len(requirements) if requirements else 0.0
    # Descriptive business outcomes, never a replacement for the integrity gate.
    duplicate_bookings = [key for key, rows in charges.items() if key in bookings and len(rows) > 1]
    excess_capture_cents = sum(max(0, sum(c["amount_cents"] for c in rows) - bookings[key]["amount_cents"])
                               for key, rows in charges.items() if key in bookings)
    business_impact = {
        "duplicate_charge_bookings": len(duplicate_bookings),
        "excess_capture_cents": excess_capture_cents,
        "pending_bookings": sum(row["status"] == "pending" for row in bookings.values()),
        "incomplete_customer_requests": len(incomplete),
    }
    return {"integrity": not violations, "violations": sorted(set(violations)),
            "recovery": recovery, "completed_requests": complete, "required_requests": len(requirements),
            "incomplete_request_ids": incomplete, "quarantined_valid_events": quarantined_valid,
            "business_impact": business_impact}
