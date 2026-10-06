"""Durable state and validated configuration for the executed airline services."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SERVICES = ("inventory", "pricing", "payment", "booking", "checkin")
DEFAULT_CONFIG = {
    "inventory": {"enforce_capacity": True},
    "pricing": {"cache_enabled": True},
    "payment": {"provider_latency_ms": 80, "idempotency_enabled": True, "lookup_quota": 3, "idempotency_window_steps": 1000},
    "booking": {"payment_timeout_ms": 200, "payment_idempotency_enabled": True, "payment_key_version": 1, "validate_price": True},
    "checkin": {"consumer_enabled": True, "batch_size": 10, "accepted_schema": 1, "auto_pause_after_attempts": 0},
}
BOUNDS = {"provider_latency_ms": (0, 10000), "payment_timeout_ms": (1, 10000), "payment_key_version": (1, 2), "batch_size": (1, 50), "accepted_schema": (1, 2),
          "auto_pause_after_attempts": (0, 50), "lookup_quota": (0, 20), "idempotency_window_steps": (1, 1000)}
# External conditions the operator observes but cannot patch.
READ_ONLY_FIELDS = frozenset({("payment", "provider_latency_ms"), ("payment", "lookup_quota"), ("payment", "idempotency_window_steps")})
PUBLIC_TABLES = ("flights", "bookings", "holds", "charges", "outbox", "checkins", "service_config", "cache", "request_logs", "customer_events", "refunds", "fare_holds")
PRIVATE_TABLES = ("service_endpoints", "episode_clock", "provider_ledger", "provider_plan")


def configuration_contracts() -> dict[str, Any]:
    """Public operator schemas, with no baseline values or incident information."""
    contracts = {}
    for service, fields in DEFAULT_CONFIG.items():
        properties = {}
        for name, exemplar in fields.items():
            schema = {"type": "boolean" if type(exemplar) is bool else "integer"}
            if name in BOUNDS:
                schema.update(minimum=BOUNDS[name][0], maximum=BOUNDS[name][1])
            schema["readOnly"] = (service, name) in READ_ONLY_FIELDS
            properties[name] = schema
        contracts[service] = {
            "observed_fields": properties,
            "patch_schema": {"type": "object", "minProperties": 1,
                "additionalProperties": False,
                "properties": {name: dict(schema) for name, schema in properties.items()
                               if not schema["readOnly"]}},
        }
    return contracts


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *exc: Any) -> bool:
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    # as_uri percent-encodes characters such as # and ? in a temporary directory name.
    db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro" if readonly else str(path), uri=readonly, timeout=10.0, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    return db


def initialize(path: str | Path, seed: int = 0) -> None:
    # The episode seed identifies the workload; catalog facts are intentionally stable.
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE flights(flight_id TEXT PRIMARY KEY, capacity INTEGER NOT NULL, price_cents INTEGER NOT NULL, version INTEGER NOT NULL);
        CREATE TABLE bookings(booking_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, flight_id TEXT NOT NULL, passenger_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','confirmed','cancelled')), client_reference TEXT, confirmed_at INTEGER);
        CREATE TABLE holds(booking_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, passenger_id TEXT NOT NULL);
        CREATE TABLE charges(charge_id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, amount_cents INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'captured' CHECK(state IN ('captured','submitted','declined','lost')), created_step INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX charges_key ON charges(idempotency_key);
        CREATE INDEX charges_booking ON charges(booking_id);
        CREATE TABLE episode_clock(step INTEGER NOT NULL);
        INSERT INTO episode_clock(step) VALUES (0);
        CREATE TABLE provider_ledger(attempt_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, booking_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('captured','declined','pending')), final_state TEXT CHECK(final_state IN ('captured','declined')), settles_at_step INTEGER, created_step INTEGER NOT NULL);
        CREATE INDEX provider_ledger_key ON provider_ledger(idempotency_key);
        CREATE INDEX provider_ledger_booking ON provider_ledger(booking_id);
        CREATE TABLE provider_plan(idempotency_key TEXT PRIMARY KEY, outcome TEXT NOT NULL CHECK(outcome IN ('captured','declined','pending')), final_state TEXT, settles_at_step INTEGER);
        CREATE TABLE customer_events(event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('cancel')), step INTEGER NOT NULL);
        CREATE TABLE refunds(refund_id TEXT PRIMARY KEY, charge_id TEXT NOT NULL, booking_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, created_step INTEGER NOT NULL);
        CREATE TABLE fare_holds(hold_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, price_cents INTEGER NOT NULL, until_step INTEGER NOT NULL);
        CREATE TABLE outbox(event_id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','delivered','quarantined')), attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE checkins(booking_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, passenger_id TEXT NOT NULL);
        CREATE TABLE service_config(service TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE service_endpoints(service TEXT PRIMARY KEY, url TEXT NOT NULL);
        CREATE TABLE cache(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE request_logs(id INTEGER PRIMARY KEY, service TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, status INTEGER NOT NULL, duration_ms REAL NOT NULL, trace_id TEXT NOT NULL, message TEXT NOT NULL);
        """)
        db.executemany("INSERT INTO flights VALUES (?,?,?,?)", [("F100", 120, 20000, 1), ("F200", 150, 25000, 1)])
        db.executemany("INSERT INTO service_config VALUES (?,?)", [(name, json.dumps(value)) for name, value in DEFAULT_CONFIG.items()])


def clock(db: sqlite3.Connection) -> int:
    """The episode step the environment last published; 0 outside an episode."""
    return db.execute("SELECT step FROM episode_clock").fetchone()[0]


def live_fare_hold(db: sqlite3.Connection, flight_id: str, step: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM fare_holds WHERE flight_id=? AND until_step>=? ORDER BY rowid DESC LIMIT 1", (flight_id, step)).fetchone()


def captured_charges(db: sqlite3.Connection, booking_id: str) -> list[sqlite3.Row]:
    """Charges the provider confirms captured; local state stands in only where the provider was never consulted."""
    rows = db.execute("SELECT c.* FROM charges c JOIN provider_ledger l ON l.attempt_id=c.charge_id WHERE c.booking_id=? AND l.state='captured' ORDER BY c.rowid", (booking_id,)).fetchall()
    if rows or db.execute("SELECT 1 FROM provider_ledger WHERE booking_id=? LIMIT 1", (booking_id,)).fetchone():
        return rows
    return db.execute("SELECT * FROM charges WHERE booking_id=? AND state='captured' ORDER BY rowid", (booking_id,)).fetchall()


def require_service(service: str) -> None:
    if service not in SERVICES:
        raise ValueError(f"Unknown service: {service}")


def get_config(path: str | Path, service: str) -> dict[str, Any]:
    require_service(service)
    with connect(path) as db:
        return json.loads(db.execute("SELECT value FROM service_config WHERE service=?", (service,)).fetchone()[0])


def patch_config(path: str | Path, service: str, values: dict[str, Any]) -> dict[str, Any]:
    require_service(service)
    if not isinstance(values, dict) or not values:
        raise ValueError("Configuration values must be a nonempty object")
    for key, value in values.items():
        if key not in DEFAULT_CONFIG[service]:
            raise ValueError(f"Unknown configuration field for {service}: {key}")
        expected = type(DEFAULT_CONFIG[service][key])
        if type(value) is not expected:
            raise ValueError(f"{key} must be {expected.__name__}")
        if key in BOUNDS and not BOUNDS[key][0] <= value <= BOUNDS[key][1]:
            raise ValueError(f"{key} must be between {BOUNDS[key][0]} and {BOUNDS[key][1]}")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        current = json.loads(db.execute("SELECT value FROM service_config WHERE service=?", (service,)).fetchone()[0])
        current.update(values)
        db.execute("UPDATE service_config SET value=? WHERE service=?", (json.dumps(current), service))
    return current
