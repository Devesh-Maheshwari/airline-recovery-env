# Contributing to Airline Recovery Env

The useful unit of contribution is a failure that changes actual transaction
behavior, with evidence that recovery preserves the business records. Start by
running `python -m unittest discover -s tests -v` and the reference policy.

For a new incident:

1. Reproduce a failure against the executed service code in `airline_recovery/live/worker.py`.
   Start with `tests/test_live_environment.py`: a healthy transaction, the failed
   transaction, a legitimate recovery, and a plausible unsafe recovery must have
   different observable outcomes.
2. Add the trusted injection in `LiveAirlineEnv._inject` in
   `airline_recovery/live/environment.py` and a case in `airline_recovery/live/scenarios.py`. Keep fault names and repair
   recipes out of agent observations and task IDs.
3. Add tests showing an untouched incident fails, a valid repair works, and at
   least one plausible unsafe shortcut is caught through resulting records.
4. Test a second valid solution when the service semantics permit one. Reward
   checks belong in `verification.py` and must not compare the agent's settings
   against an oracle configuration.
5. Run multiple seeds and retain failed traces. Document exactly what is executed
   and what remains an approximation.
6. Compare against `examples.blanket_baseline:agent`, which fails the
   payment-migration cases by charging customers twice and solves most of the
   remaining cases. Explain any new behavior that requires choosing a different
   repair; do not add arbitrary penalties just to make that baseline fail.

High-value next contributions include another independently authored incident
family, actual PostgreSQL/Redis backends with parity tests, an external model
agent integration, and a trainer adapter with a measured before/after experiment.
Changes that only rename services, add metric noise, or multiply seed counts do
not by themselves add a new benchmark capability.

Policies use the observation-to-action contract in `examples/custom_agent.py`.
For third-party or untrusted policies, connect through the native OpenEnv server
in a separate container/process; an in-process Python plugin is a trusted local
development convenience, not a security sandbox.

Please include the command, Python version, seed, task split/index and failing
trace with bug reports. Do not upload credentials or real customer records.
