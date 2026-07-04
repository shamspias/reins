"""Tests for transparency (M5.1): the run trace, agent.explain(), and the
standard hinted-error set."""

import pytest

from reins import Agent, capability
from reins.errors import ModelError, ReinsError
from reins.loop import run_loop
from reins.model import FakeModel
from reins.registry import CapabilityRegistry


@capability(reads=True)
def find_orders(status: str = "open") -> list[str]:
    "Find orders by status."
    return ["o1", "o2"]


@capability
def update_password(user_id: int, password: str) -> str:
    "Update a user's password."
    return "updated"


def _registry(*caps: object) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for cap in caps:
        reg.register(cap)  # type: ignore[arg-type]
    return reg


def _kinds(report: object) -> list[str]:
    return [e.kind for e in report.trace]  # type: ignore[attr-defined]


# --- the trace records what happened ----------------------------------------------


def test_trace_records_a_capability_call() -> None:
    model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "2 orders"}]
    )
    report = run_loop(model, _registry(find_orders), "count", can_write=False)
    calls = [e for e in report.trace if e.kind == "call"]
    assert len(calls) == 1
    assert "find_orders(status='open')" in calls[0].detail
    assert calls[0].ok


def test_trace_records_a_program_and_its_output() -> None:
    program = "rows = find_orders(status='open')\nprint(len(rows))"
    model = FakeModel.from_script([{"program": program}, {"final": "done"}])
    report = run_loop(model, _registry(find_orders), "count via code", can_write=False)
    kinds = _kinds(report)
    assert "program" in kinds
    assert "output" in kinds
    prog = next(e for e in report.trace if e.kind == "program")
    assert "find_orders" in prog.detail  # the exact code the agent wrote


def test_trace_records_discovery() -> None:
    reg = CapabilityRegistry()
    for i in range(13):  # over the defer threshold → discovery is active
        reg.register(find_orders if i == 0 else _stub(f"find_thing_{i}"))
    model = FakeModel.from_script(
        [{"call": "search_capabilities", "args": {"query": "orders"}}, {"final": "found"}]
    )
    report = run_loop(model, reg, "look around", can_write=False)
    searches = [e for e in report.trace if e.kind == "search"]
    assert searches and "orders" in searches[0].detail


def test_trace_records_approval_decision() -> None:
    model = FakeModel.from_script(
        [
            {"call": "update_password", "args": {"user_id": 7, "password": "hunter2"}},
            {"final": "ok"},
        ]
    )
    report = run_loop(
        model, _registry(update_password), "reset pw", can_write=True, approve=lambda r: True
    )
    approvals = [e for e in report.trace if e.kind == "approval"]
    assert approvals and "approved" in approvals[0].detail


def test_trace_redacts_sensitive_arguments() -> None:
    # The trace must never leak secrets, just like the audit log.
    model = FakeModel.from_script(
        [
            {"call": "update_password", "args": {"user_id": 7, "password": "hunter2"}},
            {"final": "ok"},
        ]
    )
    report = run_loop(
        model, _registry(update_password), "reset pw", can_write=True, approve=lambda r: True
    )
    rendered = report.explain()
    assert "hunter2" not in rendered
    assert "[redacted]" in rendered


# --- explain() rendering ----------------------------------------------------------


def test_explain_is_readable() -> None:
    model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "2 orders are open."}]
    )
    report = run_loop(model, _registry(find_orders), "how many open?", can_write=False)
    text = report.explain()
    assert "reins trace" in text
    assert "outcome: completed" in text
    assert "find_orders" in text
    assert "answer: 2 orders are open." in text


def test_agent_explain_reflects_the_last_run() -> None:
    agent = Agent(
        model=FakeModel.from_script([{"call": "find_orders", "args": {}}, {"final": "two"}]),
        capabilities=[find_orders],
    )
    with pytest.raises(ReinsError, match="→"):
        agent.explain()  # nothing run yet
    agent.ask("count")
    assert "find_orders" in agent.explain()
    assert agent.last_report is not None


# --- the standard hinted-error set (every user-facing error ends with →) ----------


def test_no_model_configured_is_hinted(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "ZAI_API_KEY",
        "OLLAMA_BASE_URL",
        "VLLM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    agent = Agent(capabilities=[find_orders])
    with pytest.raises(ModelError, match="→"):
        agent.ask("anything")


def test_write_under_ask_is_refused_with_a_next_step() -> None:
    model = FakeModel.from_script(
        [{"call": "update_password", "args": {"user_id": 1, "password": "x"}}]
    )
    report = run_loop(model, _registry(update_password), "reset", can_write=False)
    assert report.outcome == "refused"
    assert "→" in (report.output.text or "")
    assert "run(" in (report.output.text or "")


def test_unknown_capability_suggests_closest_matches() -> None:
    reg = _registry(find_orders)
    result = reg.call("find_ordes", {})  # typo
    assert result.ok is False
    assert "→" in (result.error or "")
    assert "find_orders" in (result.error or "")  # closest match suggested


def _stub(name: str) -> object:
    from reins.capability import BoundCapability
    from reins.types import Access, Capability

    spec = Capability(
        name=name,
        description=f"{name.replace('_', ' ').capitalize()}.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        access=Access.READ,
    )
    return BoundCapability(spec=spec, func=lambda **kwargs: [])
