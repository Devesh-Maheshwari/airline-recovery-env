"""Business correctness regressions against executed HTTP workers."""
import json
import unittest

from airline_recovery.live.environment import LiveAirlineEnv, task_manifest


def act(env, tool, **arguments):
    return env.step({"tool":tool,"arguments":arguments})


class LiveEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.env = LiveAirlineEnv()

    def tearDown(self):
        self.env.close()

    def repair_deadline(self):
        provider = self.env.stack.get_config("payment")["provider_latency_ms"]
        act(self.env,"patch_config",service="booking",values={"payment_timeout_ms":provider+100})
        pending = self.env.stack.query("SELECT booking_id FROM bookings WHERE status='pending'")
        for row in pending:
            act(self.env,"reconcile_booking",booking_id=row["booking_id"])
        while self.env.step_count < 6:
            act(self.env,"get_metrics")
        act(self.env,"probe")
        act(self.env,"probe")

    def test_commit_before_timeout_is_real_and_recovery_preserves_single_charge(self):
        self.env.reset(seed=19,options={"split":"train","index":0})
        rows = self.env.stack.inspect()
        pending = {r["booking_id"] for r in rows["bookings"] if r["status"] == "pending"}
        self.assertTrue(pending)
        self.assertTrue(any(c["booking_id"] in pending for c in rows["charges"]))
        self.repair_deadline()
        _,reward,done,_,info = act(self.env,"finish")
        self.assertTrue(done)
        self.assertEqual(reward,1.0)
        self.assertTrue(info["score"]["integrity"])
        for row in self.env.stack.query("SELECT booking_id,COUNT(*) AS n FROM charges GROUP BY booking_id"):
            self.assertEqual(row["n"],1)

    def test_restoring_old_default_timeout_does_not_solve_changed_dependency(self):
        self.env.reset(seed=5,options={"split":"train","index":0})
        act(self.env,"patch_config",service="booking",values={"payment_timeout_ms":200})
        for _ in range(6):
            act(self.env,"probe")
        _,reward,_,_,info = act(self.env,"finish")
        self.assertFalse(info["score"]["success"])
        self.assertLess(reward,1)

    def test_distinct_working_timeouts_are_accepted_by_outcome_grader(self):
        for timeout in (900,1400):
            with self.subTest(timeout=timeout):
                self.env.reset(seed=7,options={"split":"train","index":0})
                act(self.env,"patch_config",service="booking",values={"payment_timeout_ms":timeout})
                for row in self.env.stack.query("SELECT booking_id FROM bookings WHERE status='pending'"):
                    act(self.env,"reconcile_booking",booking_id=row["booking_id"])
                while self.env.step_count < 6:
                    act(self.env,"get_metrics")
                act(self.env,"probe"); act(self.env,"probe")
                self.assertEqual(act(self.env,"finish")[1],1.0)

    def test_disabled_idempotency_causes_actual_duplicate_charges_and_zero_score(self):
        self.env.reset(seed=9,options={"split":"train","index":0})
        act(self.env,"patch_config",service="payment",values={"idempotency_enabled":False})
        duplicates = self.env.stack.query("SELECT booking_id,COUNT(*) AS n FROM charges GROUP BY booking_id HAVING COUNT(*)>1")
        self.assertTrue(duplicates)
        self.repair_deadline()
        _,reward,_,_,info = act(self.env,"finish")
        self.assertEqual(reward,0)
        self.assertFalse(info["score"]["integrity"])

    def test_price_validation_bypass_fails_on_undercharged_customer_records(self):
        self.env.reset(seed=2,options={"split":"train","index":2})
        act(self.env,"patch_config",service="booking",values={"validate_price":False})
        self.assertTrue(any(v.startswith("accepted_request_changed:") for v in self.env.violations_seen))
        for _ in range(5):
            act(self.env,"probe")
        self.assertEqual(act(self.env,"finish")[1],0)

    def test_discarding_valid_schema_migration_event_fails_integrity(self):
        self.env.reset(seed=2,options={"split":"eval","index":1})
        events = self.env.stack.query("SELECT event_id,payload FROM outbox WHERE status='pending'")
        target = next(r for r in events if json.loads(r["payload"]).get("schema_version") == 2)
        act(self.env,"quarantine_event",event_id=target["event_id"])
        self.assertIn("valid_event_discarded:"+target["event_id"],self.env.violations_seen)

    def test_readonly_query_cannot_delete_booking_evidence(self):
        self.env.reset(seed=2,options={"split":"train","index":1})
        count = len(self.env.stack.inspect()["bookings"])
        obs,*_ = act(self.env,"query_sql",query="DELETE FROM bookings")
        self.assertFalse(obs["result"]["ok"])
        self.assertGreaterEqual(len(self.env.stack.inspect()["bookings"]),count)

    def test_delayed_incident_and_no_reference_answer_in_reset(self):
        obs,info = self.env.reset(seed=3,options={"split":"test","index":0})
        for name in ("fault","scenario","seed","baseline","repair_config"):
            self.assertNotIn(name,obs)
            self.assertNotIn(name,info)
        for _ in range(3):
            act(self.env,"get_logs")
        self.assertTrue(self.env.delayed_injected)
        self.assertTrue(any(r["payload"].endswith('"booking_id":') for r in self.env.stack.inspect()["outbox"]))

    def test_entity_ids_are_fresh_every_episode_and_carry_no_fault_label(self):
        identities = []
        for seed in (3, 3, 4):
            self.env.reset(seed=seed, options={"split":"eval", "index":0})
            state = self.env.stack.inspect()
            identities.append({table: {r[key] for r in state[table]} for table, key in
                               (("bookings", "booking_id"), ("outbox", "event_id"))})
            self.assertTrue(all("poison" not in row["event_id"] for row in state["outbox"]))
            self.assertTrue(all(row["request_id"].startswith("req_") for row in state["bookings"]))
        for table in identities[0]:
            self.assertTrue(identities[0][table].isdisjoint(identities[1][table]))
            self.assertTrue(identities[0][table].isdisjoint(identities[2][table]))

    def test_operator_cannot_rewrite_external_provider_conditions(self):
        self.env.reset(seed=3, options={"split":"train", "index":0})
        before = self.env.stack.get_config("payment")
        observation, *_ = act(self.env, "patch_config", service="payment",
                             values={"provider_latency_ms":0, "idempotency_enabled":False})
        self.assertFalse(observation["result"]["ok"])
        self.assertEqual(self.env.stack.get_config("payment"), before)
        self.assertGreater(self.env.stack.get_config("payment")["provider_latency_ms"], 0)

    def test_reset_removes_previous_records_and_allocates_new_episode(self):
        first,_ = self.env.reset(seed=3,options={"split":"train","index":1})
        old_stack = self.env.stack
        for _ in range(3): act(self.env,"get_metrics")
        second,_ = self.env.reset(seed=3,options={"split":"train","index":1})
        self.assertNotEqual(first["episode_id"],second["episode_id"])
        self.assertIsNot(self.env.stack,old_stack)
        self.assertEqual(first["summary"],second["summary"])

    def test_finish_before_evaluation_horizon_cannot_pass(self):
        self.env.reset(seed=3,options={"split":"train","index":1})
        _,reward,done,truncated,info=act(self.env,"finish")
        self.assertTrue(done)
        self.assertFalse(truncated)
        self.assertEqual(reward,0)
        with self.assertRaises(RuntimeError): act(self.env,"probe")


if __name__ == "__main__":
    unittest.main()
