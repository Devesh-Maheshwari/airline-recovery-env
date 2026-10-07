"""Failure causes for the recorded hard-tier coding-agent episodes (v0.5.0).

    .venv/bin/python scripts/failure_taxonomy.py           # rewrite the outputs
    .venv/bin/python scripts/failure_taxonomy.py --check   # exit 1 if they are stale

Reads evidence/v0.5.0/hard/agents/<agent>/episodes.jsonl, each agent's action
list (actions/hard-<split>-<idx>-seed<seed>.json), the case every slot and seed
generates (airline_recovery.live.hardcases.generate_case) and the manual review
notes in evidence/v0.5.0/hard/failure-analysis/manual_checks.json. Writes
labels.jsonl, summary.json and README.md in that folder. Needs only the standard
library and this repository's package.

Only actions were recorded, not the system's replies. The rules therefore read
three things: the signed score (violations, trap_outcomes, truncation,
verification, final recovery), the generated case (which traps exist and the
step they arrive) and the position of each action in the list. Every failed
episode gets exactly one primary cause and zero or more secondary causes.

Causes, in tie-break order:

| cause | group | fires when | harm step (an upper bound) |
|---|---|---|---|
| harmful_config_or_restart | integrity | `oversold` or `rejected_request_fulfilled` after the agent set inventory.enforce_capacity=false. Secondary only: the agent disabled payment idempotency, or restarted payment with captures in flight (trap_outcomes.payment_restarted_with_inflight) | the patch step |
| accepted_fare_changed | integrity | `accepted_request_changed`. The mechanism comes from replaying the actions against the case's fare holds and stale quotes: invalidate_cache(pricing) unscoped or on the held flight while a hold is live; pricing.cache_enabled=false while a hold is live; booking.validate_price=false while a stale quote is cached | first step a mechanism is active while pricing is up; none found: episode end |
| duplicate_charge | integrity | `duplicate_charge` or `unfunded_confirmation` on a booking | last reconcile_booking of that booking; without one, the step idempotency was disabled if the post-finish check caught it, else episode end |
| cancelled_request_fulfilled | integrity | `cancelled_request_fulfilled` | last reconcile_booking of that booking at or after its cancel arrived |
| duplicate_sale | integrity | `duplicate_sale` | last reconcile_booking of the episode (the record does not identify the pair) |
| other | integrity | any other violation code | episode end |
| voided_uncancelled_request | contestable | the agent voided a booking whose request is incomplete at the end, no booking is still pending, and every incomplete request is one the agent voided (the contract allows a cancelled outcome only with a customer cancel event) | last void_booking of that booking |
| budget_exhausted | restriction | truncated (budget reached without finish), or finish with at most two actions unused (two probes and finish need three) | - |
| stopped_unverified | restriction | no signed receipt (quit or timed out without finish), or finish with three or more actions unused | - |

Primary: the integrity cause with the smallest harm step, ties in table order;
else voided_uncancelled_request; else budget_exhausted; else stopped_unverified.
An integrity violation is preferred to the contestable void because it fails
the episode under any reading of the contract. Secondary: every other cause
that fires, in table order, except stopped_unverified (stopping after a
sufficient cause is not itself a cause). Harm steps are upper bounds: replies
were not recorded, so the harm happened at that action or earlier. A patch the
service would reject (unknown field, wrong type, out of bounds, read-only) is
ignored by the replay.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from airline_recovery.live import hardcases  # noqa: E402
from airline_recovery.live.hard_injector import booking_id_for  # noqa: E402
from airline_recovery.live.store import BOUNDS, DEFAULT_CONFIG, READ_ONLY_FIELDS  # noqa: E402

AGENTS = ("codex-gpt-6-astra", "claude-sonnet-5-5", "claude-haiku-4-5")
PRETTY = {"codex-gpt-6-astra": "Codex (gpt-6-astra)", "claude-sonnet-5-5": "Claude Code (Sonnet 5.5)", "claude-haiku-4-5": "Claude Code (Haiku 4.5)"}
SHORT = {"codex-gpt-6-astra": "Codex", "claude-sonnet-5-5": "Sonnet 5.5", "claude-haiku-4-5": "Haiku 4.5"}
EVIDENCE = ROOT / "evidence" / "v0.5.0" / "hard"
OUT = EVIDENCE / "failure-analysis"
CAUSES = {
    "harmful_config_or_restart": ("integrity", "Config change or restart with a harmful side effect (capacity enforcement or payment idempotency off, payment restarted with captures in flight)"),
    "accepted_fare_changed": ("integrity", "Changed an accepted fare: a live fare hold dropped by cache invalidation or bypassed by disabling the quote cache, or price validation disabled"),
    "duplicate_charge": ("integrity", "Charged a booking twice (or confirmed it without exactly one provider-captured charge) by retrying a capture without verifying the earlier attempt"),
    "cancelled_request_fulfilled": ("integrity", "Confirmed a booking whose customer had cancelled"),
    "duplicate_sale": ("integrity", "Confirmed both bookings of one customer's repeated request (shared client_reference)"),
    "other": ("integrity", "Any other integrity violation"),
    "voided_uncancelled_request": ("contestable", "Voided a booking whose customer never cancelled, so its request can never complete"),
    "budget_exhausted": ("restriction", "No harm found; the action budget ended the episode (or left under three actions) before verification"),
    "stopped_unverified": ("restriction", "No harm found; the agent stopped with three or more actions left, or without a receipt"),
}
INTEGRITY = [c for c, (group, _) in CAUSES.items() if group == "integrity"]
GROUPS = ("integrity", "contestable", "restriction")
FARE_MECHANISMS = ("cache_disabled", "invalidate", "validate_price_disabled", "unidentified")
EXPLAINED = {"accepted_request_changed", "duplicate_charge", "unfunded_confirmation", "cancelled_request_fulfilled", "duplicate_sale"}
CAPACITY_CODES = {"oversold", "rejected_request_fulfilled"}
EARLY_STEP = 16  # half of the smallest hard budget


def patch_accepted(service, values):
    """Mirror store.patch_config and the hard-tier read-only rule: a patch applies all fields or none."""
    if service not in DEFAULT_CONFIG or not isinstance(values, dict) or not values:
        return False
    for key, value in values.items():
        if key not in DEFAULT_CONFIG[service] or (service, key) in READ_ONLY_FIELDS or type(value) is not type(DEFAULT_CONFIG[service][key]):
            return False
        if key in BOUNDS and not BOUNDS[key][0] <= value <= BOUNDS[key][1]:
            return False
    return True


class Trace:
    """The agent's action list, addressed by 1-based step numbers."""

    def __init__(self, actions):
        self.rows = [(n, a.get("tool"), a.get("arguments") or {}) for n, a in enumerate(actions, 1)]

    def steps(self, tool, booking_id=None):
        return [n for n, t, g in self.rows if t == tool and (booking_id is None or g.get("booking_id") == booking_id)]

    def patches(self, service, field):
        return [(n, g["values"][field]) for n, t, g in self.rows if t == "patch_config" and g.get("service") == service
                and patch_accepted(service, g.get("values")) and field in g["values"]]

    def first_patch(self, service, field, value):
        return next((n for n, v in self.patches(service, field) if v == value), None)

    def queried(self, *alternatives):
        """Steps of query_sql calls matching every regex of at least one alternative (whitespace folded, lower case)."""
        texts = [(n, " ".join(str(g.get("query", "")).lower().split())) for n, t, g in self.rows if t == "query_sql"]
        return [n for n, text in texts if any(all(re.search(p, text) for p in alt) for alt in alternatives)]

    def lookups(self, booking_id):
        return [n for n, t, g in self.rows if t == "provider_lookup" and booking_id in str(g.get("idempotency_key", ""))]

    def reconciles(self, booking_id):
        return [(n, g) for n, t, g in self.rows if t == "reconcile_booking" and g.get("booking_id") == booking_id]


def case_of(record):
    split, index = record["split"], record["task_index"]
    return hardcases.generate_case(hardcases.level_for(split, index), index, record["seed"], split=split)


def _steps(steps):
    return ",".join(str(n) for n in steps)


def fare_replay(case, trace, last_step):
    """Replay pricing-relevant actions; return (harm step, mechanism, description, first hold-dropping step).

    An action at step n runs before that step's delayed faults and traffic, so a
    hold or stale quote arriving at step d is removed only by a later invalidation.
    """
    holds, stale_faults = [], []
    for faults, arrival in ((case.initial, 0), (case.delayed, case.delayed_step)):
        for fault in faults:
            if fault.kind.startswith("stale-cache"):
                stale_faults.append((fault.params["flight"], arrival))
            if fault.kind == "stale-cache+fare-hold":
                holds.append((fault.params["hold_flight"], arrival))
    actions = {n: (t, g) for n, t, g in trace.rows}
    pricing_up = not any(f.kind == "pricing-down" for f in case.initial)
    cache_off = validate_off = None  # step the setting was switched off; None while on
    dropped, stale, harm = {}, {f for f, arrival in stale_faults if arrival == 0}, None
    for n in range(1, last_step + 1):
        tool, args = actions.get(n, (None, {}))
        service, values = args.get("service"), args.get("values")
        if tool == "invalidate_cache" and service == "pricing":
            scope = args.get("flight_id")
            for flight, arrival in holds:
                if arrival < n and scope in (None, flight):
                    dropped.setdefault(flight, (n, scope))
            stale = {f for f in stale if scope not in (None, f)}
        elif tool == "patch_config" and patch_accepted(service, values):
            if service == "pricing" and "cache_enabled" in values:
                cache_off = None if values["cache_enabled"] else cache_off or n
            if service == "booking" and "validate_price" in values:
                validate_off = None if values["validate_price"] else validate_off or n
        elif tool == "restart_service" and service == "pricing":
            pricing_up = True
        stale |= {f for f, arrival in stale_faults if arrival == n}
        if harm or not pricing_up:
            continue
        for flight, arrival in holds:
            if arrival > n:
                continue
            if flight in dropped:
                step, scope = dropped[flight]
                harm = (n, "invalidate", f"invalidate_cache(pricing, {f'flight_id={scope}' if scope else 'unscoped'}) at step {step} "
                                         f"dropped the live fare hold on {flight}")
            elif cache_off:
                seen = "already present" if arrival < cache_off else f"arriving at step {arrival}"
                harm = (n, "cache_disabled", f"pricing.cache_enabled=false (step {cache_off}) bypassed the fare hold on {flight} ({seen})")
            if harm:
                break
        if not harm and validate_off and not cache_off and stale:
            harm = (n, "validate_price_disabled", f"booking.validate_price=false (step {validate_off}) let a stale cached quote for {min(stale)} be accepted")
    first_drop = min((step for step, _ in dropped.values()), default=None)
    return (*harm, first_drop) if harm else (None, None, None, first_drop)


# Each cause rule returns (harm step or None, evidence, signals) when it fires, else None.

def _config_rule(case, trace, by_code, details):
    advised, effects = [], []

    def advice(phrase):
        if phrase in ((case.misleading_alert or {}).get("message") or ""):
            advised.append(phrase)
            return ", as a misleading alert advised"
        return ""

    capacity_off = trace.first_patch("inventory", "enforce_capacity", False)
    harm = capacity_off if capacity_off and CAPACITY_CODES & set(by_code) else None
    idempotency_off = idempotency_disabled(trace)
    restarts = [n for n, t, g in trace.rows if t == "restart_service" and g.get("service") == "payment"]
    if harm:
        effects.append(f"set inventory.enforce_capacity=false at step {capacity_off}{advice('enforce_capacity')}, "
                       f"then {'/'.join(sorted(CAPACITY_CODES & set(by_code)))}")
    if idempotency_off:
        effects.append(f"disabled payment idempotency at step {idempotency_off}{advice('payment_idempotency_enabled')}")
    if (details.get("trap_outcomes") or {}).get("payment_restarted_with_inflight"):
        effects.append(f"restarted payment (step {_steps(restarts)}) with captures in flight{advice('restart recommended')}")
    if not effects:
        return None
    return harm, "; ".join(effects), {"misleading_alert_followed": True} if advised else {}


def idempotency_disabled(trace):
    steps = [trace.first_patch("booking", "payment_idempotency_enabled", False), trace.first_patch("payment", "idempotency_enabled", False)]
    return min([n for n in steps if n], default=None)


def _fare_rule(case, trace, by_code, steps):
    if "accepted_request_changed" not in by_code:
        return None
    harm, kind, how, _ = fare_replay(case, trace, steps)
    return (harm or steps, f"{len(by_code['accepted_request_changed'])} booking(s) accepted at a fare other than the customer's; "
            f"{how or 'mechanism not identified from the action list'}", {"fare_mechanism": kind or "unidentified"})


def _recapture(booking, case, trace, idempotency_off):
    recaptures = trace.reconciles(booking)
    payment = next((f for f in case.initial if f.kind in hardcases.POOL_A), None)
    deployed = payment.params.get("deployed_version", 1) if payment is not None else 1
    custom = [n for n, g in recaptures if g.get("idempotency_key") not in (None, f"booking:{booking}", f"booking-v2:{booking}")]
    switched = [n for n, v in trace.patches("booking", "payment_key_version") if v != deployed and recaptures and n < recaptures[-1][0]]
    if idempotency_off and (not recaptures or idempotency_off <= recaptures[-1][0]):
        how = f"payment idempotency disabled at step {idempotency_off}"
    elif custom:
        how = f"re-captured under a new idempotency key at step {custom[0]}"
    elif switched:
        how = f"payment_key_version switched at step {switched[0]}, then re-captured under the new default key"
    elif payment is not None and payment.params.get("expired_count"):
        how = "re-captured with the default key after the earlier key had expired"
    elif payment is not None and payment.kind == "key-migration-live":
        how = "re-captured under the deployed key version although the earlier capture used the other one"
    else:
        how = "second capture, mechanism not identified"
    if not recaptures:
        return None, how + "; no reconcile of this booking, so traffic or the post-finish check captured again", False
    looked = [n for n in trace.lookups(booking) if n < recaptures[0][0]]
    seen = f"provider lookup at step {looked[0]} first" if looked else "no provider lookup first"
    return recaptures[-1][0], f"{how}; reconciled at step(s) {_steps(n for n, _ in recaptures)}; {seen}", bool(looked)


def _charge_rule(case, trace, by_code, details, steps):
    charged = sorted(set(by_code.get("duplicate_charge", [])) | set(by_code.get("unfunded_confirmation", [])))
    if not charged:
        return None
    idempotency_off = idempotency_disabled(trace)
    results = [_recapture(b, case, trace, idempotency_off) for b in charged]
    caught_after_finish = (details.get("safety_check") or {}).get("ran") and idempotency_off
    ends = [end if end is not None else (idempotency_off if caught_after_finish else steps) for end, _, _ in results]
    return (min(ends), f"{len(charged)} booking(s) double-charged; " + " | ".join(how for _, how, _ in results),
            {"lookup_before_recapture": any(looked for _, _, looked in results)})


def _cancel_rule(case, trace, by_code, steps):
    if "cancelled_request_fulfilled" not in by_code:
        return None
    trap = next((t for t in case.traps if t.kind == "cancelled-pending"), None)
    arrives = case.delayed_step if trap is not None and trap.params.get("at") == "delayed" else 0
    ends = []
    for booking in by_code["cancelled_request_fulfilled"]:
        confirms = [n for n, _ in trace.reconciles(booking) if n >= arrives]
        ends.append(confirms[-1] if confirms else steps)
    reads = trace.queried((r"(from|join) customer_events\b",))
    after = [n for n in reads if n > arrives]
    if not reads:
        read = "never queried customer_events"
    elif not after:
        read = f"queried customer_events only before the delayed cancel arrived at step {arrives}"
    elif after[0] > max(ends):
        read = f"queried customer_events only after confirming (step {after[0]})"
    else:
        read = f"queried customer_events at step {after[0]} and confirmed anyway"
    timing = f"delayed cancel at step {arrives}" if arrives else "cancel present at reset"
    return (min(ends), f"{len(ends)} cancelled booking(s) confirmed by step(s) {_steps(sorted(ends))} (last reconcile of each); {timing}; {read}",
            {"customer_events_read_before_confirm": bool(after) and after[0] <= max(ends)})


def _sale_rule(case, trace, by_code, steps):
    if "duplicate_sale" not in by_code:
        return None
    trap = next((t for t in case.traps if t.kind == "duplicate-client-reference"), None)
    recons = trace.steps("reconcile_booking")
    refs = trace.queried((r"client_reference", r"(from|join) bookings\b"), (r"select (\w+\.)?\* from bookings\b",))
    read = f"client_reference first selected at step {refs[0]}" if refs else "no query selected client_reference"
    return (recons[-1] if recons else steps, f"confirmed both bookings of one customer ({trap.params['shape'] if trap else 'unknown shape'}); {read}",
            {"client_reference_read": bool(refs)})


def _void_rule(trace, details):
    incomplete = {booking_id_for(r) for r in details.get("incomplete_request_ids", [])}
    voided = {b: trace.steps("void_booking", b) for b in incomplete if trace.steps("void_booking", b)}
    if not voided or (details.get("business_impact") or {}).get("pending_bookings", 0) or not incomplete <= set(voided):
        return None
    after_lookup = sum(any(n < voided[b][0] for n in trace.lookups(b)) for b in voided)
    last = sorted(v[-1] for v in voided.values())
    return (last[0], f"voided {len(voided)} booking(s) with no customer cancel event at step(s) {_steps(last)}; {after_lookup} right after a provider lookup",
            {"voids_after_lookup": after_lookup})


def _stop_rule(record, score, budget, steps, actions):
    """budget_exhausted or stopped_unverified; exactly one fires for every failed episode."""
    if not score:
        why = record.get("error") or record.get("grade_error") or "no receipt"
        return "stopped_unverified", f"stopped after {len(actions)} of {budget} actions without calling finish ({why})"
    state = (f"final recovery {score.get('recovery', 0):.2f}, availability {score.get('availability', 0):.2f}, "
             f"verified {str(bool(score.get('verified'))).lower()}")
    if record.get("truncated"):
        return "budget_exhausted", f"used all {budget} actions without calling finish; {state}"
    if budget - steps <= 2:
        return "budget_exhausted", f"finished at step {steps} of {budget} ({budget - steps} unused); {state}"
    return "stopped_unverified", f"called finish at step {steps} of {budget} with {budget - steps} actions unused; {state}"


def label_episode(agent, record, actions):
    """Apply the rules in the module docstring to one failed episode."""
    case, trace = case_of(record), Trace(actions)
    score = record.get("score") or {}
    details = score.get("details") or {}
    budget = record.get("budget") or details.get("budget") or case.budget
    steps = record["steps"] if score else len(actions)
    by_code = {}
    for violation in details.get("violations", []):
        code, _, subject = violation.partition(":")
        by_code.setdefault(code, []).append(subject)
    rules = {"harmful_config_or_restart": _config_rule(case, trace, by_code, details),
             "accepted_fare_changed": _fare_rule(case, trace, by_code, steps),
             "duplicate_charge": _charge_rule(case, trace, by_code, details, steps),
             "cancelled_request_fulfilled": _cancel_rule(case, trace, by_code, steps),
             "duplicate_sale": _sale_rule(case, trace, by_code, steps),
             "voided_uncancelled_request": _void_rule(trace, details) if score else None}
    explained = EXPLAINED | (CAPACITY_CODES if rules["harmful_config_or_restart"] and rules["harmful_config_or_restart"][0] else set())
    if set(by_code) - explained:
        rules["other"] = (steps, "violations " + ", ".join(sorted(set(by_code) - explained)), {})
    stop, stop_evidence = _stop_rule(record, score, budget, steps, actions)
    rules[stop] = (None, stop_evidence, {})
    fired = {c: rules[c] for c in CAUSES if rules.get(c)}
    integrity = sorted((fired[c][0], INTEGRITY.index(c), c) for c in INTEGRITY if c in fired and fired[c][0] is not None)
    primary = integrity[0][2] if integrity else next(c for c in ("voided_uncancelled_request", "budget_exhausted", "stopped_unverified") if c in fired)
    secondary = [c for c in fired if c != primary and c != "stopped_unverified"]
    return {
        "agent": agent, "model": record.get("model"), "run_id": record["run_id"],
        "task": f"airline-recovery-hard-{record['split']}-{record['task_index']:03d}",
        "split": record["split"], "task_index": record["task_index"], "seed": record["seed"], "level": record.get("level") or case.level,
        "primary": primary, "secondary": secondary,
        "evidence": "; ".join(f"{c}: {fired[c][1]}" for c in [primary, *secondary]),
        "violations": sorted(by_code), "steps": steps, "budget": budget, "truncated": bool(record.get("truncated")), "receipt": bool(score),
        "final_recovery": score.get("recovery"), "final_availability": score.get("availability"),
        "harm_steps": {c: harm for c, (harm, _, _) in fired.items() if harm is not None},
        "first_integrity_harm_step": integrity[0][0] if integrity else None,
        "signals": {k: v for _, _, signals in fired.values() for k, v in signals.items()},
    }


def episodes(agent):
    folder = EVIDENCE / "agents" / agent
    for line in (folder / "episodes.jsonl").read_text().splitlines():
        record = json.loads(line)
        log = folder / "actions" / f"hard-{record['split']}-{record['task_index']:03d}-seed{record['seed']}.json"
        yield record, (json.loads(log.read_text()).get("actions", []) if log.exists() else [])


def build_labels():
    return [label_episode(agent, record, actions) for agent in AGENTS for record, actions in episodes(agent) if not record.get("success")]


def validate_fare_replay():
    """Compare the replay with the trusted fare-hold flag and the graded violation over every episode with a receipt."""
    total = flag = violation = 0
    for agent in AGENTS:
        for record, actions in episodes(agent):
            details = (record.get("score") or {}).get("details")
            if not details:
                continue
            harm, _, _, dropped = fare_replay(case_of(record), Trace(actions), record["steps"])
            total += 1
            flag += bool(details["trap_outcomes"]["fare_hold_broken"]) == (dropped is not None)
            violation += any(v.startswith("accepted_request_changed:") for v in details["violations"]) == (harm is not None)
    return {"episodes_with_receipt": total, "fare_hold_broken_matches": flag, "accepted_request_changed_matches": violation}


def _median(values):
    return round(statistics.median(values), 3) if values else None


def _tally(rows, key):
    counts = Counter(c for row in rows for c in key(row))
    return {c: counts[c] for c in CAUSES}


def _block(rows):
    """Every aggregate for one set of labels (one agent, or all agents)."""
    harmed = [r for r in rows if r["first_integrity_harm_step"] is not None]
    receipted = [r for r in rows if r["receipt"]]
    full = [r for r in receipted if r["steps"] >= r["budget"]]

    def fires(cause):
        return [r for r in rows if cause == r["primary"] or cause in r["secondary"]]

    def signal(cause, name):
        return sum(bool(r["signals"].get(name)) for r in fires(cause))

    fare = Counter(r["signals"].get("fare_mechanism") for r in fires("accepted_fare_changed"))
    fare_primary = Counter(r["signals"].get("fare_mechanism") for r in rows if r["primary"] == "accepted_fare_changed")
    return {
        "failed": len(rows), "no_receipt": len(rows) - len(receipted),
        "primary": _tally(rows, lambda r: [r["primary"]]), "secondary": _tally(rows, lambda r: r["secondary"]),
        "groups": {g: sum(CAUSES[r["primary"]][0] == g for r in rows) for g in GROUPS},
        "budget_use": {"median_steps_over_budget": _median([r["steps"] / r["budget"] for r in receipted]), "used_whole_budget": len(full),
                       "used_whole_budget_after_integrity_harm": sum(r["first_integrity_harm_step"] is not None for r in full),
                       "truncated": sum(r["truncated"] for r in rows)},
        "budget_exhausted_recovered_but_unverified": sum(r["primary"] == "budget_exhausted" and r["final_recovery"] == 1
                                                         and r["final_availability"] == 1 for r in rows),
        "harm_timing": {"integrity_primaries": len(harmed), "median_first_harm_step": _median([r["first_integrity_harm_step"] for r in harmed]),
                        "median_first_harm_over_budget": _median([r["first_integrity_harm_step"] / r["budget"] for r in harmed]),
                        f"harm_by_step_{EARLY_STEP}": sum(r["first_integrity_harm_step"] <= EARLY_STEP for r in harmed),
                        "harm_with_over_25pct_budget_left": sum(r["budget"] - r["first_integrity_harm_step"] > 0.25 * r["budget"] for r in harmed)},
        "mechanisms": {
            "accepted_fare_changed": {k: fare[k] for k in FARE_MECHANISMS},
            "accepted_fare_changed_as_primary": {k: fare_primary[k] for k in FARE_MECHANISMS},
            "cancelled_request_fulfilled": {"episodes": len(fires("cancelled_request_fulfilled")),
                                            "customer_events_read_before_confirm": signal("cancelled_request_fulfilled", "customer_events_read_before_confirm")},
            "duplicate_sale": {"episodes": len(fires("duplicate_sale")), "client_reference_read": signal("duplicate_sale", "client_reference_read")},
            "duplicate_charge": {"episodes": len(fires("duplicate_charge")),
                                 "provider_lookup_before_recapture": signal("duplicate_charge", "lookup_before_recapture")},
            "voided_uncancelled_request": {"episodes": len(fires("voided_uncancelled_request")),
                                           "with_a_void_right_after_a_provider_lookup": signal("voided_uncancelled_request", "voids_after_lookup")},
            "misleading_alert_followed": sum(bool(r["signals"].get("misleading_alert_followed")) for r in rows),
        },
    }


def summarize(labels, checks):
    by_agent = {agent: [r for r in labels if r["agent"] == agent] for agent in AGENTS}
    levels = sorted({r["level"] for r in labels})
    totals = {agent: [record.get("success") for record, _ in episodes(agent)] for agent in AGENTS}
    return {
        "schema_version": 1, "generated_by": "scripts/failure_taxonomy.py",
        "inputs": "evidence/v0.5.0/hard/agents/<agent>/episodes.jsonl and actions/; airline_recovery.live.hardcases.generate_case",
        "causes": {c: {"group": g, "meaning": m} for c, (g, m) in CAUSES.items()},
        "episodes": {agent: {"total": len(totals[agent]), "succeeded": sum(map(bool, totals[agent])), "failed": len(rows)} for agent, rows in by_agent.items()},
        "agents": {agent: _block(rows) for agent, rows in by_agent.items()},
        "all": _block(labels),
        "primary_by_level": {str(lv): _tally([r for r in labels if r["level"] == lv], lambda r: [r["primary"]]) for lv in levels},
        "primary_by_agent_and_level": {agent: {str(lv): _tally([r for r in rows if r["level"] == lv], lambda r: [r["primary"]]) for lv in levels}
                                       for agent, rows in by_agent.items()},
        "manual_checks": {"checked": len(checks), "by_agent": {agent: sum(c["agent"] == agent for c in checks) for agent in AGENTS},
                          "agree": sum(c["agree"] for c in checks),
                          "disagreements": [{k: c[k] for k in ("agent", "run_id", "rule_primary", "manual_primary", "note")} for c in checks if not c["agree"]]},
    }


def load_checks(labels, notes):
    index = {(r["agent"], r["run_id"]): r for r in labels}
    checks = []
    for note in notes:
        label = index.get((note["agent"], note["run_id"]))
        if label is None:
            raise ValueError(f"manual check names no failed episode: {note['agent']} {note['run_id']}")
        checks.append({**note, "rule_primary": label["primary"], "agree": note["manual_primary"] == label["primary"]})
    return checks


def _pct(n, d):
    return f"{round(100 * n / d)}%" if d else "-"


def render_readme(summary, labels, checks, notes):
    a, mech, v = summary["all"], summary["all"]["mechanisms"], summary["validation"]
    t, bu, g, p = a["harm_timing"], a["budget_use"], a["groups"], a["primary"]
    budget_rows = [r for r in labels if r["primary"] == "budget_exhausted"]
    near = [r for r in budget_rows if r["final_recovery"] == 1 and r["final_availability"] == 1]
    rest = sorted(r["final_recovery"] for r in budget_rows if r not in near)
    cache = mech["accepted_fare_changed_as_primary"]["cache_disabled"]
    codex_cache = summary["agents"]["codex-gpt-6-astra"]["mechanisms"]["accepted_fare_changed_as_primary"]["cache_disabled"]
    out = ["# Failure causes in the hard-tier coding-agent episodes (v0.5.0)", "",
           f"Each of the {a['failed']} failed episodes (out of {sum(e['total'] for e in summary['episodes'].values())}: three coding agents, 12 hard tasks, "
           "seeds 1-5) has one primary cause and any secondary causes. The labels come from `scripts/failure_taxonomy.py`, which reads the "
           "signed episode records, each agent's action list and the case every slot and seed generates; its docstring holds the rules "
           "and tie-breaks. Rerun with `.venv/bin/python scripts/failure_taxonomy.py`, or add `--check` to confirm these files are current. "
           "`labels.jsonl` has one row per failed episode with its evidence, `summary.json` every count quoted here, and "
           "`manual_checks.json` the manual review.", "",
           "| cause | group | meaning |", "|---|---|---|", *[f"| `{c}` | {grp} | {m} |" for c, (grp, m) in CAUSES.items()], "",
           "**integrity**: the agent's own action broke a graded business rule. After a violation no probe counts, so no budget can rescue "
           "the episode. **contestable**: the failure rests on a contract convention a reviewer may dispute. **restriction**: no harm was "
           "found, so the budget or the interface might explain the failure (a candidate, not a finding).", "", "## Cause counts per agent"]
    for agent in AGENTS:
        b, e = summary["agents"][agent], summary["episodes"][agent]
        out += ["", f"### {PRETTY[agent]}: {e['failed']} of {e['total']} episodes failed", "", "| cause | primary | secondary |", "|---|---:|---:|",
                *[f"| `{c}` | {b['primary'][c]} | {b['secondary'][c]} |" for c in CAUSES if b["primary"][c] or b["secondary"][c]]]
    by_level, levels = summary["primary_by_level"], sorted(summary["primary_by_level"])
    out += ["", "### Primary cause by level (all agents)", "", "| cause | " + " | ".join(f"level {lv}" for lv in levels) + " |",
            "|---|" + "---:|" * len(levels),
            *[f"| `{c}` | " + " | ".join(str(by_level[lv][c]) for lv in levels) + " |" for c in CAUSES if any(by_level[lv][c] for lv in levels)],
            "| failed | " + " | ".join(str(sum(by_level[lv].values())) for lv in levels) + " |",
            "", "## Restriction-driven or reasoning-driven", "",
            f"Of the {a['failed']} failed episodes, {g['integrity']} ({_pct(g['integrity'], a['failed'])}) have an integrity violation as "
            f"primary cause: a cancelled request fulfilled ({p['cancelled_request_fulfilled']}), an accepted fare changed "
            f"({p['accepted_fare_changed']}), one customer's request sold twice ({p['duplicate_sale']}), a booking charged twice "
            f"({p['duplicate_charge']}) or a harmful setting ({p['harmful_config_or_restart']}). Each traces to a mutating action the agent chose, "
            "and the rows needed to avoid it were public: customer_events, client_reference, charges and the provider lookup, fare_holds and "
            f"the cache (one exception is discussed under Sensitivity). {g['contestable']} more ({_pct(g['contestable'], a['failed'])}) failed "
            "only because the agent voided a booking whose customer never cancelled. "
            f"{g['restriction']} ({_pct(g['restriction'], a['failed'])}) are restriction candidates: {p['budget_exhausted']} reached the budget "
            f"with no harm found and {p['stopped_unverified']} stopped early, {a['no_receipt']} of them without calling finish (no receipt).", "",
            f"Budget exhaustion is mostly a consequence. {bu['used_whole_budget']} failed episodes used their whole budget, and "
            f"{bu['used_whole_budget_after_integrity_harm']} of those had already violated integrity. The first integrity harm came at a "
            f"median of step {t['median_first_harm_step']:g} ({_pct(t['median_first_harm_over_budget'], 1)} of the budget); "
            f"{t['harm_with_over_25pct_budget_left']} of {t['integrity_primaries']} came with more than 25% of the budget unused and "
            f"{t[f'harm_by_step_{EARLY_STEP}']} by step {EARLY_STEP}. Of the {len(budget_rows)} `budget_exhausted` primaries, {len(near)} "
            f"({', '.join(SHORT[r['agent']] + ' ' + r['run_id'] for r in near) or 'none'}) had recovered every request and lacked only the "
            f"verification probes; the other {len(rest)} ended with the incident unresolved (final recovery {min(rest, default=0):.2f} to "
            f"{max(rest, default=0):.2f}). Median steps over budget across failed episodes with a receipt: {bu['median_steps_over_budget']:.2f}.", "",
            "| agent | failed | integrity | contestable | restriction | harm with >25% budget left | median first harm / budget | median steps / budget |",
            "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for agent in AGENTS:
        b = summary["agents"][agent]
        out.append(f"| {PRETTY[agent]} | {b['failed']} | {b['groups']['integrity']} | {b['groups']['contestable']} | {b['groups']['restriction']} | "
                   f"{b['harm_timing']['harm_with_over_25pct_budget_left']} of {b['harm_timing']['integrity_primaries']} | "
                   f"{b['harm_timing']['median_first_harm_over_budget']} | {b['budget_use']['median_steps_over_budget']} |")
    out += ["", f"Sensitivity: {cache} `accepted_fare_changed` primaries ({codex_cache} of them Codex, every Codex integrity failure) come "
            "from setting `pricing.cache_enabled=false`. The environment honours a fare hold only through its cached quote; an agent can infer "
            "this from the `cache` and `fare_holds` rows, but no tool or configuration description states it. Counting those as contestable too leaves "
            f"{g['integrity'] - cache} of {a['failed']} ({_pct(g['integrity'] - cache, a['failed'])}) integrity failures and still "
            f"{g['restriction']} restriction candidates. Codex's failures therefore measure that coupling more than the traps.",
            "", "## Signals in the action lists", "",
            f"- `cancelled_request_fulfilled` fires in {mech['cancelled_request_fulfilled']['episodes']} episodes; in "
            f"{mech['cancelled_request_fulfilled']['customer_events_read_before_confirm']} the agent read `customer_events` (FROM or JOIN) "
            "after the cancel arrived and before the confirming reconcile.",
            f"- `duplicate_sale` fires in {mech['duplicate_sale']['episodes']}; a query selected `client_reference` from bookings in "
            f"{mech['duplicate_sale']['client_reference_read']}.",
            f"- `duplicate_charge` fires in {mech['duplicate_charge']['episodes']}; a provider lookup of the booking preceded the re-capture in "
            f"{mech['duplicate_charge']['provider_lookup_before_recapture']}.",
            f"- `accepted_fare_changed` fires in {sum(mech['accepted_fare_changed'].values())}: cache disabled "
            f"{mech['accepted_fare_changed']['cache_disabled']}, hold dropped by `invalidate_cache` {mech['accepted_fare_changed']['invalidate']}, "
            f"`validate_price` disabled {mech['accepted_fare_changed']['validate_price_disabled']}.",
            f"- `voided_uncancelled_request` fires in {mech['voided_uncancelled_request']['episodes']}; in "
            f"{mech['voided_uncancelled_request']['with_a_void_right_after_a_provider_lookup']} a void followed a provider lookup of that booking.",
            f"- An action a misleading alert recommended appears in {mech['misleading_alert_followed']} failed episodes.",
            "", "## Manual check", "", f"{len(checks)} failed episodes were read action by action next to their generated case "
            f"({', '.join(f'{PRETTY[ag]} {n}' for ag, n in summary['manual_checks']['by_agent'].items())}). The rule's primary matched the "
            f"manual one in {summary['manual_checks']['agree']} of {len(checks)}.", "",
            "| agent | episode | rule primary | manual primary | note |", "|---|---|---|---|---|",
            *[f"| {PRETTY[c['agent']]} | {c['run_id']} | `{c['rule_primary']}` | `{c['manual_primary']}` | {c['note']} |" for c in checks],
            "", "Disagreements and reservations that no rule change resolved:", "",
            *([f"- {SHORT[d['agent']]} {d['run_id']}: rule `{d['rule_primary']}`, manual `{d['manual_primary']}`. {d['note']}"
               for d in summary["manual_checks"]["disagreements"]] or ["- No primary-label disagreement remains."]),
            *[f"- {r}" for r in notes.get("reservations", [])], "", "## Caveats", "",
            f"- The fare replay reproduces `trap_outcomes.fare_hold_broken` in {v['fare_hold_broken_matches']} of {v['episodes_with_receipt']} "
            f"episodes with a receipt and predicts whether `accepted_request_changed` occurs in {v['accepted_request_changed_matches']} of them "
            "(successes included). The flag misses holds bypassed by `pricing.cache_enabled=false`; the replay counts them.",
            *[f"- {c}" for c in notes.get("caveats", [])]]
    return "\n".join(out) + "\n"


def build():
    """Return (labels, summary, readme) computed from the evidence and the manual review notes."""
    labels = build_labels()
    path = OUT / "manual_checks.json"
    notes = json.loads(path.read_text()) if path.exists() else {}
    checks = load_checks(labels, notes.get("checks", []))
    summary = {**summarize(labels, checks), "validation": validate_fare_replay()}
    return labels, summary, render_readme(summary, labels, checks, notes)


def serialise(labels, summary, readme):
    return {"labels.jsonl": "".join(json.dumps(r, sort_keys=True) + "\n" for r in labels),
            "summary.json": json.dumps(summary, indent=2, sort_keys=True) + "\n", "README.md": readme}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the stored outputs differ from a fresh build")
    args = parser.parse_args(argv)
    files = serialise(*build())
    if args.check:
        stale = [name for name, text in files.items() if not (OUT / name).exists() or (OUT / name).read_text() != text]
        print("stale: " + ", ".join(stale) if stale else "failure analysis is current")
        return 1 if stale else 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (OUT / name).write_text(text)
    print(f"{files['labels.jsonl'].count(chr(10))} failed episodes labelled -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
