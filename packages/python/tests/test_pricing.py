"""Tests for the cost meter (M3.3): token usage → USD, honest about unknowns."""

from reins.pricing import DEFAULT_PRICING, Pricing
from reins.types import Usage


def test_known_model_cost_matches_rates() -> None:
    pricing = Pricing(rates={"m": (10.0, 30.0)})  # per 1M tokens
    cost = pricing.cost_of("m", Usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert cost == 40.0


def test_partial_usage_is_prorated() -> None:
    pricing = Pricing(rates={"m": (10.0, 30.0)})
    cost = pricing.cost_of("m", Usage(input_tokens=500_000, output_tokens=100_000))
    assert cost == 5.0 + 3.0


def test_unknown_model_costs_nothing_rather_than_guessing() -> None:
    assert Pricing(rates={"m": (10.0, 30.0)}).cost_of("other", Usage(input_tokens=999)) == 0.0
    assert Pricing().cost_of("anything", Usage(input_tokens=999)) == 0.0


def test_default_pricing_covers_the_default_models() -> None:
    for model in ("claude-opus-4-8", "gpt-5.1", "gemini-2.5-flash"):
        assert DEFAULT_PRICING.cost_of(model, Usage(input_tokens=1000, output_tokens=1000)) > 0
