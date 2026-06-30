"""Tests for the harness loop, stop conditions, budget, and the Agent (M1.4)."""

import pytest

from reins.budget import Budget
from reins.capability import BoundCapability, capability
from reins.errors import ModelError
from reins.loop import Agent, run_loop
from reins.model import FakeModel
from reins.registry import CapabilityRegistry
from reins.stop import GoalReached, MaxTurns
from reins.types import FinishReason, Message, ModelResponse, Role, RunState, Usage


@capability
def find_orders(status: str = "open") -> list[str]:
    "Find orders by status."
    return ["o1", "o2"]


@capability
def delete_order(order_id: int) -> None:
    "Delete an order."


def _registry(*caps: BoundCapability) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for cap in caps:
        reg.register(cap)
    return reg


def test_read_goal_completes() -> None:
    model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "found 2"}]
    )
    report = run_loop(model, _registry(find_orders), "list open orders", can_write=False)
    assert report.outcome == "completed"
    assert report.executed == ("find_orders",)


def test_ask_refuses_a_write() -> None:
    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])
    report = run_loop(
        model, _registry(find_orders, delete_order), "delete order 1", can_write=False
    )
    assert report.outcome == "refused"
    assert report.reason == "write_in_read_only"
    assert report.executed == ()


def test_budget_stops_a_looping_model() -> None:
    script = [{"call": "find_orders", "args": {}} for _ in range(5)]
    model = FakeModel.from_script(script)
    report = run_loop(
        model, _registry(find_orders), "loop forever", can_write=False, budget=Budget(max_turns=2)
    )
    assert report.outcome == "stopped"
    assert report.reason == "budget_exhausted"


def test_max_turns_counts_the_final_answer_turn() -> None:
    # A 1-tool-call goal + its answer needs max_turns >= 2; at 1 it fail-stops on budget.
    model = FakeModel.from_script([{"call": "find_orders", "args": {}}, {"final": "done"}])
    report = run_loop(
        model, _registry(find_orders), "list", can_write=False, budget=Budget(max_turns=1)
    )
    assert report.outcome == "stopped"
    assert report.reason == "budget_exhausted"


def test_run_write_not_executed_without_approval() -> None:
    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])
    report = run_loop(model, _registry(delete_order), "delete order 1", can_write=True)
    assert report.outcome == "not_executed"  # write gating is M2.x


def test_agent_ask_returns_run_result() -> None:
    agent = Agent(model=FakeModel.from_script([{"final": "hello"}]), capabilities=[find_orders])
    result = agent.ask("say hi")
    assert isinstance(result.output, Message)
    assert result.output.text == "hello"


def test_agent_without_model_or_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = Agent(capabilities=[find_orders])
    with pytest.raises(ModelError, match="→"):
        agent.ask("anything")


def test_goal_reached_stop_condition() -> None:
    resp = ModelResponse(
        message=Message(role=Role.ASSISTANT, text="done"),
        finish_reason=FinishReason.STOP,
        usage=Usage(),
    )
    assert GoalReached().evaluate(RunState(turn=1, last_response=resp)).stop is True


def test_max_turns_stop_condition() -> None:
    assert MaxTurns(3).evaluate(RunState(turn=3)).stop is True
    assert MaxTurns(4).evaluate(RunState(turn=3)).stop is False


def test_budget_over_tokens() -> None:
    assert Budget(max_tokens=10).over_tokens(Usage(input_tokens=8, output_tokens=5)) is True
    assert Budget(max_tokens=100).over_tokens(Usage(input_tokens=8, output_tokens=5)) is False
    assert Budget().over_tokens(Usage(input_tokens=10_000)) is False  # no token cap
