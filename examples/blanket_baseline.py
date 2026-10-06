"""Adversarial baseline: blanket repairs plus public lookup of required IDs.

This is intentionally not diagnostic reasoning. It applies the same five broad
repairs to every episode, then uses one SQL result to identify pending bookings
and malformed JSON events. It never reads the task, seed, logs, metrics, or
configuration. Its limitations should accompany every reported result.

Run from the repository root:
  python -m airline_recovery.live.evaluate --policy examples.blanket_baseline:agent \
      --split all --seeds 1,2,3 --output runs/blanket-baseline
"""
from collections import deque
from copy import deepcopy


def action(tool, **arguments):
    return {"tool": tool, "arguments": arguments}


LOOKUP = (
    "SELECT 'booking' AS kind, booking_id AS id FROM bookings WHERE status='pending' "
    "UNION ALL SELECT 'event' AS kind, event_id AS id FROM outbox "
    "WHERE status='pending' AND NOT json_valid(payload) ORDER BY kind,id"
)


class BlanketWithLookup:
    def __init__(self):
        self.reset()

    def reset(self):
        self.queue = deque([
            action("patch_config", service="booking", values={"payment_timeout_ms": 10000}),
            action("restart_service", service="pricing"),
            action("restart_service", service="payment"),
            action("invalidate_cache", service="pricing"),
            action("patch_config", service="checkin", values={"consumer_enabled": True,
                   "accepted_schema": 2, "batch_size": 50}),
            action("query_sql", query=LOOKUP),
        ])
        self.looked_up = False

    def __call__(self, observation):
        if not self.queue and not self.looked_up:
            result = observation["result"]
            if not result["ok"] or not isinstance(result["data"], list):
                raise RuntimeError("The public ID lookup did not return rows")
            for row in result["data"]:
                if row["kind"] == "booking":
                    self.queue.append(action("reconcile_booking", booking_id=row["id"]))
                elif row["kind"] == "event":
                    self.queue.append(action("quarantine_event", event_id=row["id"]))
                else:
                    raise RuntimeError("Unexpected public lookup row")
            self.queue.extend([action("probe"), action("probe"), action("finish")])
            self.looked_up = True
        return deepcopy(self.queue.popleft())


agent = BlanketWithLookup()


if __name__ == "__main__":
    print("This module is a policy plugin. Evaluate it from the repository root with:\n"
          "  python -m airline_recovery.live.evaluate --policy examples.blanket_baseline:agent "
          "--split all --seeds 1,2,3 --output runs/blanket-baseline")
