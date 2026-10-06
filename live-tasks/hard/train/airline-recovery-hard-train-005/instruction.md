Recover a synthetic airline booking-to-checkin system running in separate HTTP worker processes.
Run `python /app/control.py` for the current observation, the tool schemas, the configuration contracts and the episode contract.
Run `python /app/control.py action '{"tool":"get_metrics","arguments":{}}'` to act.
Every tool action advances customer traffic. Budget: 34 actions.
Alerts, logs and metrics are best-effort and may be stale or wrong; durable business rows and the payment provider are authoritative.
There is one episode and its actions cannot be undone: the world runs in an isolated sidecar and is the only thing graded.
When you call finish (or exhaust the budget) the sidecar issues a signed receipt, which the CLI stores in /app/episode.json. An episode without a receipt scores 0.
Reward measures the outcome of every customer request, the integrity rules in the episode contract and the cost of your actions; correct configuration names or a written explanation do not earn credit.
This benchmark executes local HTTP and SQLite transactions; it does not connect to real airlines, payments or cloud infrastructure.
