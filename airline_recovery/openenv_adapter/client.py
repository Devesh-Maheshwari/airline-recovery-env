"""The actual OpenEnv WebSocket client, with Airline Recovery Env's typed action/result models."""

from typing import Any

from openenv.core.client_types import StepResult
from openenv.core.env_client import EnvClient

from .models import AirlineAction, AirlineObservation, AirlineState


class AirlineEnv(EnvClient[AirlineAction, AirlineObservation, AirlineState]):
    def _step_payload(self, action: AirlineAction) -> dict[str, Any]:
        return action.model_dump()

    def _parse_result(self, payload: dict[str, Any]) -> StepResult[AirlineObservation]:
        # OpenEnv transports reward/done outside the nested observation. Rejoin
        # them so result.done and result.observation.done always agree.
        metadata = payload.get("metadata") or payload["observation"].get("metadata", {})
        data = {
            **payload["observation"],
            "reward": payload.get("reward") or 0.0,
            "done": payload.get("done", False),
            "metadata": metadata,
        }
        observation = AirlineObservation.model_validate(data)
        return StepResult(
            observation=observation, reward=observation.reward,
            done=observation.done, metadata=metadata,
        )

    def _parse_state(self, payload: dict[str, Any]) -> AirlineState:
        return AirlineState.model_validate(payload)
