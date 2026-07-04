"""Loop integration for context discipline (M3.3): the cost & time budgets, the
injected clock, and history compaction firing during a long run."""

from reins.budget import Budget
from reins.capability import BoundCapability, capability
from reins.loop import run_loop
from reins.model import FakeModel, ModelParams
from reins.pricing import Pricing
from reins.registry import CapabilityRegistry
from reins.types import FinishReason, Message, ModelResponse, Role, ToolCall, Usage


@capability(reads=True)
def find_orders(status: str = "open") -> list[str]:
    "Find orders by status."
    return ["o1", "o2"]


def _registry(*caps: BoundCapability) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for cap in caps:
        reg.register(cap)
    return reg


class FakeClock:
    """A clock the test advances by hand — no wall-clock dependence."""

    def __init__(self, times: list[float]) -> None:
        self._times = times
        self._i = 0

    def now(self) -> float:
        t = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return t


def _usage_response(text: str, input_tokens: int, output_tokens: int) -> ModelResponse:
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, text=text),
        finish_reason=FinishReason.STOP,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _usage_call(input_tokens: int, output_tokens: int) -> ModelResponse:
    """A tool-call turn (keeps the loop going) that also spends tokens."""
    return ModelResponse(
        message=Message(
            role=Role.ASSISTANT, tool_calls=[ToolCall(id="c", name="find_orders", arguments={})]
        ),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# --- cost budget + the meter ------------------------------------------------------


def test_cost_meter_accumulates_and_matches_usage() -> None:
    model = FakeModel(
        responses=[
            _usage_response("done", input_tokens=1_000_000, output_tokens=1_000_000),
        ]
    )
    pricing = Pricing(rates={"m": (10.0, 30.0)})
    report = run_loop(
        model,
        _registry(find_orders),
        "hello",
        can_write=False,
        pricing=pricing,
        params=ModelParams(model="m"),
    )
    assert report.outcome == "completed"
    assert report.usage.cost == 40.0  # meter matches the priced usage


def test_cost_budget_stops_before_it_gets_expensive() -> None:
    # Each tool-call turn costs 40 USD; max_cost=50 stops at the guard once the
    # cumulative cost (80 after two turns) exceeds it.
    model = FakeModel(
        responses=[
            _usage_call(1_000_000, 1_000_000),
            _usage_call(1_000_000, 1_000_000),
            _usage_response("done", 1_000_000, 1_000_000),
        ]
    )
    report = run_loop(
        model,
        _registry(find_orders),
        "expensive",
        can_write=False,
        pricing=Pricing(rates={"m": (10.0, 30.0)}),
        params=ModelParams(model="m"),
        budget=Budget(max_turns=99, max_cost=50.0),
    )
    assert report.outcome == "stopped"
    assert report.reason == "budget_exhausted"
    assert "cost budget" in (report.output.text or "")


def test_no_pricing_means_no_cost_tracked() -> None:
    model = FakeModel(responses=[_usage_response("done", 1_000_000, 1_000_000)])
    report = run_loop(
        model, _registry(find_orders), "hi", can_write=False, params=ModelParams(model="m")
    )
    assert report.usage.cost == 0.0  # honest: no meter, no cost


# --- time budget + injected clock -------------------------------------------------


def test_time_budget_stops_a_slow_run_deterministically() -> None:
    # started_at=0; the pre-think guard on the 2nd iteration sees 10s elapsed.
    clock = FakeClock([0.0, 10.0, 10.0])
    model = FakeModel(
        responses=[
            _usage_response("first", 1, 1),
            _usage_response("second", 1, 1),
        ]
    )
    report = run_loop(
        model,
        _registry(find_orders),
        "slow",
        can_write=False,
        clock=clock,
        budget=Budget(max_turns=99, max_seconds=5.0),
    )
    assert report.outcome == "stopped"
    assert report.reason == "budget_exhausted"
    assert "time budget" in (report.output.text or "")


def test_time_budget_not_tripped_when_fast() -> None:
    clock = FakeClock([0.0, 1.0, 1.0])
    model = FakeModel(responses=[_usage_response("done", 1, 1)])
    report = run_loop(
        model,
        _registry(find_orders),
        "fast",
        can_write=False,
        clock=clock,
        budget=Budget(max_seconds=5.0),
    )
    assert report.outcome == "completed"


# --- compaction during a run ------------------------------------------------------


def test_long_run_compacts_and_keeps_the_goal() -> None:
    # A model that keeps calling a capability that returns a big blob; each tool
    # result balloons the history until compaction fires — but the run still ends.
    big = "z" * 6000

    @capability(reads=True)
    def fetch_blob() -> str:
        "Fetch a large blob."
        return big

    script: list[dict[str, object]] = [{"call": "fetch_blob", "args": {}} for _ in range(8)]
    script.append({"final": "done"})
    model = FakeModel.from_script(script)
    report = run_loop(
        model,
        _registry(fetch_blob),
        "keep fetching then summarize",
        can_write=False,
        budget=Budget(max_turns=99, compact_at=3000),
    )
    assert report.outcome == "completed"
    assert report.compactions > 0  # compaction actually fired
    # The goal survived every compaction: the model always saw the user goal.
    for msgs, _tools in model.calls:
        assert any(m.role is Role.USER and "keep fetching" in (m.text or "") for m in msgs)


def test_compaction_off_by_default_leaves_short_runs_untouched() -> None:
    model = FakeModel.from_script([{"call": "find_orders", "args": {}}, {"final": "two"}])
    report = run_loop(model, _registry(find_orders), "count", can_write=False)
    assert report.compactions == 0


# --- the tiny, immutable, policy-free system prompt -------------------------------


def test_system_prompt_is_tiny_and_holds_no_policy_text() -> None:
    from reins.loop import SYSTEM_PROMPT

    assert len(SYSTEM_PROMPT) < 400  # immutable and small (§2.5, §3.3)
    lowered = SYSTEM_PROMPT.lower()
    for term in ("autonomy", "approval", "policy", "destructive", "read_only", "principal"):
        assert term not in lowered, term
