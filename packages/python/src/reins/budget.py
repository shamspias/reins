"""Budgets (M1.4, extended M3.3): caps that stop a run deterministically (§2.13).

A runaway loop is a bug, not an edge case. ``max_turns`` and ``max_tokens`` are
the original hard stops; M3.3 adds a wall-clock ``max_seconds`` and a
``max_cost`` (USD, via the cost meter), plus ``compact_at`` — a *soft* limit that
triggers history compaction rather than stopping the run.
"""

from __future__ import annotations

from dataclasses import dataclass

from reins.conversation import DEFAULT_COMPACT_AT
from reins.types import Usage


@dataclass(frozen=True, slots=True)
class Budget:
    """Caps for one run.

    ``max_turns`` bounds the number of model calls (each loop iteration calls the
    model once), *including* the final-answer turn — so a goal that needs k rounds
    of tool calls plus an answer needs ``max_turns >= k + 1``. The loop always
    makes at least one model call, so ``max_turns=0`` does not mean "do nothing".
    ``max_tokens`` / ``max_cost`` / ``max_seconds`` bound total token spend, USD
    cost, and wall-clock time (``None`` = uncapped). ``compact_at`` is a soft
    limit: when the conversation's estimated token size exceeds it, history is
    compacted (the goal is always preserved) — set ``None`` to disable.
    """

    max_turns: int = 8
    max_tokens: int | None = None
    max_cost: float | None = None
    max_seconds: float | None = None
    compact_at: int | None = DEFAULT_COMPACT_AT

    def over_tokens(self, usage: Usage) -> bool:
        if self.max_tokens is None:
            return False
        return usage.input_tokens + usage.output_tokens > self.max_tokens

    def over_cost(self, usage: Usage) -> bool:
        if self.max_cost is None:
            return False
        return usage.cost > self.max_cost

    def over_time(self, elapsed_seconds: float) -> bool:
        if self.max_seconds is None:
            return False
        return elapsed_seconds > self.max_seconds
