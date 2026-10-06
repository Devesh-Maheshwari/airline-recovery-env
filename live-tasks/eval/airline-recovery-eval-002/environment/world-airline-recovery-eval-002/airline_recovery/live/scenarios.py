"""Trusted incident specifications. Not sent over the agent interface."""
from dataclasses import dataclass

from . import hardcases


@dataclass(frozen=True)
class Case:
    split: str
    index: int
    family: str
    initial: tuple[str, ...]
    delayed: tuple[str, ...] = ()


CASES = (
    Case("train", 0, "lost-payment-ack", ("deadline",)),
    Case("train", 1, "paused-projection", ("paused",)),
    Case("train", 2, "stale-fare", ("cache",)),
    Case("train", 3, "process-unavailable", ("pricing-down",)),
    Case("eval", 0, "poison-event", ("poison",)),
    Case("eval", 1, "schema-migration", ("schema",)),
    Case("test", 0, "deadline-then-poison", ("deadline",), ("poison",)),
    Case("test", 1, "payment-outage-then-cutover", ("payment-down",), ("cache",)),
    Case("train", 4, "mixed-payment-keys", ("payment-migration",)),
    Case("eval", 2, "payment-migration-then-fare-change", ("payment-migration",), ("cache",)),
    Case("test", 2, "payment-migration-then-schema-change", ("payment-migration",), ("schema",)),
)
TIERS = ("easy", "hard")


def case_for(split, index, tier="easy", seed=0):
    if tier not in TIERS:
        raise ValueError("tier must be easy or hard")
    if type(index) is not int:
        raise ValueError("Task index must be an integer")
    if tier == "hard":
        # The slot fixes the level; the seed is a structural sample within it.
        return hardcases.generate_case(hardcases.level_for(split, index), index, seed, split=split)
    for case in CASES:
        if case.split == split and case.index == index:
            return case
    raise ValueError(f"No task at split={split!r}, index={index!r}")


def task_manifest(tier="easy"):
    if tier not in TIERS:
        raise ValueError("tier must be easy or hard")
    if tier == "hard":
        return {split: [{"id": f"airline-recovery-hard-{split}-{index:03d}", "index": index, "level": level,
                         "description": f"Hard tier level {level}: resolve every customer request correctly under ambiguous payment truth, traps and a step budget."}
                        for index, level in enumerate(levels)] for split, levels in hardcases.LEVEL_BY_SLOT.items()}
    return {split: [{"id": f"airline-recovery-{split}-{c.index:03d}", "index": c.index,
                     "description": "Restore booking and check-in workflows while preserving all accepted transactions."}
                    for c in CASES if c.split == split] for split in ("train", "eval", "test")}
