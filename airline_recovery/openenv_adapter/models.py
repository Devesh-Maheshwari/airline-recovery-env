"""Public, validated OpenEnv wire models. No scenario internals are serialized."""

from typing import Any, Literal

from openenv.core.env_server.types import Action, Observation, State
from pydantic import BaseModel, ConfigDict, Field


ToolName = Literal[
    "get_metrics", "get_logs", "get_config", "patch_config", "restart_service",
    "query_sql", "replay_events", "quarantine_event", "invalidate_cache",
    "reconcile_booking", "provider_lookup", "void_booking", "probe", "finish",
]
Tier = Literal["easy", "hard"]


class AirlineAction(Action):
    tool: ToolName = Field(description="Operational tool to execute.")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments; discover each tool's schema in available_tools after reset.",
    )


class AirlineObservation(Observation):
    episode_id: str
    step: int = Field(ge=0)
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    available_tools: list[dict[str, Any]] = Field(default_factory=list)
    configuration_contracts: dict[str, Any] = Field(default_factory=dict)
    episode_contract: dict[str, Any] = Field(default_factory=dict)
    mission: str | None = None
    # Present on hard-tier reset observations only.
    tier: Tier | None = None
    level: int | None = Field(default=None, ge=1)
    reward: float = 0.0
    terminated: bool = False
    truncated: bool = False


class AirlineState(State):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    done: bool = False


class ResetOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    split: Literal["train", "eval", "test"] = "train"
    index: int = Field(default=0, ge=0)
    tier: Tier = "easy"

    def core_options(self) -> dict[str, Any]:
        """Options for the core environment; the easy tier sends what it always sent."""
        options: dict[str, Any] = {"split": self.split, "index": self.index}
        if self.tier != "easy":
            options["tier"] = self.tier
        return options


class SeedValue(BaseModel):
    model_config = ConfigDict(strict=True)
    seed: int = Field(default=0, ge=0)
