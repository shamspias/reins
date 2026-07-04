"""The cost meter (M3.3): turn token usage into money, deterministically.

A :class:`Pricing` maps a model name to its per-million-token input/output
rates (USD). The loop applies it to each model response and folds the cost into
the run's cumulative :class:`reins.types.Usage`, so ``Budget.max_cost`` can stop
a run before it gets expensive.

Prices change and vary by provider, so this never *guesses*: an unknown model
costs 0.0 (the meter reports honestly rather than inventing a number). Supply
your own rates for models not in :data:`DEFAULT_PRICING`, or rely on an adapter
that reports ``Usage.cost`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reins.types import Usage


@dataclass(frozen=True, slots=True)
class Pricing:
    """Per-model (input, output) USD rates per 1,000,000 tokens."""

    rates: dict[str, tuple[float, float]] = field(default_factory=dict)

    def cost_of(self, model: str, usage: Usage) -> float:
        """The USD cost of one response's token usage, or 0.0 for an unknown model."""
        rate = self.rates.get(model)
        if rate is None:
            return 0.0
        input_rate, output_rate = rate
        return (usage.input_tokens * input_rate + usage.output_tokens * output_rate) / 1_000_000


# A small, explicit table for the default models (USD per 1M tokens). Deliberately
# not exhaustive — callers pass their own rates for anything else.
DEFAULT_PRICING = Pricing(
    rates={
        "claude-opus-4-8": (15.0, 75.0),
        "gpt-5.1": (5.0, 15.0),
        "gemini-2.5-flash": (0.30, 2.50),
        "llama-3.3-70b-versatile": (0.59, 0.79),
        "glm-4.6": (0.60, 2.20),
    }
)
