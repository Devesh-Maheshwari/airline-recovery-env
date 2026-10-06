"""Lifecycle, HTTP client and bounded operations for five local service processes."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import weakref
from pathlib import Path
from typing import Any

from . import store
from .worker import http_request


def _terminate(processes: dict[str, subprocess.Popen]) -> None:
    # Terminate all first so shutdown cost is independent of service count.
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
    for process in processes.values():
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    processes.clear()


class LiveStack:
    services = store.SERVICES

    def __init__(self) -> None:
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._db_path: Path | None = None
        self._processes: dict[str, subprocess.Popen] = {}
        self._urls: dict[str, str] = {}
        self._token = secrets.token_hex(16)
        # A stack dropped without close() still stops its workers.
        self._finalizer = weakref.finalize(self, _terminate, self._processes)

    def __enter__(self) -> "LiveStack":
        return self.start() if self._temporary is None else self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _database(self) -> Path:
        if self._db_path is None:
            raise RuntimeError("LiveStack is not started")
        return self._db_path

    def start(self, seed: int = 0) -> "LiveStack":
        if self._temporary is not None:
            return self
        self._temporary = tempfile.TemporaryDirectory(prefix="airline-recovery-")
        self._db_path = Path(self._temporary.name) / "episode.sqlite3"
        try:
            store.initialize(self._db_path, seed)
            for service in self.services:
                self._launch(service)
        except BaseException:
            self.close()
            raise
        return self

    def _launch(self, service: str) -> None:
        store.require_service(service)
        db_path = self._database()
        ready_path = db_path.parent / f"{service}.ready.json"
        ready_path.unlink(missing_ok=True)
        package_root = str(Path(__file__).resolve().parents[2])
        process_env = os.environ.copy()
        process_env["PYTHONPATH"] = package_root + os.pathsep + process_env.get("PYTHONPATH", "")
        process_env["AIRLINE_RECOVERY_EPISODE_TOKEN"] = self._token
        log_path = db_path.parent / f"{service}.worker.log"
        with log_path.open("ab") as stderr:
            process = subprocess.Popen([sys.executable, "-m", "airline_recovery.live.worker", "--db", str(db_path), "--service", service, "--ready", str(ready_path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr, env=process_env)
        self._processes[service] = process
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = log_path.read_text(errors="replace").strip().splitlines()[-1:] if log_path.exists() else []
                raise RuntimeError(f"{service} worker exited during startup; local HTTP sockets must be permitted"
                                   + (f" ({detail[0]})" if detail else ""))
            if ready_path.exists():
                try:
                    port = json.loads(ready_path.read_text())["port"]
                except (ValueError, KeyError):
                    time.sleep(0.01)
                    continue
                self._urls[service] = f"http://127.0.0.1:{port}"
                with store.connect(db_path) as db:
                    db.execute("INSERT OR REPLACE INTO service_endpoints VALUES (?,?)", (service, self._urls[service]))
                return
            time.sleep(0.01)
        self.stop_service(service)
        raise RuntimeError(f"{service} worker did not become ready")

    def close(self) -> None:
        _terminate(self._processes)
        self._urls.clear()
        self._db_path = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def stop_service(self, service: str) -> None:
        store.require_service(service)
        process = self._processes.get(service)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        # Retain the old endpoint: dependent services encounter an actual refused HTTP connection.

    def restart(self, service: str) -> dict:
        store.require_service(service)
        self.stop_service(service)
        if service == "payment":
            # In-flight acknowledgements die with the process; the provider's own record survives.
            with store.connect(self._database()) as db:
                db.execute("UPDATE charges SET state='lost' WHERE state='submitted'")
        self._launch(service)
        return {"service": service, "running": True}

    def request(self, service: str, method: str, path: str, body: dict | None = None, trace_id: str | None = None) -> dict:
        store.require_service(service)
        self._database()
        if method not in ("GET", "POST"):
            raise ValueError("Only GET and POST are supported")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("Path must be a local absolute HTTP path")
        trace_id = trace_id or uuid.uuid4().hex
        started = time.monotonic()
        status, result = http_request(self._urls[service] + path, method, body, trace_id, self._token)
        return {"status": status, "body": result, "duration_ms": round((time.monotonic() - started) * 1000, 3), "trace_id": trace_id}

    def get_config(self, service: str) -> dict:
        return store.get_config(self._database(), service)

    def patch_config(self, service: str, values: dict) -> dict:
        return store.patch_config(self._database(), service, values)

    def query(self, query: str) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 20000:
            raise ValueError("query must contain at most 20000 characters")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

        def authorize(action: int, arg1: str | None, arg2: str | None, *unused: Any) -> int:
            if action not in allowed:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and arg1 not in (*store.PUBLIC_TABLES, "sqlite_master", "sqlite_schema"):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ("load_extension", "readfile", "writefile"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db = None
        try:
            db = store.connect(self._database(), readonly=True)
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 262144)
            db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20000)
            db.set_authorizer(authorize)
            # Bound computation as well as the number of returned rows.
            budget = [0]
            def progress() -> int:
                budget[0] += 1
                return int(budget[0] > 1000)
            db.set_progress_handler(progress, 1000)
            rows, byte_count = [], 0
            for index,row in enumerate(db.execute(query)):
                if index >= 500:
                    break
                # SQLite BLOB values must not break the JSON agent protocol.
                result = {key:("0x"+value.hex() if isinstance(value, bytes) else value) for key,value in dict(row).items()}
                byte_count += len(json.dumps(result,allow_nan=False).encode())
                if byte_count > 524288:
                    raise ValueError("Read-only query response exceeds 512 KiB")
                rows.append(result)
            return rows
        except sqlite3.Error as error:
            raise ValueError(f"Read-only query rejected: {error}") from None
        finally:
            if db is not None:
                db.close()

    def admin_execute(self, sql: str, params: tuple = ()) -> None:
        with store.connect(self._database()) as db:
            try:
                db.execute(sql, params)
            except sqlite3.OperationalError as error:
                # Trusted injectors written before the hard-tier columns fill the
                # leading columns; the appended columns keep their defaults.
                short = re.fullmatch(r"table (\w+) has (\d+) columns but (\d+) values were supplied", str(error))
                if short is None:
                    raise
                columns = [row[1] for row in db.execute(f"PRAGMA table_info({short[1]})")][:int(short[3])]
                db.execute(re.sub(rf"(?is)^(\s*INSERT\s+INTO\s+{short[1]})\s*VALUES", rf"\1({','.join(columns)}) VALUES", sql), params)

    def set_clock(self, step: int) -> None:
        """Publish the episode step that workers date their rows with."""
        if type(step) is not int or step < 0:
            raise ValueError("step must be a nonnegative integer")
        self.admin_execute("UPDATE episode_clock SET step=?", (step,))

    def install_log_retention(self, limit: int) -> None:
        """Keep only the newest `limit` request_logs rows; older telemetry silently disappears."""
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError("limit must be an integer from 1 to 100000")
        self.admin_execute("DROP TRIGGER IF EXISTS request_logs_retention")
        self.admin_execute(f"CREATE TRIGGER request_logs_retention AFTER INSERT ON request_logs BEGIN DELETE FROM request_logs WHERE id <= NEW.id - {limit}; END")

    def lookup(self, sql: str, params: tuple = ()) -> list[dict]:
        """Trusted targeted read for the traffic generator; not an agent tool."""
        with store.connect(self._database()) as db:
            return [dict(row) for row in db.execute(sql, params)]

    def inspect(self) -> dict:
        # The provider ledger is private to the agent but the grader's truth for payments.
        with store.connect(self._database()) as db:
            state = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in (*store.PUBLIC_TABLES, "provider_ledger")}
        state["processes"] = {name: process.poll() is None for name, process in self._processes.items()}
        return state

    def pump(self, limit: int = 10) -> dict:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        config = self.get_config("checkin")
        limit = min(limit, config["batch_size"])
        delivered = attempted = 0
        blocked_event_id = error = None
        with store.connect(self._database()) as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY event_id LIMIT ?", (limit,))]
        for row in rows:
            with store.connect(self._database()) as db:
                db.execute("UPDATE outbox SET attempts=attempts+1 WHERE event_id=? AND status='pending'", (row["event_id"],))
                attempts = db.execute("SELECT attempts FROM outbox WHERE event_id=?", (row["event_id"],)).fetchone()[0]
            attempted += 1
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                # Sending the malformed payload to the consumer still records a real HTTP rejection.
                payload = row["payload"]
            result = self.request("checkin", "POST", "/consume", {"event_id": row["event_id"], "booking_id": row["booking_id"], "payload": payload})
            if result["status"] != 200:
                blocked_event_id, error = row["event_id"], result["body"].get("error", "event delivery failed")
                # A head event that keeps failing trips the breaker; re-enabling the consumer alone treats the symptom.
                if 0 < config["auto_pause_after_attempts"] <= attempts and config["consumer_enabled"]:
                    self.patch_config("checkin", {"consumer_enabled": False})
                    with store.connect(self._database()) as db:
                        db.execute("INSERT INTO request_logs(service,method,path,status,duration_ms,trace_id,message) VALUES (?,?,?,?,?,?,?)",
                                   ("checkin", "POST", "/consume", result["status"], result["duration_ms"], result["trace_id"], "consumer paused by circuit breaker after repeated delivery failure"))
                break
            with store.connect(self._database()) as db:
                changed = db.execute("UPDATE outbox SET status='delivered' WHERE event_id=? AND status='pending'", (row["event_id"],)).rowcount
            delivered += changed
        with store.connect(self._database()) as db:
            remaining = db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
        return {"attempted": attempted, "delivered": delivered, "remaining": remaining, "blocked_event_id": blocked_event_id, "error": error}

    def quarantine(self, event_id: str) -> dict:
        with store.connect(self._database()) as db:
            changed = db.execute("UPDATE outbox SET status='quarantined' WHERE event_id=? AND status='pending'", (event_id,)).rowcount
            if not changed:
                raise ValueError("event_id must identify a pending event")
        return {"event_id": event_id, "status": "quarantined"}

    def invalidate_cache(self, service: str, flight_id: str | None = None) -> dict:
        store.require_service(service)
        if flight_id is not None and (not isinstance(flight_id, str) or not 1 <= len(flight_id) <= 20):
            raise ValueError("flight_id must be a string of 1 to 20 characters")
        cleared = 0
        if service == "pricing":
            with store.connect(self._database()) as db:
                cleared = (db.execute("DELETE FROM cache WHERE key=?", (f"quote:{flight_id}",)) if flight_id
                           else db.execute("DELETE FROM cache WHERE key LIKE 'quote:%'")).rowcount
        return {"service": service, "cleared": cleared}

    def reconcile(self, booking_id: str, *, idempotency_key: str | None = None,
                  existing_charge_id: str | None = None) -> dict:
        with store.connect(self._database()) as db:
            booking = db.execute("SELECT * FROM bookings WHERE booking_id=?", (booking_id,)).fetchone()
        if booking is None:
            raise ValueError("booking_id was not found")
        body = {key: booking[key] for key in ("request_id", "flight_id", "passenger_id")}
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        if existing_charge_id is not None:
            body["existing_charge_id"] = existing_charge_id
        return self.request("booking", "POST", "/book", body)

    def logs(self, service: str | None = None, limit: int = 30) -> list[dict]:
        if service is not None:
            store.require_service(service)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        where, params = ("WHERE service=?", (service, limit)) if service else ("", (limit,))
        with store.connect(self._database()) as db:
            rows = db.execute(f"SELECT * FROM request_logs {where} ORDER BY id DESC LIMIT ?", params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def metrics(self, service: str | None = None) -> dict:
        if service is not None:
            store.require_service(service)
        selected = (service,) if service else self.services
        result: dict[str, Any] = {"services": {}}
        with store.connect(self._database()) as db:
            for name in selected:
                counts = {str(row["status"]): row["count"] for row in db.execute("SELECT status,COUNT(*) AS count FROM request_logs WHERE service=? GROUP BY status", (name,))}
                process = self._processes.get(name)
                result["services"][name] = {"running": bool(process is not None and process.poll() is None), "requests": sum(counts.values()), "errors": sum(count for status, count in counts.items() if int(status) >= 400), "status_counts": counts, "health": "healthy"}
                if name == "payment":
                    # Deadline failures in the recent window read as sickness even when every capture committed.
                    recent = [row[0] for row in db.execute("SELECT status FROM request_logs WHERE service='payment' ORDER BY id DESC LIMIT 50")]
                    if recent and sum(status == 504 for status in recent) > 0.3 * len(recent):
                        result["services"][name]["health"] = "degraded"
            for table, statuses in (("outbox", ("pending", "delivered", "quarantined")), ("bookings", ("pending", "confirmed"))):
                counts = {row[0]: row[1] for row in db.execute(f"SELECT status,COUNT(*) FROM {table} GROUP BY status")}
                result[table] = {status: counts.get(status, 0) for status in statuses}
        return result
