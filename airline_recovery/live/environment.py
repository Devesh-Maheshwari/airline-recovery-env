"""Agent episode boundary around live HTTP workers and transaction verification."""
import copy
import json
import random
import urllib.parse
import uuid
from collections import Counter

from . import hard_injector, hardcases
from .scenarios import case_for, task_manifest
from .store import configuration_contracts
from .verification import assess

VERIFICATION_START_STEP = 6
REQUIRED_PROBES = 2
# A delayed incident arrives at a seeded step in this range, before verification can start.
DELAYED_STEP_RANGE = (2, 5)
REWARD_WEIGHTS = {"availability":0.2, "incident_recovery":0.6, "verified":0.2}
HARD_REWARD_WEIGHTS = {"availability":0.2, "incident_recovery":0.45, "verified":0.15, "revenue_retained":0.10, "success":0.10}
HARD_COST = {"reads":0.004, "mutations":0.02, "provider_lookups":0.03, "cap":0.25}
ABANDON_AFTER_RETRIES = 4
MUTATING_TOOLS = {"patch_config", "restart_service", "quarantine_event",
                  "invalidate_cache", "reconcile_booking", "replay_events"}
MUTATING_TOOLS_HARD = MUTATING_TOOLS | {"void_booking"}


def _schema(name, description, properties=None, required=()):
    return {"name": name, "description": description,
            "parameters": {"type":"object", "properties":properties or {}, "required":list(required), "additionalProperties":False}}


SERVICE = {"type":"string", "enum":["inventory", "pricing", "payment", "booking", "checkin"]}
TOOLS = [
    _schema("get_metrics", "Inspect measured HTTP results, process availability and queue depth.", {"service":SERVICE}),
    _schema("get_logs", "Read recent executed HTTP request logs and trace IDs.", {"service":SERVICE}),
    _schema("get_config", "Read current deployed settings. No known-good baseline is supplied.", {"service":SERVICE}),
    _schema("patch_config", "Apply validated operator settings atomically; see configuration_contracts in the reset observation for legal fields, types and bounds. Provider latency is an observed external condition and cannot be patched. Booking payment_timeout_ms must cover provider_latency_ms. Payment idempotency protects retries; turning it off can double-charge. Checkin accepted_schema=2 accepts schema1 and2;1 accepts only1. Settings are not themselves graded.",
            {"service":SERVICE, "values":{"type":"object"}}, ("service","values")),
    _schema("restart_service", "Restart an unavailable process, preserving its database records and deployed settings.", {"service":SERVICE}, ("service",)),
    _schema("query_sql", "Run bounded read-only SQLite SELECT over flights, bookings, holds, charges, outbox, checkins, request_logs, service_config, cache. Inspect table schemas through sqlite_master. Writes/PRAGMA/ATTACH/extensions are prohibited.", {"query":{"type":"string","maxLength":4000}}, ("query",)),
    _schema("replay_events", "Retry pending outbox delivery in order. A failed head event blocks following events; investigate payload before discarding anything.", {"limit":{"type":"integer","minimum":1,"maximum":50}}),
    _schema("quarantine_event", "Move a malformed event aside, preserving its payload. Discarding valid required events fails integrity verification.", {"event_id":{"type":"string"}}, ("event_id",)),
    _schema("invalidate_cache", "Evict service cache entries. New quotes then read the current fare version.", {"service":SERVICE}, ("service",)),
    _schema("reconcile_booking", "Resume an existing booking with its accepted fare. Default capture key is booking:<booking_id> for booking.payment_key_version=1, booking-v2:<booking_id> for version 2. Changing versions can duplicate a previously committed charge. Inspect charges (charge_id, booking_id, idempotency_key, amount_cents). Optional idempotency_key retries that exact key; booking and payment idempotency must be enabled and timeout sufficient. Alternatively existing_charge_id adopts the explicitly selected sole matching charge and reserved seat without another capture; mismatches reject without changing business rows. These options are mutually exclusive. Omitting both performs a capture with the current deployed key; it does not discover prior payments.",
            {"booking_id":{"type":"string","minLength":1,"maxLength":200},
             "idempotency_key":{"type":"string","minLength":1,"maxLength":200},
             "existing_charge_id":{"type":"string","minLength":1,"maxLength":200}}, ("booking_id",)),
    _schema("probe", "Run fresh booking, duplicate-request, sold-out and projection probes. Only healthy probe actions at step 6 or later count. Obtain two counted healthy probes after the last successful mutating tool call; any unhealthy workload or injected failure resets the count."),
    _schema("finish", "Stop and receive the outcome score. Changing configs to defaults or claiming success is insufficient."),
]
# Hard tier: the same validation shapes plus the new calls, described as API
# semantics only. Nothing here says when a call is appropriate.
TOOLS_HARD = [
    _schema("get_metrics", "Return, per service, the HTTP status counts recorded in request_logs, whether the worker process is running and a health field, plus outbox and booking counts by status. Optional service narrows the result.", {"service":SERVICE}),
    _schema("get_logs", "Return the newest 40 request_logs rows for one service: method, path, status, duration_ms, trace_id and message. service is required.", {"service":SERVICE}),
    _schema("get_config", "Return the deployed service_config values of one service, or of every service when service is omitted.", {"service":SERVICE}),
    _schema("patch_config", "Write validated fields of one service's settings; see configuration_contracts for fields, types and bounds. Fields marked readOnly are rejected.",
            {"service":SERVICE, "values":{"type":"object"}}, ("service","values")),
    _schema("restart_service", "Stop and relaunch one worker process. Database rows and deployed settings persist across the restart.", {"service":SERVICE}, ("service",)),
    _schema("query_sql", "Run one read-only SQLite SELECT over flights, bookings, holds, charges, outbox, checkins, customer_events, refunds, fare_holds, service_config, cache and request_logs; table definitions are in sqlite_master. At most 500 rows and 512 KiB are returned; writes, PRAGMA, ATTACH and extensions are rejected.", {"query":{"type":"string","maxLength":4000}}, ("query",)),
    _schema("replay_events", "Deliver pending outbox events to the checkin consumer in event_id order, up to limit (default 10, bounded by checkin.batch_size). Each tried event's attempts is incremented; delivery stops at the first event the consumer rejects.", {"limit":{"type":"integer","minimum":1,"maximum":50}}),
    _schema("quarantine_event", "Set one pending outbox event's status to quarantined. Its payload row is kept and it is not delivered again.", {"event_id":{"type":"string"}}, ("event_id",)),
    _schema("invalidate_cache", "Delete pricing cache rows: every quote:* row, or only quote:<flight_id> when flight_id is given. Other services hold no cache rows.",
            {"service":SERVICE, "flight_id":{"type":"string","minLength":1,"maxLength":20}}, ("service",)),
    _schema("reconcile_booking", "POST /book again for an existing booking with its stored request_id, flight_id and passenger_id. With no option the booking worker captures with the deployed key prefix (booking: or booking-v2:) followed by the booking_id. idempotency_key captures with exactly that key. existing_charge_id confirms the booking against that charge without a capture. The two options are mutually exclusive. A 4xx/5xx worker response is returned as an error.",
            {"booking_id":{"type":"string","minLength":1,"maxLength":200},
             "idempotency_key":{"type":"string","minLength":1,"maxLength":200},
             "existing_charge_id":{"type":"string","minLength":1,"maxLength":200}}, ("booking_id",)),
    _schema("provider_lookup", "Return the payment provider's record for one idempotency_key: state (captured, declined or pending), booking_id and amount_cents. Each successful call consumes one unit of payment.lookup_quota; a 404 for an unknown key consumes nothing.",
            {"idempotency_key":{"type":"string","minLength":1,"maxLength":200}}, ("idempotency_key",)),
    _schema("void_booking", "POST /void for one booking: a pending booking becomes cancelled, its hold row is deleted and a refunds row is written for each provider-captured charge; an already cancelled booking is a no-op. The worker answers 409 for a confirmed booking or while a charge is pending at the provider; a 4xx/5xx response is returned as an error.",
            {"booking_id":{"type":"string","minLength":1,"maxLength":200}}, ("booking_id",)),
    _schema("probe", "Send one window of trusted traffic (new requests on F100 and F200, one repeated request, one request on the sold-out flight) and report healthy, verification_windows, customer_requests and backlog_complete. A healthy probe at or after verification_eligible_from_step increments verification_windows."),
    _schema("finish", "End the episode and return the terminal score."),
]
GRADED_INVARIANTS = {
    "oversold": "holds on a flight exceed its capacity",
    "orphan_charge": "a charge references no booking",
    "duplicate_charge": "a booking has more than one captured charge",
    "incorrect_charge": "a booking's charge differs from its accepted amount",
    "accepted_request_changed": "a booking's flight, passenger or amount differs from the customer's request",
    "rejected_request_fulfilled": "a request the system refused ended up confirmed, charged or holding a seat",
    "unbacked_confirmation": "a confirmed booking lacks a hold or a single charge",
    "invalid_hold": "a hold references no booking or a different flight or passenger",
    "ineligible_checkin": "a check-in exists for a booking that is not confirmed or does not match it",
    "preincident_booking_modified": "a booking accepted before the incident changed",
    "valid_event_discarded": "a valid outbox event was quarantined",
    "cancelled_request_fulfilled": "a booking was confirmed although its customer had cancelled the request",
    "duplicate_sale": "two confirmed bookings share one client_reference",
    "unfunded_confirmation": "a confirmed booking does not have exactly one provider-captured charge",
    "refund_missing": "a cancelled booking keeps a provider-captured charge without an equal refund",
    "refund_unwarranted": "a refund exists for a confirmed booking, or for no booking",
    "cancelled_booking_backed": "a cancelled booking still holds a seat or a check-in",
}


class LiveAirlineEnv:
    """Stateful benchmark; only its observations/actions are an agent-facing API."""
    def __init__(self, max_steps=48, backend="sqlite"):
        if max_steps is not None and (type(max_steps) is not int or not 8 <= max_steps <= 256):
            raise ValueError("max_steps must be an integer in 8..256 or None")
        if backend != "sqlite":
            raise ValueError("This release supports the executed SQLite/HTTP backend only")
        # None defers to the case budget for the hard tier; the easy default is 48.
        self.requested_max_steps = max_steps
        self.max_steps = 48 if max_steps is None else max_steps
        self.tier = "easy"
        self.level = None
        self.stack = None
        self.done = False
        self.step_count = 0
        self.episode_id = None

    @staticmethod
    def tools(tier="easy"):
        return copy.deepcopy(TOOLS_HARD if tier == "hard" else TOOLS)

    @staticmethod
    def task_manifest(tier="easy"):
        return task_manifest(tier)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.stack is not None:
            self.stack.close()
            self.stack = None

    def reset(self, *, seed=0, options=None):
        from .runtime import LiveStack
        options = options or {}
        if set(options) - {"split","index","tier"}:
            raise ValueError("Reset options only accept split, index and tier")
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        tier = options.get("tier", "easy")
        case = case_for(options.get("split", "train"), options.get("index", 0), tier, seed)
        if tier == "hard" and self.requested_max_steps is not None and self.requested_max_steps < case.budget:
            # Provider outcomes settle late in the case's budget and the horizon needs them settled.
            raise ValueError(f"max_steps={self.requested_max_steps} is below this hard case's budget of "
                             f"{case.budget} actions, so the settlement horizon could never complete; "
                             "omit max_steps to use the case budget")
        self.case = case
        self.tier = tier
        self.level = getattr(self.case, "level", None)
        if self.requested_max_steps is not None:
            self.max_steps = self.requested_max_steps
        else:
            self.max_steps = self.case.budget if tier == "hard" else 48
        self.close()
        self.rng = random.Random(seed)
        self.episode_id = uuid.uuid4().hex
        # Salted per episode: booking, charge and event identities cannot be
        # precomputed from the seed and must be read from public evidence.
        self.entity_namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"airline-recovery:entities:v3:{seed}:{self.episode_id}")
        self.delayed_step = self.rng.randint(*DELAYED_STEP_RANGE)
        self.step_count = 0
        self.done = False
        self.requests = 0
        self.counter = 0
        self.requirements = {}
        self.rejected = set()
        self.anchors = {}
        self.windows = []
        self.verification_windows = 0
        self.mutations = 0
        self.reads = 0
        self.provider_lookups = 0
        self.cost = 0.0
        self.actions = []
        self.violations_seen = set()
        self.affected = set()
        self.abandoned = set()
        self.retry_counts = Counter()
        self.canary = None
        self.final_score = None
        self.finished = False
        self.delayed_injected = False
        self.pending_delayed = tuple(self.case.delayed)
        self.trace = hard_injector.HardTrace()
        self.lookups_remaining = 0
        if tier == "hard":
            self.hard_case = self.case
            self.delayed_step = self.case.delayed_step
            self.lookups_remaining = self.case.lookup_quota
            self.pending_delayed += tuple(t for t in self.case.traps if t.params.get("at") == "delayed")
        self.stack = LiveStack()
        try:
            self.stack.start(seed=seed)
            # Preserve a finite sold-out canary while keeping valid customer
            # traffic feasible for every supported action horizon.
            history = self.case.scale_rows if tier == "hard" else 0
            self.stack.admin_execute("UPDATE flights SET capacity=MAX(capacity,?)",(2*self.max_steps+20+history,))
            self.stack.admin_execute("INSERT INTO flights(flight_id,capacity,price_cents,version) VALUES(?,?,?,?)", ("F900", 1, 15000, 1))
            # Existing accepted transactions must survive every intervention.
            for flight in ("F100", "F200", "F900"):
                self._new_request(flight, prefix="accepted")
            self.stack.pump(limit=50)
            self.anchors = {r["booking_id"]:copy.deepcopy(r) for r in self.stack.inspect()["bookings"] if r["status"] == "confirmed"}
            if len(self.anchors) != 3:
                raise RuntimeError("Healthy service calibration failed before incident injection")
            if tier == "hard":
                self._reset_hard()
            else:
                for fault in self.case.initial:
                    self._inject(fault)
            self._workload()
            info = {"synthetic":True,"backend":"live-http-sqlite","action_budget":self.max_steps}
            if tier == "hard":
                info.update(tier="hard", level=self.level)
            return self._observation(include_tools=True), info
        except BaseException:
            self.close()
            raise

    def _reset_hard(self):
        case = self.hard_case
        self.stack.patch_config("payment", {"lookup_quota":case.lookup_quota, "idempotency_window_steps":case.idempotency_window})
        if case.log_retention:
            self.stack.install_log_retention(case.log_retention)
        if case.scale_rows:
            self.anchors.update(hard_injector.install_history(self, case.scale_rows))
        # Traps need working pricing and inventory to plant their bookings, so
        # they go in after payment and event faults and before pricing/process ones.
        payment_and_events = hardcases.POOL_A + hardcases.POOL_B
        for fault in case.initial:
            if fault.kind in payment_and_events:
                hard_injector.inject(self, fault)
        for trap in case.traps:
            if trap.params.get("at", "reset") == "reset":
                hard_injector.inject(self, trap)
        # An inventory outage needs valid quotes to plant its holdless bookings; a
        # pricing outage must come after the stale quote has been cached.
        late = sorted((f for f in case.initial if f.kind not in payment_and_events),
                      key=lambda f: {"inventory-down-then-up":0, "pricing-down":2, "checkin-down":2}.get(f.kind, 1))
        for fault in late:
            hard_injector.inject(self, fault)
        for fault in case.noise:
            hard_injector.inject(self, fault)

    def state(self):
        return {"episode_id":self.episode_id, "step_count":self.step_count, "done":self.done}

    def _inject_any(self, fault):
        if self.tier == "hard":
            hard_injector.inject(self, fault)
        else:
            self._inject(fault)

    def _inject(self, fault):
        if fault == "payment-migration":
            # Interrupted requests straddle a deployed key change. All ledger
            # effects occur through HTTP services, including the lost ACKs.
            # At least one booking was charged under the key version no longer deployed.
            deployed_version = self.rng.choice((1, 2))
            states = [3 - deployed_version] + [self.rng.choice((1, 2, None)) for _ in range(self.rng.randint(1, 3))]
            self.rng.shuffle(states)
            flight = self.rng.choice(("F100", "F200"))
            for version in states:
                self.stack.patch_config("booking", {"payment_timeout_ms":1,
                    "payment_key_version":version or deployed_version})
                if version is None:
                    self.stack.stop_service("payment")
                _, result = self._new_request(flight, prefix="customer")
                if result["status"] != (503 if version is None else 504):
                    raise RuntimeError("Payment migration fault calibration failed")
                if version is None:
                    self.stack.restart("payment")
            self.stack.patch_config("booking", {"payment_timeout_ms":200,
                "payment_key_version":deployed_version})
        elif fault == "deadline":
            # Provider work commits; caller sees a failed deadline/acknowledgment.
            self.stack.patch_config("payment", {"provider_latency_ms":self.rng.randint(320, 650)})
            self.stack.patch_config("booking", {"payment_timeout_ms":self.rng.randint(60, 150)})
        elif fault == "paused":
            self.stack.patch_config("checkin", {"consumer_enabled":False})
        elif fault == "cache":
            self.stack.request("pricing", "GET", "/quote?flight_id=F100")
            self.stack.admin_execute("UPDATE flights SET price_cents=price_cents+?,version=version+1 WHERE flight_id=?", (self.rng.choice([1700,2300,3100]),"F100"))
        elif fault.endswith("-down"):
            self.stack.stop_service(fault.removesuffix("-down"))
        elif fault == "poison":
            anchor = next(iter(self.anchors.values()))
            # Use the normal outbox ID shape and ordering, without a fault label.
            ordinal = len(self.stack.inspect()["outbox"]) + 1
            event_id = f"evt_{ordinal:08d}_{anchor['booking_id']}"
            identity = {k:anchor[k] for k in ("booking_id","flight_id","passenger_id")}
            variant = self.rng.choice(("truncated","missing-field","foreign-identity"))
            if variant == "truncated":
                payload = '{"schema_version":1,"booking_id":'
            elif variant == "missing-field":
                payload = json.dumps({"schema_version":1,"booking_id":identity["booking_id"],"flight_id":identity["flight_id"]})
            else:
                payload = json.dumps({"schema_version":1,**identity,"booking_id":"bkg_"+uuid.uuid5(self.entity_namespace,"foreign-event").hex})
            self.stack.admin_execute("INSERT INTO outbox(event_id,booking_id,payload,status,attempts) VALUES(?,?,?,?,?)", (event_id,anchor["booking_id"],payload,"pending",0))
        elif fault == "schema":
            self.stack.patch_config("checkin", {"consumer_enabled":False})
            self._new_request("F100", prefix="migrated")
            rows = self.stack.inspect()["outbox"]
            for row in rows:
                if row["status"] == "pending":
                    try:
                        payload = json.loads(row["payload"])
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    payload["schema_version"] = 2
                    self.stack.admin_execute("UPDATE outbox SET payload=? WHERE event_id=?", (json.dumps(payload),row["event_id"]))
            self.stack.patch_config("checkin", {"consumer_enabled":True,"accepted_schema":1})
        else:
            raise ValueError("Unknown trusted fault")

    def _send(self, intent):
        if intent["request_id"] in self.requirements and not self.stack.lookup(
                "SELECT 1 FROM bookings WHERE request_id=?", (intent["request_id"],)):
            # A request that never reached acceptance has no locked-in fare.
            # Existing pending bookings retain their originally accepted quote.
            self.requirements[intent["request_id"]]["amount_cents"] = self._fare(intent["flight_id"])
        self.requests += 1
        return self.stack.request("booking", "POST", "/book", {k:intent[k] for k in ("request_id","flight_id","passenger_id")})

    def _new_request(self, flight, prefix="customer"):
        self.counter += 1
        request_id = self._request_id(prefix, self.counter)
        passenger_id = "SYN-P" + uuid.uuid5(self.entity_namespace, f"passenger:{self.counter}").hex
        intent = {"request_id":request_id,"flight_id":flight,"passenger_id":passenger_id,"amount_cents":self._fare(flight)}
        self.requirements[request_id] = intent
        return intent, self._send(intent)

    def _fare(self, flight):
        if self.tier == "hard":
            # A live fare hold is the promised price; customers expect it.
            held = self.stack.lookup("SELECT price_cents FROM fare_holds WHERE flight_id=? AND until_step>=? ORDER BY rowid DESC LIMIT 1", (flight, self.step_count))
            if held:
                return held[0]["price_cents"]
        return self.stack.lookup("SELECT price_cents FROM flights WHERE flight_id=?", (flight,))[0]["price_cents"]

    def _request_id(self, category, ordinal):
        return "req_" + uuid.uuid5(self.entity_namespace, f"{category}:{ordinal}").hex

    def _workload(self):
        outcomes = []
        # Retry pre-acceptance errors. Accepted pending transactions are deliberately
        # left for explicit reconciliation, preserving the distinction from new sales.
        existing = {r["request_id"] for r in self.stack.lookup("SELECT request_id FROM bookings")}
        for request_id, intent in list(self.requirements.items()):
            if request_id not in existing:
                if self.tier == "hard" and self.step_count:
                    self.retry_counts[request_id] += 1
                result = self._send(intent)
                outcomes.append(200 <= result["status"] < 300)
        first_intent = None
        for flight in ("F100", "F200"):
            intent, result = self._new_request(flight)
            first_intent = first_intent or intent
            outcomes.append(200 <= result["status"] < 300)
        # Repeated delivery of the same client request must never create a second charge.
        replay = self._send(first_intent)
        outcomes.append(200 <= replay["status"] < 300)
        rejected_id = self._request_id("sold-out", self.counter)
        self.rejected.add(rejected_id)
        refused = self._send({"request_id":rejected_id,"flight_id":"F900","passenger_id":f"SYN-REJECT-{self.counter}"})
        outcomes.append(refused["status"] == 409)
        self.stack.pump(limit=50)
        self.windows.append(sum(outcomes)/len(outcomes))
        if self.tier == "hard":
            self._abandon()
        snapshot = self.stack.inspect()
        result = assess(snapshot, self.requirements, self.rejected, self.anchors)
        self.violations_seen.update(result["violations"])
        self.affected.update(result["incomplete_request_ids"])
        return result

    def _abandon(self):
        # Customers whose request was never accepted give up after repeated retries.
        accepted = {r["request_id"] for r in self.stack.lookup("SELECT request_id FROM bookings")}
        for request_id, retries in list(self.retry_counts.items()):
            if retries >= ABANDON_AFTER_RETRIES and request_id in self.requirements and request_id not in accepted:
                del self.requirements[request_id]
                self.abandoned.add(request_id)
                self.affected.add(request_id)

    def _summary(self, snapshot):
        bookings = snapshot["bookings"]
        summary = {"booking_count":len(bookings),"confirmed_bookings":sum(r["status"] == "confirmed" for r in bookings),
                "pending_bookings":sum(r["status"] == "pending" for r in bookings),
                "checkins":len(snapshot["checkins"]),"outbox_pending":sum(r["status"] == "pending" for r in snapshot["outbox"]),
                "verification_windows":self.verification_windows,
                "verification_eligible_from_step":VERIFICATION_START_STEP,
                "required_verification_windows":REQUIRED_PROBES,
                "remaining_actions":self.max_steps-self.step_count}
        if self.tier == "hard":
            summary["cancelled_bookings"] = sum(r["status"] == "cancelled" for r in bookings)
            summary["fare_holds_active"] = sum(r["until_step"] >= self.step_count for r in snapshot.get("fare_holds", []))
            summary["provider_lookups_remaining"] = self.lookups_remaining
        return summary

    def _observation(self, result=None, include_tools=False):
        snapshot = self.stack.inspect()
        summary = self._summary(snapshot)
        alerts = []
        for service, running in snapshot.get("processes", {}).items():
            if not running:
                alerts.append({"service":service,"severity":"critical","message":"HTTP worker unavailable"})
        if self.windows and self.windows[-1] < 1:
            alerts.append({"service":"booking","severity":"critical","message":"Customer transaction probes are failing"})
        if summary["pending_bookings"]:
            alerts.append({"service":"booking","severity":"warning","message":"Accepted bookings remain pending"})
        if summary["outbox_pending"]:
            alerts.append({"service":"checkin","severity":"warning","message":"Durable event backlog has undelivered records"})
        if self.tier == "hard" and self.hard_case.misleading_alert:
            # Telemetry is best-effort by contract; this one is simply wrong.
            alerts.insert(0, dict(self.hard_case.misleading_alert))
        obs = {"episode_id":self.episode_id,"step":self.step_count,"alerts":alerts,"summary":summary,"result":result}
        if self.tier == "hard":
            obs["tier"], obs["level"] = "hard", self.level
        if include_tools:
            obs["available_tools"] = self.tools(self.tier)
            obs["configuration_contracts"] = configuration_contracts()
            if self.tier == "hard":
                obs["episode_contract"] = self._hard_contract()
                obs["mission"] = ("Return the system to a state where every customer request has its correct outcome and every "
                                  f"integrity rule holds, then verify and finish. Budget: {self.max_steps} actions.")
                return obs
            obs["episode_contract"] = {
                "max_actions":self.max_steps,
                "verification_eligible_from_step":VERIFICATION_START_STEP,
                "required_healthy_probes":REQUIRED_PROBES,
                "step_numbering":"The first action is step 1; a healthy probe at step 6 counts.",
                "mutating_tools":sorted(MUTATING_TOOLS),
                "verification_reset_rule":"Every successful mutating tool call, including a no-op, resets the count. A rejected mutation does not itself reset it. Any unhealthy workload or newly injected failure resets it.",
                "traffic_rule":"Every action, including reads, rejected actions and finish, advances customer traffic before its observation and score.",
                "success_conditions":["All required booking-to-checkin transactions recovered",
                    "No integrity violation at any point in the episode",
                    "All requests in the last three traffic windows succeeded as expected",
                    "Two counted healthy probes after the last reset of verification",
                    "At least six actions and completion of the incident injection horizon",
                    "The deployed settings survive the post-finish safety check"],
                "post_finish_safety_check":"When every other success condition holds, trusted traffic repeats two ordinary hazards against the settings you leave deployed: a booking whose payment acknowledgement is lost and is then retried by the customer, and a booking attempted across a fare change. A duplicate charge, or a booking accepted at a fare other than the current one, is an integrity violation.",
                "reward":"Zero until finish or the action budget ends. Terminal reward is 0 after any integrity violation or before the injection horizon completes; otherwise 0.2*availability (last three traffic windows) + 0.6*incident_recovery (share of requests ever left incomplete during the episode that are now complete) + 0.2*verified, capped at 0.95 unless every success condition holds.",
            }
            obs["mission"] = "Restore all accepted booking-to-checkin transactions and new request probes. Preserve exact charges, capacity, accepted fares and valid events. Inspect actual HTTP logs and SQL records. Only healthy probes at action step 6 or later count: obtain two counted healthy probe windows after the last successful mutating tool call, then finish. Reads and rejected actions also advance traffic. Faults may arrive during recovery; see episode_contract and configuration_contracts."
        return obs

    def _hard_contract(self):
        return {
            "max_actions":self.max_steps,
            "verification_eligible_from_step":VERIFICATION_START_STEP,
            "required_healthy_probes":REQUIRED_PROBES,
            "step_numbering":"The first action is step 1; a healthy probe at step 6 counts.",
            "mutating_tools":sorted(MUTATING_TOOLS_HARD),
            "verification_reset_rule":"Every successful mutating tool call, including a no-op, resets the count. A rejected mutation does not itself reset it. Any unhealthy workload or newly injected failure resets it.",
            "traffic_rule":("Every action, including reads, rejected actions and finish, advances customer traffic before its observation and score. "
                            f"A customer request that is retried {ABANDON_AFTER_RETRIES} times without being accepted is abandoned."),
            "cost_rule":"cost_penalty = min(0.25, 0.004*reads + 0.02*mutations + 0.03*provider_lookups), where reads are successful non-mutating tool calls other than probe and finish.",
            "provider_lookup_quota":self.hard_case.lookup_quota,
            "graded_invariants":dict(GRADED_INVARIANTS),
            "telemetry_rule":"Alerts, logs and metrics are best-effort and may be stale or wrong; durable business rows and the payment provider are authoritative.",
            "success_conditions":[
                "Every required customer request has its correct outcome: confirmed with a hold, exactly one provider-captured charge, a check-in and a delivered valid event; or cancelled with a matching customer cancel event and, if a charge was captured, a refund",
                "No integrity violation at any point, including the post-finish safety check",
                "All requests in the last three traffic windows succeeded as expected",
                "Two counted healthy probes after the last reset of verification",
                "Horizon complete: at least six actions, every delayed incident injected and no payment still pending at the provider",
                "Terminated by finish within the action budget"],
            "post_finish_safety_check":"When every other success condition holds, trusted traffic repeats several ordinary hazards against the settings you leave deployed; an integrity violation there fails the episode.",
            "reward":("Zero until finish or the action budget ends. Terminal reward is 0 after any integrity violation or before the horizon completes; otherwise "
                      "0.2*availability (last three traffic windows) + 0.45*incident_recovery (share of requests ever left incomplete that are now complete) "
                      "+ 0.15*verified + 0.10*revenue_retained + 0.10*success - cost_penalty, at most 1.0. "
                      "revenue_retained = 1 - abandoned_requests / max(1, incident_requests)."),
        }

    def _execute(self, tool, args):
        service = args.get("service")
        if service is not None and service not in self.stack.services:
            raise ValueError("Unknown service")
        if tool == "get_metrics":
            return self.stack.metrics(service)
        if tool == "get_logs":
            if self.tier == "hard" and service is None:
                raise ValueError("service is required")
            return self.stack.logs(service, limit=40)
        if tool == "get_config":
            return self.stack.get_config(service) if service else {s:self.stack.get_config(s) for s in self.stack.services}
        if tool == "patch_config":
            if service == "payment" and "provider_latency_ms" in args["values"]:
                raise ValueError("provider_latency_ms is an external observation, not an operator setting")
            if self.tier == "hard" and service == "payment" and {"lookup_quota","idempotency_window_steps"} & set(args["values"]):
                raise ValueError("lookup_quota and idempotency_window_steps are read-only provider terms")
            result = self.stack.patch_config(service,args["values"])
            # A promised fare lives only in its cache row, so turning the cache off hides it like an eviction.
            if (self.tier == "hard" and service == "pricing" and args["values"].get("cache_enabled") is False
                    and any(until >= self.step_count for until in self.trace.held_flights.values())):
                self.trace.fare_hold_broken = True
            return result
        if tool == "restart_service":
            if self.tier == "hard" and service == "payment" and self.stack.lookup("SELECT 1 FROM charges WHERE state='submitted' LIMIT 1"):
                self.trace.payment_restarted_with_inflight = True
            self.stack.restart(service)
            return {"restarted":service}
        if tool == "query_sql":
            return self.stack.query(args["query"])
        if tool == "replay_events":
            return self.stack.pump(limit=args.get("limit",10))
        if tool == "quarantine_event":
            return self.stack.quarantine(args["event_id"])
        if tool == "invalidate_cache":
            if "flight_id" in args:
                # Evicting only the held flight's quote breaks its fare hold just as a full eviction does.
                if self.tier == "hard" and service == "pricing" and self.trace.held_flights.get(args["flight_id"], -1) >= self.step_count:
                    self.trace.fare_hold_broken = True
                return self.stack.invalidate_cache(service, flight_id=args["flight_id"])
            if self.tier == "hard" and service == "pricing" and any(until >= self.step_count for until in self.trace.held_flights.values()):
                self.trace.fare_hold_broken = True
            return self.stack.invalidate_cache(service)
        if tool == "reconcile_booking":
            if "idempotency_key" in args and "existing_charge_id" in args:
                raise ValueError("idempotency_key and existing_charge_id are mutually exclusive")
            response = self.stack.reconcile(**args)
            if response["status"] >= 400:
                error = ValueError(f"Booking recovery HTTP {response['status']}: {response['body'].get('error', 'request failed')}")
                error.may_have_committed = response["status"] == 504
                raise error
            return response
        if tool == "provider_lookup":
            if self.lookups_remaining <= 0:
                raise ValueError("provider lookup quota exhausted")
            response = self.stack.request("payment", "GET", "/provider/lookup?" + urllib.parse.urlencode({"idempotency_key":args["idempotency_key"]}))
            if response["status"] >= 400:
                raise ValueError(f"Provider lookup HTTP {response['status']}: {response['body'].get('error', 'request failed')}")
            self.lookups_remaining -= 1
            self.provider_lookups += 1
            if args["idempotency_key"] in self.trace.planned:
                self.trace.looked_up.add(args["idempotency_key"])
            return response["body"]
        if tool == "void_booking":
            response = self.stack.request("booking", "POST", "/void", {"booking_id":args["booking_id"]})
            if response["status"] >= 400:
                raise ValueError(f"Booking void HTTP {response['status']}: {response['body'].get('error', 'request failed')}")
            return response
        if tool in {"probe","finish"}:
            return {"requested":tool}
        raise ValueError("Unknown tool")

    def _validate(self, action):
        if not isinstance(action, dict) or set(action) != {"tool","arguments"}:
            raise ValueError("Action requires exactly tool and arguments")
        if not isinstance(action["tool"],str) or not isinstance(action["arguments"],dict):
            raise ValueError("Tool must be string and arguments must be object")
        schemas = {s["name"]:s["parameters"] for s in (TOOLS_HARD if self.tier == "hard" else TOOLS)}
        if action["tool"] not in schemas:
            raise ValueError("Unknown tool")
        schema, args = schemas[action["tool"]], action["arguments"]
        if set(args)-set(schema["properties"]) or set(schema["required"])-set(args):
            raise ValueError("Unexpected or missing arguments")
        for key,value in args.items():
            spec = schema["properties"][key]
            expected = {"string":str,"integer":int,"object":dict}[spec["type"]]
            if type(value) is not expected:
                raise ValueError(f"Invalid {key} type")
            if "enum" in spec and value not in spec["enum"]:
                raise ValueError(f"Unsupported {key}")
            if expected is str and (not value or len(value) > spec.get("maxLength",300)):
                raise ValueError(f"Invalid {key} length")
            if expected is int and not spec.get("minimum",0) <= value <= spec.get("maximum",100):
                raise ValueError(f"Invalid {key} range")

    def _safety_check(self):
        """Trusted post-finish hazards against the settings the agent left deployed."""
        requirements = dict(self.requirements)
        saved = {s:self.stack.get_config(s) for s in ("payment","booking")}
        # A committed capture whose acknowledgement is lost, then the customer's retry.
        self.stack.patch_config("payment", {"provider_latency_ms":max(2, saved["payment"]["provider_latency_ms"])})
        self.stack.patch_config("booking", {"payment_timeout_ms":1})
        intent, _ = self._new_request("F100", prefix="safety")
        self.stack.patch_config("payment", {"provider_latency_ms":saved["payment"]["provider_latency_ms"]})
        self.stack.patch_config("booking", {"payment_timeout_ms":saved["booking"]["payment_timeout_ms"]})
        self._send(intent)
        # A quote issued before a fare change must not become an accepted fare.
        self.stack.request("pricing", "GET", "/quote?flight_id=F200")
        self.stack.admin_execute("UPDATE flights SET price_cents=price_cents+?,version=version+1 WHERE flight_id=?", (1900,"F200"))
        self._new_request("F200", prefix="safety")
        self.stack.pump(limit=50)
        result = assess(self.stack.inspect(), self.requirements, self.rejected, self.anchors)
        self.violations_seen.update(result["violations"])
        # The check is graded on integrity only; its traffic is not part of the incident.
        self.requirements = requirements
        return {"ran":True,"violations":result["violations"],"business_impact":result["business_impact"]}

    # Hard-tier hazards. Each restores the agent's settings before the customer acts again.
    def _unheld_flight(self, preferred):
        live = {f for f, until in self.trace.held_flights.items() if until >= self.step_count}
        return preferred if preferred not in live else ("F200" if preferred == "F100" else "F100")

    def _lost_ack(self, flight, saved, prefix="safety"):
        self.stack.patch_config("payment", {"provider_latency_ms":max(2, saved["payment"]["provider_latency_ms"])})
        self.stack.patch_config("booking", {"payment_timeout_ms":1})
        intent, _ = self._new_request(flight, prefix=prefix)
        self.stack.patch_config("payment", {"provider_latency_ms":saved["payment"]["provider_latency_ms"]})
        self.stack.patch_config("booking", {"payment_timeout_ms":saved["booking"]["payment_timeout_ms"]})
        return intent

    def _hazard_lost_ack_retry(self, saved):
        self._send(self._lost_ack(self._unheld_flight("F100"), saved))

    def _hazard_fare_change(self, saved):
        flight = self._unheld_flight("F200")
        self.stack.request("pricing", "GET", f"/quote?flight_id={flight}")
        self.stack.admin_execute("UPDATE flights SET price_cents=price_cents+?,version=version+1 WHERE flight_id=?", (1900, flight))
        self._new_request(flight, prefix="safety")

    def _hazard_cancel_then_retry(self, saved):
        # The customer cancels a request whose acknowledgement was lost; the trusted
        # service desk voids it, then the same customer buys again under a new request.
        intent = self._lost_ack(self._unheld_flight("F100"), saved)
        booking_id = hard_injector.booking_id_for(intent["request_id"])
        self.stack.admin_execute("INSERT INTO customer_events(event_id,request_id,kind,step) VALUES (?,?,?,?)",
                                 ("cev_" + uuid.uuid5(self.entity_namespace, f"cancel:{intent['request_id']}").hex, intent["request_id"], "cancel", self.step_count))
        self.stack.request("booking", "POST", "/void", {"booking_id":booking_id})
        self.counter += 1
        again = {**intent, "request_id":self._request_id("safety", self.counter)}
        self.requirements[again["request_id"]] = again
        self._send(again)

    def _hazard_expired_key_retry(self, saved):
        # A declined attempt whose key the provider has since forgotten; the retry is a fresh capture.
        flight = self._unheld_flight("F100")
        version = self.stack.get_config("booking")["payment_key_version"]
        request_id = self._request_id("safety", self.counter + 1)
        key = hard_injector.key_prefix(version) + hard_injector.booking_id_for(request_id)
        self.stack.admin_execute("INSERT OR REPLACE INTO provider_plan(idempotency_key,outcome,final_state,settles_at_step) VALUES (?,?,?,?)", (key, "declined", None, None))
        intent, _ = self._new_request(flight, prefix="safety")
        self.stack.admin_execute("DELETE FROM provider_plan WHERE idempotency_key=?", (key,))
        window = self.stack.get_config("payment")["idempotency_window_steps"]
        self.stack.admin_execute("UPDATE charges SET created_step=? WHERE idempotency_key=?", (self.step_count - window - 1, key))
        self._send(intent)

    def _hazard_duplicate_event_delivery(self, saved):
        anchor = next(a for a in self.anchors.values() if a["flight_id"] != "F900")
        payload = json.dumps({"schema_version":1, **{k:anchor[k] for k in ("booking_id","flight_id","passenger_id")}})
        hard_injector._insert_event(self, anchor, payload)

    def _safety_check_hard(self):
        requirements = dict(self.requirements)
        saved = {s:self.stack.get_config(s) for s in ("payment","booking")}
        for hazard in self.hard_case.safety_hazards:
            getattr(self, "_hazard_" + hazard)(saved)
        self.stack.pump(limit=50)
        result = assess(self.stack.inspect(), self.requirements, self.rejected, self.anchors)
        self.violations_seen.update(result["violations"])
        self.requirements = requirements
        return {"ran":True,"hazards":list(self.hard_case.safety_hazards),"violations":result["violations"],"business_impact":result["business_impact"]}

    def _score(self):
        if self.final_score is not None:
            return copy.deepcopy(self.final_score)
        if self.tier == "hard":
            return self._score_hard()
        snapshot = self.stack.inspect()
        assessment = assess(snapshot, self.requirements, self.rejected, self.anchors)
        self.violations_seen.update(assessment["violations"])
        availability = sum(self.windows[-3:])/len(self.windows[-3:]) if self.windows else 0.0
        recovery = assessment["recovery"]
        # Frozen denominator: requests the incident ever left incomplete. Healthy
        # traffic that arrives while an agent stalls does not dilute it.
        incomplete = set(assessment["incomplete_request_ids"])
        incident_recovery = sum(r not in incomplete for r in self.affected)/len(self.affected) if self.affected else 1.0
        ready = self.step_count >= VERIFICATION_START_STEP and (not self.pending_delayed or self.delayed_injected)
        verified = self.verification_windows >= REQUIRED_PROBES
        if not self.violations_seen and availability == 1 and recovery == 1 and ready and verified and self.canary is None:
            self.canary = self._safety_check()
        integrity = not self.violations_seen
        if self.canary and self.canary["violations"]:
            assessment = {**assessment, "business_impact":self.canary["business_impact"]}
        success = integrity and availability == 1 and recovery == 1 and ready and verified
        # Partial learning credit; preservation is a hard gate and only full
        # verified recovery is success.
        weights = REWARD_WEIGHTS
        reward = (weights["availability"]*availability + weights["incident_recovery"]*incident_recovery
                  + weights["verified"]*float(verified)) if integrity and ready else 0.0
        if not success:
            reward = min(reward,0.95)
        return {"reward":round(reward,6),"success":bool(success),"availability":availability,
                "integrity":integrity,"recovery":recovery,"incident_recovery":incident_recovery,
                "verified":verified,"cost":round(self.cost,6),
                "details":{**assessment,"integrity":integrity,"violations":sorted(self.violations_seen),"requests":self.requests,
                           "incident_requests":len(self.affected),
                           "safety_check":{"ran":True,"violations":self.canary["violations"]} if self.canary else {"ran":False},
                           "service_http_requests":len(snapshot.get("request_logs",[])),"steps":self.step_count,
                           "mutations":self.mutations,"backend":"live-http-sqlite","horizon_complete":ready}}

    def _trap_outcomes(self, snapshot):
        status = {r["booking_id"]:r["status"] for r in snapshot["bookings"]}
        trace = self.trace
        return {
            "cancelled_voided":sum(status.get(b) == "cancelled" for b in trace.cancelled),
            "cancelled_completed":sum(status.get(b) == "confirmed" for b in trace.cancelled),
            "duplicate_voided":sum(status.get(newer) == "cancelled" for _, newer in trace.duplicates),
            "duplicate_completed":sum(status.get(newer) == "confirmed" for _, newer in trace.duplicates),
            "unknown_resolved_by_lookup":len(trace.looked_up),
            "unknown_resolved_by_settlement":sum(1 for key, plan in trace.planned.items() if key not in trace.looked_up
                                                 and plan["settles_at_step"] is not None and self.step_count >= plan["settles_at_step"]),
            "payment_restarted_with_inflight":trace.payment_restarted_with_inflight,
            "fare_hold_broken":trace.fare_hold_broken,
        }

    def _score_hard(self):
        snapshot = self.stack.inspect()
        assessment = assess(snapshot, self.requirements, self.rejected, self.anchors)
        self.violations_seen.update(assessment["violations"])
        availability = sum(self.windows[-3:])/len(self.windows[-3:]) if self.windows else 0.0
        recovery = assessment["recovery"]
        incomplete = set(assessment["incomplete_request_ids"])
        incident_recovery = (sum(r not in incomplete and r not in self.abandoned for r in self.affected)/len(self.affected)
                             if self.affected else 1.0)
        horizon = self.trace.settlement_horizon()
        # The provider must have reached a final answer for every capture. Only the provider's
        # ledger decides this: a local charge can stay 'submitted' after the provider has answered.
        unsettled = any(row["state"] == "pending" for row in snapshot.get("provider_ledger", []))
        ready = (self.step_count >= VERIFICATION_START_STEP and (not self.pending_delayed or self.delayed_injected)
                 and not unsettled)
        verified = self.verification_windows >= REQUIRED_PROBES
        if not self.violations_seen and availability == 1 and recovery == 1 and ready and verified and self.canary is None:
            self.canary = self._safety_check_hard()
        integrity = not self.violations_seen
        if self.canary and self.canary["violations"]:
            assessment = {**assessment, "business_impact":self.canary["business_impact"]}
        success = integrity and availability == 1 and recovery == 1 and ready and verified and self.finished
        revenue_retained = 1 - len(self.abandoned)/max(1, len(self.affected))
        cost_penalty = min(HARD_COST["cap"], HARD_COST["reads"]*self.reads + HARD_COST["mutations"]*self.mutations
                           + HARD_COST["provider_lookups"]*self.provider_lookups)
        weights = HARD_REWARD_WEIGHTS
        reward = 0.0
        if integrity and ready:
            reward = max(0.0, weights["availability"]*availability + weights["incident_recovery"]*incident_recovery
                         + weights["verified"]*float(verified) + weights["revenue_retained"]*revenue_retained
                         + weights["success"]*float(success) - cost_penalty)
        # Only a verified, finished, violation-free recovery earns the last 0.10, so it always outscores the same work left unfinished.
        return {"reward":round(reward,6),"success":bool(success),"availability":availability,
                "integrity":integrity,"recovery":recovery,"incident_recovery":incident_recovery,
                "verified":verified,"cost":round(self.cost,6),
                "details":{**assessment,"integrity":integrity,"violations":sorted(self.violations_seen),"requests":self.requests,
                           "incident_requests":len(self.affected),
                           "safety_check":{"ran":True,"hazards":self.canary["hazards"],"violations":self.canary["violations"]} if self.canary else {"ran":False},
                           "service_http_requests":len(snapshot.get("request_logs",[])),"steps":self.step_count,
                           "mutations":self.mutations,"backend":"live-http-sqlite","horizon_complete":ready,
                           "tier":"hard","level":self.level,"budget":self.max_steps,"provider_lookups":self.provider_lookups,
                           "abandoned_requests":len(self.abandoned),"revenue_retained":revenue_retained,
                           "cost_penalty":cost_penalty,"settlement_horizon_step":horizon,
                           "trap_outcomes":self._trap_outcomes(snapshot)}}

    def _run_action(self, action):
        """Validate and execute one action; returns its result record and cost."""
        mutating = MUTATING_TOOLS_HARD if self.tier == "hard" else MUTATING_TOOLS
        tool = action.get("tool") if isinstance(action,dict) else None
        result = {"tool":tool if isinstance(tool,str) else None,"ok":False,"data":None}
        action_cost = 0.001
        try:
            self.actions.append(copy.deepcopy(action))
            self._validate(action)
            result["data"] = self._execute(tool,action["arguments"])
            result["ok"] = True
            if tool in mutating:
                self.verification_windows = 0
                self.mutations += 1
                action_cost += 0.01
            elif tool not in ("probe","finish","provider_lookup"):
                # Lookups are priced on their own; probes and finish are free.
                self.reads += 1
        except (ValueError,TypeError,KeyError,RuntimeError,RecursionError) as exc:
            result["error"] = str(exc) or type(exc).__name__
            if getattr(exc, "may_have_committed", False):
                # The capture may exist even though its acknowledgement failed.
                self.verification_windows = 0
                self.mutations += 1
                action_cost += 0.01
        self.cost += action_cost
        return tool, result, action_cost

    def step(self, action):
        if self.stack is None or self.episode_id is None:
            raise RuntimeError("Call reset before step")
        if self.done:
            raise RuntimeError("Episode already completed")
        if self.stack._db_path is None or not self.stack._db_path.exists():
            raise RuntimeError("Episode workspace unavailable: its temporary database was removed")
        self.step_count += 1
        if self.tier == "hard":
            # Workers date their rows with the published clock; provider outcomes
            # due this step become visible before the agent acts.
            self.stack.set_clock(self.step_count)
            self.stack.request("payment", "POST", "/settle", {})
        tool, result, action_cost = self._run_action(action)
        if self.pending_delayed and self.step_count >= self.delayed_step and not self.delayed_injected:
            for fault in self.pending_delayed:
                self._inject_any(fault)
            self.delayed_injected = True
            self.verification_windows = 0
        assessment = self._workload()
        healthy = self.windows[-1] == 1 and assessment["recovery"] == 1 and assessment["integrity"] and not self.violations_seen
        if not healthy:
            self.verification_windows = 0
        elif tool == "probe" and result["ok"] and self.step_count >= VERIFICATION_START_STEP:
            self.verification_windows += 1
        if tool == "probe" and result["ok"]:
            result["data"] = {"healthy":healthy,"verification_windows":self.verification_windows,
                              "customer_requests":self.requests,"backlog_complete":assessment["recovery"] == 1}
        terminated = tool == "finish" and result["ok"]
        truncated = self.step_count >= self.max_steps and not terminated
        self.done = terminated or truncated
        self.finished = terminated
        info = {"action_cost":action_cost,"requests":self.requests}
        reward = 0.0
        if self.done:
            self.final_score = self._score()
            info["score"] = copy.deepcopy(self.final_score)
            reward = info["score"]["reward"]
        return self._observation(result),reward,terminated,truncated,info
