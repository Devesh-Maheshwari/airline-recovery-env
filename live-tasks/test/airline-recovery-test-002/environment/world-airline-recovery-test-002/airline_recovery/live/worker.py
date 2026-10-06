"""Independent HTTP worker process. Every service operation executes durable work."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import store


class BusinessError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def required_string(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value or len(value) > 200:
        raise BusinessError(400, f"{key} must be a nonempty string of at most 200 characters")
    return value


def positive_integer(body: dict, key: str) -> int:
    value = body.get(key)
    if type(value) is not int or not 1 <= value <= 100_000_000:
        raise BusinessError(400, f"{key} must be a positive bounded integer")
    return value


# Loopback calls between the environment and the workers. Provider timeouts are simulated from
# config, never by this socket timeout; it only has to outlast a stalled worker, including one
# waiting out SQLite's 10 s busy timeout (store.connect), so reset-time checks do not fail at random.
LOOPBACK_TIMEOUT_S = 30


def http_request(url: str, method: str, body: dict | None, trace_id: str, token: str = "") -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Trace-ID": trace_id, "X-Episode-Token": token})
    # Loopback workers must not be routed through a machine's configured HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=LOOPBACK_TIMEOUT_S) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except (ValueError, OSError):
            return error.code, {"error": "upstream returned a non-JSON error"}
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
        return 503, {"error": "service unavailable: connection failed"}


class Service:
    def __init__(self, db_path: str, name: str, token: str = ""):
        self.db_path, self.name, self.token = db_path, name, token

    def upstream(self, service: str, method: str, path: str, body: dict | None, trace_id: str) -> dict:
        with store.connect(self.db_path) as db:
            row = db.execute("SELECT url FROM service_endpoints WHERE service=?", (service,)).fetchone()
        if row is None:
            raise BusinessError(503, f"{service} unavailable: no endpoint")
        status, result = http_request(row[0] + path, method, body, trace_id, self.token)
        if status >= 400:
            raise BusinessError(status, f"{service}: {result.get('error', 'request failed')}")
        return result

    def route(self, method: str, path: str, body: dict, trace_id: str) -> tuple[int, dict]:
        parsed = urllib.parse.urlsplit(path)
        if method == "GET" and parsed.path == "/health":
            return 200, {"service": self.name, "ready": True}
        config = store.get_config(self.db_path, self.name)
        if self.name == "pricing" and method == "GET" and parsed.path == "/quote":
            flight_id = urllib.parse.parse_qs(parsed.query).get("flight_id", [""])[0]
            with store.connect(self.db_path) as db:
                if config["cache_enabled"]:
                    cached = db.execute("SELECT value FROM cache WHERE key=?", (f"quote:{flight_id}",)).fetchone()
                    if cached:
                        return 200, json.loads(cached[0])
                flight = db.execute("SELECT * FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
                if flight is None:
                    raise BusinessError(404, "flight not found")
                # A promised fare lives only in its cache row; booking validation honours the hold.
                quote = {"flight_id": flight_id, "amount_cents": flight["price_cents"], "version": flight["version"]}
                if config["cache_enabled"]:
                    db.execute("INSERT OR REPLACE INTO cache VALUES (?,?)", (f"quote:{flight_id}", json.dumps(quote)))
                return 200, quote
        if self.name == "inventory" and method == "POST" and parsed.path == "/reserve":
            booking_id, flight_id, passenger_id = (required_string(body, k) for k in ("booking_id", "flight_id", "passenger_id"))
            with store.connect(self.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT * FROM holds WHERE booking_id=?", (booking_id,)).fetchone()
                if existing:
                    if (existing["flight_id"], existing["passenger_id"]) != (flight_id, passenger_id):
                        raise BusinessError(409, "reservation identity conflict")
                    return 200, {"reserved": True, "booking_id": booking_id}
                flight = db.execute("SELECT capacity FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
                if flight is None:
                    raise BusinessError(404, "flight not found")
                occupied = db.execute("SELECT COUNT(*) FROM holds WHERE flight_id=?", (flight_id,)).fetchone()[0]
                if config["enforce_capacity"] and occupied >= flight[0]:
                    raise BusinessError(409, "flight sold out: capacity exhausted")
                db.execute("INSERT INTO holds VALUES (?,?,?)", (booking_id, flight_id, passenger_id))
            return 200, {"reserved": True, "booking_id": booking_id}
        if self.name == "payment" and method == "POST" and parsed.path == "/capture":
            return self.capture(body, config)
        if self.name == "payment" and method == "POST" and parsed.path == "/settle":
            return self.settle()
        if self.name == "payment" and method == "GET" and parsed.path == "/provider/lookup":
            key = urllib.parse.parse_qs(parsed.query).get("idempotency_key", [""])[0]
            with store.connect(self.db_path) as db:
                row = db.execute("SELECT * FROM provider_ledger WHERE idempotency_key=? ORDER BY rowid DESC LIMIT 1", (key,)).fetchone()
            if row is None:
                raise BusinessError(404, "idempotency key unknown to provider")
            return 200, {k: row[k] for k in ("idempotency_key", "state", "booking_id", "amount_cents")}
        if self.name == "payment" and method == "POST" and parsed.path == "/refund":
            return self.refund(required_string(body, "charge_id"))
        if self.name == "booking" and method == "POST" and parsed.path == "/book":
            return self.book(body, config, trace_id)
        if self.name == "booking" and method == "POST" and parsed.path == "/void":
            return self.void(required_string(body, "booking_id"), trace_id)
        if self.name == "checkin" and method == "POST" and parsed.path == "/consume":
            if not config["consumer_enabled"]:
                raise BusinessError(503, "check-in consumer is disabled")
            required_string(body, "event_id")
            booking_id = required_string(body, "booking_id")
            payload = body.get("payload")
            if not isinstance(payload, dict):
                raise BusinessError(422, "invalid event payload: expected object")
            version = payload.get("schema_version")
            if type(version) is not int or version not in (1, 2):
                raise BusinessError(422, "invalid event schema_version")
            if version > config["accepted_schema"]:
                raise BusinessError(422, f"unsupported event schema_version {version}")
            try:
                identity = tuple(required_string(payload, k) for k in ("booking_id", "flight_id", "passenger_id"))
            except BusinessError as error:
                raise BusinessError(422, f"invalid event payload: {error.message}") from error
            if identity[0] != booking_id:
                raise BusinessError(422, "event booking identity mismatch")
            with store.connect(self.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                booking = db.execute("SELECT * FROM bookings WHERE booking_id=?", (booking_id,)).fetchone()
                if booking is None or booking["status"] != "confirmed":
                    raise BusinessError(409, "booking is not confirmed")
                if (booking["booking_id"], booking["flight_id"], booking["passenger_id"]) != identity:
                    raise BusinessError(422, "event identity does not match booking")
                existing = db.execute("SELECT * FROM checkins WHERE booking_id=?", (booking_id,)).fetchone()
                if existing and tuple(existing) != identity:
                    raise BusinessError(409, "existing check-in identity conflict")
                db.execute("INSERT OR IGNORE INTO checkins VALUES (?,?,?)", identity)
            return 200, {"booking_id": booking_id, "checked_in": True}
        raise BusinessError(404, "endpoint not found")

    def capture(self, body: dict, config: dict) -> tuple[int, dict]:
        booking_id, key = (required_string(body, k) for k in ("booking_id", "idempotency_key"))
        amount, timeout_ms = (positive_integer(body, k) for k in ("amount_cents", "timeout_ms"))
        with store.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            clock = store.clock(db)
            previous = db.execute("SELECT * FROM charges WHERE idempotency_key=? ORDER BY rowid DESC LIMIT 1", (key,)).fetchone() if config["idempotency_enabled"] else None
            # A key the provider has forgotten protects nothing: the retry is a new charge.
            existing = previous if previous and clock - previous["created_step"] <= config["idempotency_window_steps"] else None
            if existing:
                ledger = db.execute("SELECT state FROM provider_ledger WHERE attempt_id=?", (existing["charge_id"],)).fetchone()
                truth = ledger["state"] if ledger else "captured"
                if truth == "pending":
                    raise BusinessError(504, "capture outcome pending at provider")
                if truth == "captured":
                    if existing["booking_id"] != booking_id or existing["amount_cents"] != amount:
                        raise BusinessError(409, "payment idempotency key conflicts with capture identity or amount")
                    charge = dict(existing)
                    if charge["state"] == "submitted":
                        # This acknowledgement arrived, so the earlier lost one no longer leaves the row in doubt.
                        db.execute("UPDATE charges SET state='captured' WHERE charge_id=?", (charge["charge_id"],))
                        charge["state"] = "captured"
                else:
                    existing = None  # a declined attempt leaves the key free for a fresh capture
            if existing is None:
                ordinal = db.execute("SELECT COUNT(*) FROM charges WHERE booking_id=?",(booking_id,)).fetchone()[0]+1
                charge = {"charge_id": "chg_" + uuid.uuid5(uuid.NAMESPACE_URL,f"airline-recovery:charge:{booking_id}:{ordinal}").hex, "booking_id": booking_id, "idempotency_key": key, "amount_cents": amount, "state": "captured", "created_step": clock}
                # The provider's planned outcome applies only to the first capture ever made with a key.
                plan = db.execute("SELECT * FROM provider_plan WHERE idempotency_key=?", (key,)).fetchone() if previous is None and config["idempotency_enabled"] else None
                ledger = {"state": "captured", "final_state": "captured", "settles_at_step": None}
                if plan:
                    ledger = {"state": plan["outcome"], "final_state": plan["final_state"], "settles_at_step": plan["settles_at_step"]}
                    charge["state"] = "submitted"
                    db.execute("DELETE FROM provider_plan WHERE idempotency_key=?", (key,))
                db.execute("INSERT INTO charges(charge_id,booking_id,idempotency_key,amount_cents,state,created_step) VALUES (?,?,?,?,?,?)", tuple(charge.values()))
                db.execute("INSERT INTO provider_ledger(attempt_id,idempotency_key,booking_id,amount_cents,state,final_state,settles_at_step,created_step) VALUES (?,?,?,?,?,?,?,?)",
                           (charge["charge_id"], key, booking_id, amount, ledger["state"], ledger["final_state"], ledger["settles_at_step"], clock))
        # Commit occurs BEFORE the injected deadline error. This models a lost ACK,
        # not a physical provider sleep or a real socket timeout.
        if charge["state"] == "submitted" or config["provider_latency_ms"] > timeout_ms:
            raise BusinessError(504, "capture acknowledgement exceeded caller deadline; payment outcome is uncertain")
        return 200, charge

    def settle(self) -> tuple[int, dict]:
        """Provider outcomes become visible on schedule, never earlier."""
        with store.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            clock = store.clock(db)
            settled = db.execute("UPDATE provider_ledger SET state=final_state WHERE state='pending' AND final_state IS NOT NULL AND settles_at_step<=?", (clock,)).rowcount
            settled += db.execute("UPDATE charges SET state=(SELECT l.state FROM provider_ledger l WHERE l.attempt_id=charges.charge_id) WHERE state='submitted' AND EXISTS "
                                  "(SELECT 1 FROM provider_ledger l WHERE l.attempt_id=charges.charge_id AND l.state IN ('captured','declined') AND l.settles_at_step<=?)", (clock,)).rowcount
        return 200, {"settled": settled}

    def refund(self, charge_id: str) -> tuple[int, dict]:
        with store.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM refunds WHERE charge_id=?", (charge_id,)).fetchone()
            if existing:
                return 200, dict(existing)
            charge = db.execute("SELECT * FROM charges WHERE charge_id=?", (charge_id,)).fetchone()
            if charge is None or charge_id not in {row["charge_id"] for row in store.captured_charges(db, charge["booking_id"])}:
                raise BusinessError(409, "refund requires a charge the provider captured")
            refund = {"refund_id": "rfd_" + uuid.uuid5(uuid.NAMESPACE_URL, "airline-recovery:refund:" + charge_id).hex, "charge_id": charge_id,
                      "booking_id": charge["booking_id"], "amount_cents": charge["amount_cents"], "created_step": store.clock(db)}
            db.execute("INSERT INTO refunds(refund_id,charge_id,booking_id,amount_cents,created_step) VALUES (?,?,?,?,?)", tuple(refund.values()))
        return 200, refund

    def void(self, booking_id: str, trace_id: str) -> tuple[int, dict]:
        with store.connect(self.db_path) as db:
            booking = db.execute("SELECT * FROM bookings WHERE booking_id=?", (booking_id,)).fetchone()
            if booking is None:
                raise BusinessError(404, "booking not found")
            if booking["status"] == "confirmed":
                raise BusinessError(409, "confirmed booking cannot be voided")
            if booking["status"] == "cancelled":
                refunded = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM refunds WHERE booking_id=?", (booking_id,)).fetchone()[0]
                return 200, {"booking_id": booking_id, "status": "cancelled", "refunded_cents": refunded}
            if db.execute("SELECT 1 FROM provider_ledger WHERE booking_id=? AND state='pending'", (booking_id,)).fetchone():
                raise BusinessError(409, "payment outcome unresolved")
            charges = [row["charge_id"] for row in store.captured_charges(db, booking_id)]
        # Money goes back before the seat is released, so a refund outage leaves the booking retryable.
        refunded = sum(self.upstream("payment", "POST", "/refund", {"charge_id": charge_id}, trace_id)["amount_cents"] for charge_id in charges)
        with store.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("UPDATE bookings SET status='cancelled' WHERE booking_id=? AND status='pending'", (booking_id,)).rowcount:
                db.execute("DELETE FROM holds WHERE booking_id=?", (booking_id,))
        return 200, {"booking_id": booking_id, "status": "cancelled", "refunded_cents": refunded}

    def book(self, body: dict, config: dict, trace_id: str) -> tuple[int, dict]:
        request_id, flight_id, passenger_id = (required_string(body, k) for k in ("request_id", "flight_id", "passenger_id"))
        if "idempotency_key" in body and "existing_charge_id" in body:
            raise BusinessError(400, "idempotency_key and existing_charge_id are mutually exclusive")
        for option in ("idempotency_key", "existing_charge_id"):
            if option in body:
                required_string(body, option)
        with store.connect(self.db_path) as db:
            existing = db.execute("SELECT * FROM bookings WHERE request_id=?", (request_id,)).fetchone()
        if existing is None and any(k in body for k in ("idempotency_key", "existing_charge_id")):
            raise BusinessError(409, "payment recovery options require an existing booking")
        if existing:
            if (existing["flight_id"], existing["passenger_id"]) != (flight_id, passenger_id):
                raise BusinessError(409, "request_id conflicts with existing booking identity")
            if existing["status"] == "cancelled":
                raise BusinessError(409, "booking is cancelled")
            if "existing_charge_id" in body:
                return self.adopt_charge(existing["booking_id"], body["existing_charge_id"])
            if existing["status"] == "confirmed":
                return 200, {"booking_id": existing["booking_id"], "status": "confirmed"}
        quote = None if existing else self.upstream("pricing", "GET", "/quote?" + urllib.parse.urlencode({"flight_id": flight_id}), None, trace_id)
        with store.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            # Re-read inside the transaction to serialize concurrent retries.
            existing = db.execute("SELECT * FROM bookings WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if (existing["flight_id"], existing["passenger_id"]) != (flight_id, passenger_id):
                    raise BusinessError(409, "request_id conflicts with existing booking identity")
                if existing["status"] == "cancelled":
                    raise BusinessError(409, "booking is cancelled")
                if existing["status"] == "confirmed":
                    return 200, {"booking_id": existing["booking_id"], "status": "confirmed"}
                booking_id, amount = existing["booking_id"], existing["amount_cents"]
            else:
                flight = db.execute("SELECT price_cents,version FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
                if flight is None:
                    raise BusinessError(404, "flight not found")
                held = store.live_fare_hold(db, flight_id, store.clock(db))
                promised = held is not None and quote.get("amount_cents") == held["price_cents"]
                if config["validate_price"] and not promised and (quote.get("amount_cents") != flight[0] or quote.get("version") != flight[1]):
                    raise BusinessError(409, "stale pricing quote: fare or version does not match current inventory")
                booking_id, amount = "bkg_" + uuid.uuid5(uuid.NAMESPACE_URL,"airline-recovery:booking:"+request_id).hex, quote["amount_cents"]
                db.execute("INSERT INTO bookings(booking_id,request_id,flight_id,passenger_id,amount_cents,status,client_reference) VALUES (?,?,?,?,?,'pending',?)", (booking_id, request_id, flight_id, passenger_id, amount, body.get("client_reference") if isinstance(body.get("client_reference"), str) else None))
        try:
            self.upstream("inventory", "POST", "/reserve", {"booking_id": booking_id, "flight_id": flight_id, "passenger_id": passenger_id}, trace_id)
        except BusinessError as error:
            if error.status == 409:
                with store.connect(self.db_path) as db:
                    db.execute("DELETE FROM bookings WHERE booking_id=? AND status='pending' AND NOT EXISTS (SELECT 1 FROM holds WHERE booking_id=?) AND NOT EXISTS (SELECT 1 FROM charges WHERE booking_id=?)", (booking_id, booking_id, booking_id))
            raise
        prefix = "booking:" if config["payment_key_version"] == 1 else "booking-v2:"
        key = body.get("idempotency_key", prefix + booking_id) if config["payment_idempotency_enabled"] else "attempt:" + uuid.uuid4().hex
        self.upstream("payment", "POST", "/capture", {"booking_id": booking_id, "idempotency_key": key, "amount_cents": amount, "timeout_ms": config["payment_timeout_ms"]}, trace_id)
        with store.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            self.confirm(db, booking_id, flight_id, passenger_id)
        return 200, {"booking_id": booking_id, "status": "confirmed"}

    def adopt_charge(self, booking_id: str, charge_id: str) -> tuple[int, dict]:
        """Finalize a caller-selected committed capture without capturing again."""
        with store.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            booking = db.execute("SELECT * FROM bookings WHERE booking_id=?", (booking_id,)).fetchone()
            charge = db.execute("SELECT * FROM charges WHERE charge_id=?", (charge_id,)).fetchone()
            captured = [row["charge_id"] for row in store.captured_charges(db, booking_id)]
            hold = db.execute("SELECT * FROM holds WHERE booking_id=?", (booking_id,)).fetchone()
            if (booking is None or charge is None or charge["booking_id"] != booking_id
                    or charge["amount_cents"] != booking["amount_cents"] or captured != [charge_id]):
                raise BusinessError(409, "adoption requires the sole charge matching this booking and accepted amount")
            if hold is None or any(hold[k] != booking[k] for k in ("flight_id", "passenger_id")):
                raise BusinessError(409, "adoption requires a matching reserved seat")
            self.confirm(db, booking_id, booking["flight_id"], booking["passenger_id"])
        return 200, {"booking_id": booking_id, "status": "confirmed", "adopted_charge_id": charge_id}

    @staticmethod
    def confirm(db, booking_id: str, flight_id: str, passenger_id: str) -> None:
        status = db.execute("SELECT status FROM bookings WHERE booking_id=?", (booking_id,)).fetchone()[0]
        if status != "confirmed":
            db.execute("UPDATE bookings SET status='confirmed', confirmed_at=? WHERE booking_id=?", (store.clock(db), booking_id))
            event = {"schema_version": 1, "booking_id": booking_id, "flight_id": flight_id, "passenger_id": passenger_id}
            ordinal = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]+1
            event_id = f"evt_{ordinal:08d}_{booking_id}"
            db.execute("INSERT INTO outbox VALUES (?,?,?,'pending',0)", (event_id, booking_id, json.dumps(event)))

    def log(self, method: str, path: str, status: int, duration: float, trace_id: str, message: str) -> None:
        with store.connect(self.db_path) as db:
            db.execute("INSERT INTO request_logs(service,method,path,status,duration_ms,trace_id,message) VALUES (?,?,?,?,?,?,?)", (self.name, method, path, status, duration, trace_id, message))


def serve(db_path: str, service_name: str, ready_path: str, token: str = "") -> None:
    service = Service(db_path, service_name, token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.handle_request()

        def do_POST(self) -> None:
            self.handle_request()

        def handle_request(self) -> None:
            started = time.monotonic()
            trace_id = self.headers.get("X-Trace-ID", uuid.uuid4().hex)[:200]
            try:
                # A recycled port must never let another episode's services in.
                if self.headers.get("X-Episode-Token", "") != token:
                    raise BusinessError(403, "request does not belong to this episode")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 65536:
                    raise BusinessError(413, "request body exceeds 64 KiB")
                body = json.loads(self.rfile.read(size)) if size else {}
                if not isinstance(body, dict):
                    raise BusinessError(400, "request body must be an object")
                status, result = service.route(self.command, self.path, body, trace_id)
            except BusinessError as error:
                status, result = error.status, {"error": error.message}
            except (ValueError, KeyError, TypeError):
                status, result = 400, {"error": "invalid request or stored data format"}
            except Exception as error:
                # Report type, never local filesystem locations or SQL connection details.
                status, result = 500, {"error": f"service operation failed ({type(error).__name__})"}
            elapsed = (time.monotonic() - started) * 1000
            try:
                service.log(self.command, self.path, status, elapsed, trace_id, result.get("error", "ok"))
            except Exception:
                status, result = 503, {"error": "telemetry persistence unavailable"}
            encoded = json.dumps(result).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    Path(ready_path).write_text(json.dumps({"port": server.server_address[1]}))
    # A parent killed without cleanup must not leave this worker listening.
    parent = os.getppid()
    def exit_with_parent() -> None:
        while os.getppid() == parent:
            time.sleep(1)
        os._exit(0)
    threading.Thread(target=exit_with_parent, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--service", required=True, choices=store.SERVICES)
    parser.add_argument("--ready", required=True)
    args = parser.parse_args()
    serve(args.db, args.service, args.ready, os.environ.get("AIRLINE_RECOVERY_EPISODE_TOKEN", ""))


if __name__ == "__main__":
    main()
