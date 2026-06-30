"""Stop conditions (M1.4): deterministic, code-owned reasons the loop halts.

A runaway loop is a defect, not an edge case (invariant §2.13). The loop evaluates
these after each Sense phase and stops when any fires — "the model said it's done"
(GoalReached) is just one condition among several.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from reins.types import FinishReason, RunState


@dataclass(frozen=True, slots=True)
class StopDecision:
    stop: bool
    reason: str


@runtime_checkable
class StopCondition(Protocol):
    def evaluate(self, state: RunState) -> StopDecision: ...


@dataclass(frozen=True, slots=True)
class GoalReached:
    """Stops when the model's last turn was a final answer (no tool calls)."""

    def evaluate(self, state: RunState) -> StopDecision:
        resp = state.last_response
        done = resp is not None and resp.finish_reason != FinishReason.TOOL_CALLS
        return StopDecision(stop=done, reason="goal_reached")


@dataclass(frozen=True, slots=True)
class MaxTurns:
    """Stops once the loop has taken ``limit`` turns."""

    limit: int

    def evaluate(self, state: RunState) -> StopDecision:
        return StopDecision(stop=state.turn >= self.limit, reason="max_turns")


def first_stop(conditions: list[StopCondition], state: RunState) -> StopDecision | None:
    """Return the first firing stop decision, or None if none fire."""
    for condition in conditions:
        decision = condition.evaluate(state)
        if decision.stop:
            return decision
    return None
