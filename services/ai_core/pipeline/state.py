"""Explicit pipeline state machine."""

from __future__ import annotations

from shared.schemas.telemetry import PipelineState


_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    PipelineState.IDLE: frozenset(
        {PipelineState.RUNNING, PipelineState.RECOVERING, PipelineState.FAILED},
    ),
    PipelineState.RUNNING: frozenset(
        {PipelineState.PAUSED, PipelineState.RECOVERING, PipelineState.RECONFIGURING, PipelineState.FAILED, PipelineState.IDLE},
    ),
    PipelineState.PAUSED: frozenset(
        {PipelineState.RUNNING, PipelineState.RECOVERING, PipelineState.FAILED, PipelineState.IDLE},
    ),
    PipelineState.RECOVERING: frozenset(
        {PipelineState.RUNNING, PipelineState.FAILED, PipelineState.PAUSED, PipelineState.IDLE},
    ),
    PipelineState.RECONFIGURING: frozenset(
        {PipelineState.RUNNING, PipelineState.FAILED, PipelineState.PAUSED, PipelineState.IDLE},
    ),
    PipelineState.FAILED: frozenset(
        {PipelineState.RECOVERING, PipelineState.IDLE, PipelineState.RUNNING},
    ),
}


class PipelineController:
    def __init__(self, initial: PipelineState = PipelineState.IDLE) -> None:
        self._state = initial

    @property
    def state(self) -> PipelineState:
        return self._state

    def transition(self, new: PipelineState) -> bool:
        allowed = _TRANSITIONS.get(self._state, frozenset())
        if new not in allowed and new != self._state:
            return False
        self._state = new
        return True

    def force(self, new: PipelineState) -> None:
        self._state = new
