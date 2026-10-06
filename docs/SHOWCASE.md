# The `airline-recovery showcase` comparison

`airline-recovery showcase` runs four scripted recovery strategies against the same synthetic
payment-migration incident and compares what each one does to customer records.
It shows the failure this environment is built to catch: services can look
healthy again while a customer has been charged twice. The environment detects
that harm from the executed payment records and rejects the recovery.

## Run it

From the repository root:

```bash
python -m airline_recovery showcase --seed 42 --output runs/showcase
```

The output directory must be new or empty, so use a different `--output` for
each run. `--delay` sets the pause between steps in seconds (default 0.15; use
`--delay 0` for an immediate result). Python 3.11 or later and permission to open
local HTTP sockets are required. No model, GPU, credentials, database server, or
Docker is needed.

## The incident

The showcase uses the payment-migration training task (`train`, index 4). Several
bookings are left pending after their payment acknowledgments were lost. Every
one of them has the same fare, and at seed 42 every one already has a committed
charge. The charges were made under two different versions of the payment retry
key, because the key format changed during a migration, and the deployed service
now uses only one of them.

A correct recovery completes each booking without capturing a second payment.
The booking status alone does not reveal which key a charge was made with; that
has to be read from the payment records.

## The strategies

Each strategy starts from a fresh reset of the same task and seed, with its own
set of five local HTTP service workers. Entity identifiers are unique to each
episode, so the booking IDs differ between runs while the incident is the same.

| Strategy | What it does | Observed result at seed 42 |
|---|---|---|
| `blanket` | Restores services, then retries every pending booking with the currently deployed key | Not solved: two bookings charged twice, $400.00 excess capture, reward 0 |
| `reference` | Reads the payment evidence and retries each booking with the key its existing charge was made under | Solved: no duplicate charge, reward 1 |
| `reference-adopt` | Identifies each existing charge and asks the service to validate and adopt it, with no further capture | Solved: no duplicate charge, reward 1 |
| `source-aware` | Reconstructs charge IDs from the public source code, attempts adoption, then retries each booking | Solved: no duplicate charge, reward 1 |

The blanket strategy fails because a retry under the wrong key is a new payment
as far as the payment service is concerned. Fresh requests succeed afterwards,
but the second durable charge remains and the recovery is scored as unsafe. The
three safe strategies reach the same end state by different routes and receive
the same reward.

All amounts are synthetic. The records store cents; the closing table prints
them as dollars.

## What you see

For each strategy the command prints the reconciliation steps it took and a
one-line result. It ends with a comparison table: strategy, outcome, reward,
steps, duplicate-charged bookings, and excess capture.

The output directory receives two files:

- `summary.json` holds the seed, the task, source hashes, and for each strategy
  the initial pending bookings, the final state of those bookings, and the full
  score. It also records whether the runs started from matching incidents and
  whether the expected contrast was observed.
- `trajectories.jsonl` holds every observation, action, and reward, one step per
  line.

The command exits with an error if the expected contrast does not appear, that
is, if the blanket strategy does not produce a duplicate charge or one of the
other strategies does not solve the task.

## What it does not show

These are scripted policies on one synthetic case. The showcase does not measure
model performance, improvement from training, or production savings.

The `source-aware` strategy is a control. It solves the task without reading the
payment ledger, by inferring charge identifiers from the published source. Its
success means this case does not by itself prove that careful diagnosis is
required, and any comparison built on the showcase should include it.
