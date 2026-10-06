"""The easy reset observation is frozen; the hard tier leaks nothing and prices every action.

The golden test always runs. The hard-tier tests skip until the hard tier is
importable (``hardcases``, a tiered ``case_for`` and ``LiveAirlineEnv(max_steps=None)``).
"""
import importlib.util
import inspect
import json
import unittest
from pathlib import Path

from airline_recovery.live.environment import LiveAirlineEnv
from airline_recovery.live.scenarios import case_for
from airline_recovery.live.store import configuration_contracts

GOLDEN = Path(__file__).with_name("golden_easy_reset.json")
FROZEN_FIELDS = ("available_tools", "configuration_contracts", "episode_contract", "mission")
HARD_CONTRACT_KEYS = {"max_actions", "verification_eligible_from_step", "required_healthy_probes", "step_numbering",
                      "mutating_tools", "verification_reset_rule", "traffic_rule", "cost_rule", "provider_lookup_quota",
                      "graded_invariants", "telemetry_rule", "success_conditions", "post_finish_safety_check", "reward"}
HARD_OBSERVATION_KEYS = {"episode_id", "step", "alerts", "summary", "result", "available_tools",
                         "configuration_contracts", "episode_contract", "mission", "tier", "level"}
# Names that must never reach an agent: the generator's fault kinds and the private payment tables.
PRIVATE_WORDS = ["lost-ack-mixed", "key-migration-live", "payment-degraded-inflight", "expired-key", "schema-mixed",
                 "breaker-paused", "stale-cache", "fare-hold", "pricing-down", "inventory-down-then-up", "checkin-down",
                 "cancelled-pending", "duplicate-client-reference", "restart-bait", "log-retention", "misleading-alert",
                 "lying-log-line", "self-healed-transient", "settles_at_step", "provider_plan", "provider_ledger",
                 "episode_clock", "final_state"]


def _hard_available():
    if importlib.util.find_spec("airline_recovery.live.hardcases") is None:
        return False
    if "tier" not in inspect.signature(case_for).parameters:
        return False
    try:
        LiveAirlineEnv(max_steps=None)
    except ValueError:
        return False
    return True


HARD_AVAILABLE = _hard_available()
if HARD_AVAILABLE:
    from airline_recovery.live import hardcases
    for pool in ("POOL_A", "POOL_B", "POOL_C", "POOL_D", "POOL_F"):
        PRIVATE_WORDS.extend(getattr(hardcases, pool, ()))


def act(env, tool, **arguments):
    return env.step({"tool": tool, "arguments": arguments})


def _diff(expected, actual, path="$"):
    """First differing paths between two JSON values, for a readable golden failure."""
    if type(expected) is not type(actual):
        return [f"{path}: {type(expected).__name__} became {type(actual).__name__}"]
    if isinstance(expected, dict):
        lines = [f"{path}.{key}: missing" for key in expected if key not in actual]
        lines += [f"{path}.{key}: added" for key in actual if key not in expected]
        for key in expected:
            if key in actual:
                lines += _diff(expected[key], actual[key], f"{path}.{key}")
        return lines
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: {len(expected)} items became {len(actual)}"]
        return [line for i, (a, b) in enumerate(zip(expected, actual)) for line in _diff(a, b, f"{path}[{i}]")]
    return [] if expected == actual else [f"{path}: {expected!r} became {actual!r}"]


class GoldenEasyResetTests(unittest.TestCase):
    """The easy tier is frozen against the pre-hard-tier golden capture.

    Rule: episode_contract and mission are byte-identical; every tool and every
    configuration-contract entry in the golden is unchanged (name, description,
    parameters, bounds, readOnly). New tools and new configuration fields may be
    added, because the tool schema list and the contracts are shared by both tiers.
    """

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        with LiveAirlineEnv() as env:
            cls.observation, cls.info = env.reset(seed=1, options={"split": "train", "index": 1})

    def assertGolden(self, name, actual):
        expected = self.golden[name]
        if expected != actual:
            self.fail(f"easy {name} changed:\n  " + "\n  ".join(_diff(expected, actual)[:40]))

    def assertToolsSuperset(self, actual):
        """Every golden tool is present and unchanged; the golden order is preserved among them."""
        by_name = {tool["name"]: tool for tool in actual}
        lines = []
        for tool in self.golden["available_tools"]:
            if tool["name"] not in by_name:
                lines.append(f"tool {tool['name']}: missing")
            else:
                lines += _diff(tool, by_name[tool["name"]], f"tool {tool['name']}")
        golden_names = [tool["name"] for tool in self.golden["available_tools"]]
        kept_order = [name for name in by_name if name in golden_names]
        if kept_order != golden_names:
            lines.append(f"tool order changed: {kept_order}")
        if lines:
            self.fail("an existing easy tool changed:\n  " + "\n  ".join(lines[:40]))

    def assertContractsSuperset(self, actual):
        """Every golden service and field is present and unchanged; new fields may appear."""
        lines = []
        for service, contract in self.golden["configuration_contracts"].items():
            if service not in actual:
                lines.append(f"{service}: missing")
                continue
            for field, spec in contract["observed_fields"].items():
                found = actual[service]["observed_fields"].get(field)
                lines += [f"{service}.observed_fields.{field}: missing"] if found is None else \
                         _diff(spec, found, f"{service}.observed_fields.{field}")
            schema, now = contract["patch_schema"], actual[service]["patch_schema"]
            for key in schema:
                if key != "properties":
                    lines += _diff(schema[key], now.get(key), f"{service}.patch_schema.{key}")
            for field, spec in schema["properties"].items():
                found = now["properties"].get(field)
                lines += [f"{service}.patch_schema.{field}: missing"] if found is None else \
                         _diff(spec, found, f"{service}.patch_schema.{field}")
        if lines:
            self.fail("an existing easy configuration contract entry changed:\n  " + "\n  ".join(lines[:40]))

    def test_contract_text_and_mission_are_byte_identical(self):
        for name in ("episode_contract", "mission"):
            with self.subTest(field=name):
                self.assertGolden(name, self.observation[name])

    def test_existing_tools_and_contract_entries_are_unchanged(self):
        self.assertToolsSuperset(self.observation["available_tools"])
        self.assertContractsSuperset(self.observation["configuration_contracts"])

    def test_static_discovery_matches_golden(self):
        self.assertToolsSuperset(LiveAirlineEnv.tools())
        self.assertContractsSuperset(configuration_contracts())

    def test_reset_shape_and_info_match_golden(self):
        self.assertEqual(sorted(self.observation), self.golden["observation_keys"])
        self.assertEqual(sorted(self.observation["summary"]), self.golden["summary_keys"])
        self.assertEqual(self.info, self.golden["info"])
        self.assertEqual(self.observation["step"], 0)
        self.assertIsNone(self.observation["result"])

    def test_golden_file_is_the_easy_tier(self):
        text = GOLDEN.read_text(encoding="utf-8")
        self.assertEqual(self.golden["episode_contract"]["max_actions"], 48)
        self.assertNotIn('"tier"', text)
        self.assertNotIn("provider_lookup", text)
        self.assertNotIn("void_booking", text)

    def test_tier_option_is_not_an_easy_reset_option(self):
        # Easy callers never send a tier; a reset without one must behave exactly as before.
        with LiveAirlineEnv() as env:
            observation, _ = env.reset(seed=3, options={"split": "train", "index": 1})
        self.assertNotIn("tier", observation)
        self.assertNotIn("level", observation)


@unittest.skipUnless(HARD_AVAILABLE, "the hard tier is not importable in this build")
class HardObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = LiveAirlineEnv(max_steps=None)
        cls.observation, cls.info = cls.env.reset(seed=1, options={"split": "train", "index": 0, "tier": "hard"})
        cls.case = case_for("train", 0, tier="hard", seed=1)

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def test_observation_shape(self):
        observation = self.observation
        self.assertEqual(set(observation), HARD_OBSERVATION_KEYS)
        self.assertEqual(observation["tier"], "hard")
        self.assertEqual(observation["level"], self.case.level)
        self.assertEqual(set(observation["episode_contract"]), HARD_CONTRACT_KEYS)
        self.assertEqual(observation["episode_contract"]["max_actions"], self.case.budget)
        self.assertEqual(self.info["action_budget"], self.case.budget)
        self.assertEqual(self.env.max_steps, self.case.budget)
        self.assertTrue(observation["mission"].endswith(f"Budget: {self.case.budget} actions."))
        summary = observation["summary"]
        self.assertLessEqual({"cancelled_bookings", "fare_holds_active", "provider_lookups_remaining"}, set(summary))
        self.assertEqual(summary["provider_lookups_remaining"], self.case.lookup_quota)
        self.assertEqual(summary["remaining_actions"], self.case.budget)

    def test_observation_leaks_no_fault_names_or_private_tables(self):
        text = json.dumps(self.observation).lower()
        for word in PRIVATE_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word.lower(), text)
        tools = {tool["name"]: tool for tool in self.observation["available_tools"]}
        self.assertLessEqual({"provider_lookup", "void_booking"}, set(tools))
        self.assertEqual(tools["provider_lookup"]["parameters"]["required"], ["idempotency_key"])
        self.assertEqual(tools["void_booking"]["parameters"]["required"], ["booking_id"])
        self.assertIn("flight_id", tools["invalidate_cache"]["parameters"]["properties"])
        # Hard descriptions state API semantics only; the easy cause-to-fix hints are gone.
        self.assertNotIn("double-charge", tools["patch_config"]["description"])
        self.assertNotIn("must cover", tools["patch_config"]["description"])
        self.assertNotIn("Changing versions can duplicate", tools["reconcile_booking"]["description"])
        contract = self.observation["episode_contract"]
        self.assertIn("void_booking", contract["mutating_tools"])
        self.assertEqual(contract["provider_lookup_quota"], self.case.lookup_quota)
        invariants = contract["graded_invariants"]
        self.assertIsInstance(invariants, dict)
        self.assertLessEqual({"duplicate_charge", "cancelled_request_fulfilled", "duplicate_sale", "unfunded_confirmation",
                              "refund_missing", "refund_unwarranted", "cancelled_booking_backed"}, set(invariants))
        self.assertTrue(all(isinstance(meaning, str) and meaning for meaning in invariants.values()))

    def test_configuration_contracts_mark_new_payment_fields_read_only(self):
        payment = self.observation["configuration_contracts"]["payment"]
        for field in ("lookup_quota", "idempotency_window_steps"):
            self.assertTrue(payment["observed_fields"][field]["readOnly"])
            self.assertNotIn(field, payment["patch_schema"]["properties"])


@unittest.skipUnless(HARD_AVAILABLE, "the hard tier is not importable in this build")
class HardEpisodeRuleTests(unittest.TestCase):
    def hard_env(self, split="train", index=0, seed=1):
        env = LiveAirlineEnv(max_steps=None)
        self.addCleanup(env.close)
        observation, _ = env.reset(seed=seed, options={"split": split, "index": index, "tier": "hard"})
        return env, observation

    def test_private_tables_are_denied_and_logs_need_a_service(self):
        env, _ = self.hard_env()
        for table in ("provider_ledger", "provider_plan", "episode_clock"):
            with self.subTest(table=table):
                observation, *_ = act(env, "query_sql", query=f"SELECT * FROM {table}")
                self.assertFalse(observation["result"]["ok"])
        observation, *_ = act(env, "get_logs")
        self.assertFalse(observation["result"]["ok"])
        self.assertIn("service is required", observation["result"]["error"])
        observation, *_ = act(env, "get_logs", service="payment")
        self.assertTrue(observation["result"]["ok"])

    def test_provider_lookup_quota_is_consumed_by_successful_calls_only(self):
        env, initial = self.hard_env()
        quota = initial["episode_contract"]["provider_lookup_quota"]
        self.assertGreater(quota, 0)
        observation, *_ = act(env, "query_sql", query="SELECT idempotency_key FROM charges ORDER BY rowid LIMIT 1")
        key = observation["result"]["data"][0]["idempotency_key"]
        # An unknown key is a failed call and costs nothing.
        observation, *_ = act(env, "provider_lookup", idempotency_key="no-such-key")
        self.assertFalse(observation["result"]["ok"])
        self.assertEqual(observation["summary"]["provider_lookups_remaining"], quota)
        for used in range(1, quota + 1):
            observation, *_ = act(env, "provider_lookup", idempotency_key=key)
            self.assertTrue(observation["result"]["ok"], observation["result"])
            self.assertEqual(set(observation["result"]["data"]), {"idempotency_key", "state", "booking_id", "amount_cents"})
            self.assertIn(observation["result"]["data"]["state"], {"captured", "declined", "pending"})
            self.assertEqual(observation["summary"]["provider_lookups_remaining"], quota - used)
        observation, *_ = act(env, "provider_lookup", idempotency_key=key)
        self.assertFalse(observation["result"]["ok"])
        self.assertIn("provider lookup quota exhausted", observation["result"]["error"])
        self.assertEqual(observation["summary"]["provider_lookups_remaining"], 0)
        _, _, terminated, _, info = act(env, "finish")
        self.assertTrue(terminated)
        self.assertEqual(info["score"]["details"]["provider_lookups"], quota)

    def test_finishing_before_the_settlement_horizon_scores_zero(self):
        env, _ = self.hard_env()
        observation, reward, terminated, truncated, info = act(env, "finish")
        self.assertTrue(terminated and not truncated)
        score = info["score"]
        self.assertEqual(reward, 0.0)
        self.assertEqual(score["reward"], 0.0)
        self.assertFalse(score["success"])
        details = score["details"]
        self.assertFalse(details["horizon_complete"])
        self.assertEqual(details["tier"], "hard")
        self.assertEqual(details["budget"], env.max_steps)
        self.assertIsInstance(details["settlement_horizon_step"], int)
        self.assertIsInstance(details["trap_outcomes"], dict)
        self.assertLessEqual({"cancelled_voided", "cancelled_completed", "duplicate_voided", "duplicate_completed",
                              "unknown_resolved_by_lookup", "unknown_resolved_by_settlement",
                              "payment_restarted_with_inflight", "fare_hold_broken"}, set(details["trap_outcomes"]))

    def test_retried_requests_are_abandoned_and_reduce_revenue_retained(self):
        env, _ = self.hard_env()
        # Trusted setup, not an agent action: with pricing down, no new request is ever accepted.
        env.stack.stop_service("pricing")
        before = len(env.requirements)
        for _ in range(6):
            act(env, "get_metrics")
        self.assertLess(len(env.requirements), before + 12, "abandoned requests must leave the requirements")
        _, _, _, _, info = act(env, "finish")
        details = info["score"]["details"]
        self.assertGreaterEqual(details["abandoned_requests"], 1)
        self.assertGreaterEqual(details["incident_requests"], details["abandoned_requests"])
        self.assertAlmostEqual(details["revenue_retained"],
                               1 - details["abandoned_requests"] / max(1, details["incident_requests"]), places=9)
        self.assertLess(details["revenue_retained"], 1)

    def test_cost_arithmetic_and_reward_formula(self):
        env, initial = self.hard_env()
        budget = initial["episode_contract"]["max_actions"]
        observation, *_ = act(env, "query_sql", query="SELECT idempotency_key FROM charges ORDER BY rowid LIMIT 1")
        key = observation["result"]["data"][0]["idempotency_key"]
        act(env, "provider_lookup", idempotency_key=key)                  # one provider lookup, priced on its own
        act(env, "patch_config", service="checkin", values={"batch_size": 50})  # one mutation
        act(env, "get_logs")                                              # rejected: not a read
        # reads = successful non-mutating calls other than probe, finish and provider_lookup: the query so far.
        reads = 1
        while env.step_count < budget - 2:
            act(env, "get_metrics")
            reads += 1
        act(env, "probe")                                                 # probes are not reads
        _, reward, _, _, info = act(env, "finish")
        score = info["score"]
        details = score["details"]
        expected_penalty = min(0.25, 0.004 * reads + 0.02 * 1 + 0.03 * 1)
        self.assertAlmostEqual(details["cost_penalty"], expected_penalty, places=9)
        self.assertEqual(details["provider_lookups"], 1)
        self.assertEqual(details["mutations"], 1)
        self.assertTrue(details["horizon_complete"])
        formula = (0.2 * score["availability"] + 0.45 * score["incident_recovery"] + 0.15 * float(score["verified"])
                   + 0.10 * details["revenue_retained"] + 0.10 * float(score["success"]) - details["cost_penalty"])
        expected = 0.0 if not score["integrity"] else max(0.0, formula)
        self.assertAlmostEqual(score["reward"], round(expected, 6), places=6)
        self.assertEqual(reward, score["reward"])

    def test_reward_contract_states_the_real_maximum(self):
        # The weights sum to 1.0, and the last 0.10 is paid only for success.
        from airline_recovery.live.environment import HARD_REWARD_WEIGHTS
        self.assertAlmostEqual(sum(HARD_REWARD_WEIGHTS.values()), 1.0, places=9)
        self.assertAlmostEqual(HARD_REWARD_WEIGHTS["success"], 0.10, places=9)
        _, initial = self.hard_env()
        contract = initial["episode_contract"]
        self.assertNotIn("0.95", contract["reward"])
        self.assertIn("0.10*success", contract["reward"])
        self.assertIn("at most 1.0", contract["reward"])
        horizon = [text for text in contract["success_conditions"] if text.startswith("Horizon complete")]
        self.assertEqual(horizon, ["Horizon complete: at least six actions, every delayed incident injected and "
                                   "no payment still pending at the provider"])

    def test_max_steps_below_the_case_budget_is_rejected(self):
        case = case_for("train", 4, tier="hard", seed=30)
        env = LiveAirlineEnv(max_steps=case.budget - 1)
        self.addCleanup(env.close)
        with self.assertRaisesRegex(ValueError, "below this hard case's budget"):
            env.reset(seed=30, options={"split": "train", "index": 4, "tier": "hard"})
        self.assertIsNone(env.stack)
        # The easy tier and a hard budget at or above the case's are unchanged.
        roomy = LiveAirlineEnv(max_steps=case.budget + 2)
        self.addCleanup(roomy.close)
        observation, _ = roomy.reset(seed=30, options={"split": "train", "index": 4, "tier": "hard"})
        self.assertEqual(observation["episode_contract"]["max_actions"], case.budget + 2)

    def test_scoped_invalidation_of_a_held_flight_breaks_the_fare_hold(self):
        # train:3 seed 4 places a fare hold on one flight (see hard_injector's fare-hold fault).
        env, _ = self.hard_env(index=3, seed=4)
        held = [flight for flight, until in env.trace.held_flights.items() if until >= env.step_count]
        self.assertTrue(held, "this case is expected to hold a fare")
        other = next(flight for flight in ("F100", "F200") if flight not in held)
        act(env, "invalidate_cache", service="pricing", flight_id=other)
        self.assertFalse(env.trace.fare_hold_broken)
        observation, *_ = act(env, "invalidate_cache", service="pricing", flight_id=held[0])
        self.assertTrue(observation["result"]["ok"], observation["result"])
        self.assertTrue(env.trace.fare_hold_broken)


if __name__ == "__main__":
    unittest.main()
