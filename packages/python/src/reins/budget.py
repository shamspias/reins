"""Budgets (M1.4): hard caps that stop a run deterministically (invariant §2.13).

M1.4 implements turn and token caps — enough to kill a runaway loop. The time and
cost meter arrives in M3.3.
"""

from __future__ import annotations

from dataclasses import dataclass

from reins.types import Usage


@dataclass(frozen=True, slots=True)
class Budget:
    """Caps for one run.

    ``max_turns`` bounds the number of model calls (each loop iteration calls the model
    once), *including* the final-answer turn — so a goal that needs k rounds of tool
    calls plus an answer needs ``max_turns >= k + 1``. The loop always makes at least
    one model call, so ``max_turns=0`` does not mean "do nothing".
    ``max_tokens`` bounds total token spend (None = uncapped).
    """

    max_turns: int = 8
    max_tokens: int | None = None

    def over_tokens(self, usage: Usage) -> bool:
        if self.max_tokens is None:
            return False
        return usage.input_tokens + usage.output_tokens > self.max_tokens
