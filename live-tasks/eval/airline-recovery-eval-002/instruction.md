Recover a synthetic airline booking-to-checkin system running in separate HTTP worker processes.
Preserve accepted bookings, exact single charges, seat capacity, accepted fares, and valid events.
Run `python /app/control.py` for the current observation and full tool schemas.
Run `python /app/control.py action '{"tool":"get_metrics","arguments":{}}'` to act.
Inspect logs/config/SQL, repair underlying problems, reconcile accepted transactions and verify two healthy probe windows before finish.
Every tool action advances customer traffic; another incident can arrive while you work. Budget: 48 actions.
There is one episode and its actions cannot be undone: the world runs in an isolated sidecar and is the only thing graded.
When you call finish (or exhaust the budget) the sidecar issues a signed receipt, which the CLI stores in /app/episode.json. An episode without a receipt scores 0.
Reward measures actual completed requests and integrity; correct configuration names or a written explanation do not earn credit.
This benchmark executes local HTTP and SQLite transactions; it does not connect to real airlines, payments or cloud infrastructure.
