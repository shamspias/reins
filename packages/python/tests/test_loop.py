"""Tests for the harness loop, stop conditions, budget, the Agent (M1.4), the
approval gate (M2.3), and audit + identity + RLS (M2.4)."""

import io

import pytest

from reins.approval import ApprovalRequest
from reins.audit import REDACTED, AuditRecord, MemoryAuditSink
from reins.budget import Budget
from reins.capability import BoundCapability, capability
from reins.errors import ApprovalError, ModelError
from reins.loop import Agent, run_loop
from reins.model import FakeModel
from reins.policy import AutonomyLevel
from reins.registry import CapabilityRegistry
from reins.stop import GoalReached, MaxTurns
from reins.types import Context, FinishReason, Message, ModelResponse, Role, RunState, Usage


@capability
def find_orders(status: str = "open") -> list[str]:
    "Find orders by status."
    return ["o1", "o2"]


@capability
def delete_order(order_id: int) -> None:
    "Delete an order."


@capability
def update_orders(order_ids: list[int], status: str) -> str:
    "Update the status of a batch of orders."
    return "updated"


class SpyHandler:
    """A scriptable gate that records every request it is shown."""

    def __init__(self, decision: bool) -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    def approve(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self.decision


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


def test_run_approved_destructive_write_executes() -> None:
    gate = SpyHandler(decision=True)
    model = FakeModel.from_script(
        [{"call": "delete_order", "args": {"order_id": 1}}, {"final": "deleted"}]
    )
    report = run_loop(
        model, _registry(delete_order), "delete order 1", can_write=True, approve=gate
    )
    assert report.outcome == "executed"  # a write ran (after approval) and the goal was met
    assert report.executed == ("delete_order",)
    assert report.audited is True
    assert len(gate.requests) == 1


def test_run_denied_write_is_not_executed_and_audited() -> None:
    gate = SpyHandler(decision=False)
    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])
    report = run_loop(
        model, _registry(delete_order), "delete order 1", can_write=True, approve=gate
    )
    assert report.outcome == "not_executed"
    assert report.reason == "approval_denied"
    assert report.executed == ()
    assert report.audited is True
    assert "→" in (report.output.text or "")


def test_gate_preview_shows_the_exact_call_and_args() -> None:
    gate = SpyHandler(decision=False)
    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 8842}}])
    run_loop(model, _registry(delete_order), "delete order 8842", can_write=True, approve=gate)
    preview = gate.requests[0].preview()
    assert "delete_order" in preview
    assert "order_id=8842" in preview


def test_run_write_denied_by_default_when_headless(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The default gate is the terminal handler; with no interactive human it must
    # fail closed — the default never auto-executes a write (invariant §2.10).
    # Empty stdin makes input() raise EOFError deterministically (also under -s).
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])
    report = run_loop(model, _registry(delete_order), "delete order 1", can_write=True)
    assert report.outcome == "not_executed"
    assert report.reason == "approval_denied"
    assert report.executed == ()
    assert report.audited is True
    assert "delete_order" in capsys.readouterr().out  # the preview was rendered


def test_run_broken_handler_fails_closed_and_says_so() -> None:
    def broken(request: ApprovalRequest) -> bool:
        raise RuntimeError("handler crashed")

    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])
    report = run_loop(
        model, _registry(delete_order), "delete order 1", can_write=True, approve=broken
    )
    assert report.outcome == "not_executed"
    assert report.reason == "approval_denied"
    assert report.executed == ()
    # A broken handler must not masquerade as a human "no" (report honestly).
    assert "handler failed" in (report.output.text or "")
    assert "→" in (report.output.text or "")


def test_non_callable_approve_raises_at_construction() -> None:
    # approve=True is a plausible beginner mistake; it must fail loudly and early,
    # never silently deny every write.
    with pytest.raises(ApprovalError, match="→"):
        Agent(capabilities=[delete_order], approve=True)  # type: ignore[arg-type]


def test_run_accepts_a_plain_callable_as_approve() -> None:
    model = FakeModel.from_script(
        [{"call": "delete_order", "args": {"order_id": 1}}, {"final": "done"}]
    )
    report = run_loop(
        model,
        _registry(delete_order),
        "delete order 1",
        can_write=True,
        approve=lambda request: True,
    )
    assert report.outcome == "executed"
    assert report.executed == ("delete_order",)


def test_ask_never_reaches_the_gate() -> None:
    # ask() refuses writes before policy, and reads never gate — the handler must
    # be structurally unreachable from ask() (invariant §2.1).
    gate = SpyHandler(decision=True)
    model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "2 orders"}]
    )
    report = run_loop(model, _registry(find_orders), "count orders", can_write=False, approve=gate)
    assert report.outcome == "completed"
    assert gate.requests == []


def test_run_executes_plain_write_under_trusted_without_prompting() -> None:
    @capability
    def update_status(order_id: int) -> str:
        "Update an order's status."
        return "updated"

    gate = SpyHandler(decision=False)  # would deny if (wrongly) consulted
    model = FakeModel.from_script(
        [{"call": "update_status", "args": {"order_id": 1}}, {"final": "done"}]
    )
    report = run_loop(
        model,
        _registry(update_status),
        "update",
        can_write=True,
        autonomy=AutonomyLevel.TRUSTED,
        approve=gate,
    )
    assert report.outcome == "executed"
    assert report.executed == ("update_status",)
    assert gate.requests == []  # gate narrowly: no approval fatigue on trusted plain writes
    # M2.4: even an ungated write attempt is audited (invariant §2.2).
    assert report.audited is True
    assert [r.decision for r in report.audit] == ["executed"]


def test_run_with_reads_only_reports_completed() -> None:
    # The §7.4 outcome split: run() that never wrote is "completed", not "executed".
    gate = SpyHandler(decision=True)
    model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "2 orders"}]
    )
    report = run_loop(model, _registry(find_orders), "count orders", can_write=True, approve=gate)
    assert report.outcome == "completed"
    assert report.audited is False
    assert gate.requests == []


def test_validation_rejected_write_never_counts_as_executed() -> None:
    # Invalid args never reach user code (§2.6) — even after an approval, the
    # rejected call must not be reported as an executed write.
    gate = SpyHandler(decision=True)
    model = FakeModel.from_script(
        [{"call": "delete_order", "args": {"bogus": 1}}, {"final": "could not delete"}]
    )
    report = run_loop(
        model, _registry(delete_order), "delete order 1", can_write=True, approve=gate
    )
    assert report.outcome == "completed"  # nothing ran, so no write happened
    assert report.executed == ()
    assert report.audited is True  # the gate decision was still recorded


def test_write_that_raises_still_counts_as_executed() -> None:
    # Invoked write code may have side effects even when it raises; reporting
    # errs toward "a write happened" (fail-closed honesty).
    @capability
    def delete_thing(thing_id: int) -> None:
        "Delete a thing."
        raise RuntimeError("db exploded")

    model = FakeModel.from_script(
        [{"call": "delete_thing", "args": {"thing_id": 1}}, {"final": "failed"}]
    )
    report = run_loop(
        model, _registry(delete_thing), "delete", can_write=True, approve=lambda r: True
    )
    assert report.outcome == "executed"
    assert report.executed == ("delete_thing",)


def test_custom_policy_gating_reads_shows_the_whole_batch() -> None:
    # A custom policy may demand approval for a read-only turn; the fallback must
    # show the human the real batch (labeled READ), not an empty preview.
    from reins.policy import Decision, PolicyDecision, ProposedAction

    class GateEverything:
        def check_actions(
            self, actions: list[ProposedAction], autonomy: AutonomyLevel
        ) -> PolicyDecision:
            return PolicyDecision(Decision.REQUIRE_APPROVAL, "approval_required")

        def check_output(self, output: Message) -> PolicyDecision:
            return PolicyDecision(Decision.ALLOW)

    gate = SpyHandler(decision=False)
    model = FakeModel.from_script([{"call": "find_orders", "args": {"status": "open"}}])
    report = run_loop(
        model,
        _registry(find_orders),
        "count",
        can_write=True,
        policy=GateEverything(),
        approve=gate,
    )
    assert report.outcome == "not_executed"
    assert len(gate.requests[0].actions) == 1
    assert "READ" in gate.requests[0].preview()


def test_bulk_write_prompts_even_when_trusted() -> None:
    # The row-limit default: >1 row needs explicit confirm at every level.
    gate = SpyHandler(decision=False)
    model = FakeModel.from_script(
        [{"call": "update_orders", "args": {"order_ids": [1, 2, 3], "status": "shipped"}}]
    )
    report = run_loop(
        model,
        _registry(update_orders),
        "ship them all",
        can_write=True,
        autonomy=AutonomyLevel.TRUSTED,
        approve=gate,
    )
    assert report.outcome == "not_executed"
    assert report.reason == "approval_denied"
    assert gate.requests[0].bulk_count == 3


def test_confirm_bulk_off_skips_the_bulk_gate() -> None:
    gate = SpyHandler(decision=False)
    model = FakeModel.from_script(
        [
            {"call": "update_orders", "args": {"order_ids": [1, 2, 3], "status": "shipped"}},
            {"final": "done"},
        ]
    )
    report = run_loop(
        model,
        _registry(update_orders),
        "ship them all",
        can_write=True,
        autonomy=AutonomyLevel.TRUSTED,
        approve=gate,
        confirm_bulk=False,
    )
    assert report.outcome == "executed"
    assert gate.requests == []


def test_agent_run_passes_approve_through() -> None:
    gate = SpyHandler(decision=True)
    agent = Agent(
        model=FakeModel.from_script(
            [{"call": "delete_order", "args": {"order_id": 1}}, {"final": "done"}]
        ),
        capabilities=[delete_order],
        approve=gate,
    )
    result = agent.run("delete order 1")
    assert result.output.text == "done"
    assert len(gate.requests) == 1


def test_run_write_refused_under_read_only() -> None:
    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])
    report = run_loop(
        model, _registry(delete_order), "delete", can_write=True, autonomy=AutonomyLevel.READ_ONLY
    )
    assert report.outcome == "refused"
    assert report.reason == "write_in_read_only"


def test_policy_text_never_reaches_the_model() -> None:
    # Invariant §2.5: policy constraints are enforced in code, never sent to the model.
    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])
    run_loop(
        model,
        _registry(delete_order),
        "delete order 1",
        can_write=True,
        autonomy=AutonomyLevel.APPROVED_WRITES,
    )
    shown = " ".join(m.text or "" for msgs, _caps in model.calls for m in msgs).lower()
    for term in (
        "autonomy",
        "approval",
        "policy",
        "read_only",
        "approved_writes",
        "require_approval",
    ):
        assert term not in shown, term


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


# --- audit + identity + RLS (M2.4) --------------------------------------------


def _script_delete() -> FakeModel:
    return FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])


def test_denied_gate_decision_is_audited_with_redacted_args() -> None:
    @capability
    def update_secret(order_id: int, password: str) -> None:
        "Update an order's secret."

    sink = MemoryAuditSink()
    model = FakeModel.from_script(
        [{"call": "update_secret", "args": {"order_id": 1, "password": "hunter2"}}]
    )
    report = run_loop(
        model,
        _registry(update_secret),
        "update",
        can_write=True,
        approve=lambda r: False,
        audit=sink,
        principal="user:alice",
    )
    assert report.outcome == "not_executed"
    (record,) = sink.records
    assert record.decision == "approval_denied"
    assert record.capability == "update_secret"
    assert record.principal == "user:alice"
    assert record.arguments == {"order_id": 1, "password": REDACTED}
    assert record.trace_id == report.trace_id


def test_approved_write_audits_approval_and_execution() -> None:
    sink = MemoryAuditSink()
    model = FakeModel.from_script(
        [{"call": "delete_order", "args": {"order_id": 1}}, {"final": "done"}]
    )
    report = run_loop(
        model,
        _registry(delete_order),
        "delete",
        can_write=True,
        approve=lambda r: True,
        audit=sink,
    )
    assert report.outcome == "executed"
    assert [r.decision for r in sink.records] == ["approved", "executed"]
    assert report.audit == tuple(sink.records)


def test_policy_denied_write_is_audited() -> None:
    report = run_loop(
        _script_delete(),
        _registry(delete_order),
        "delete",
        can_write=True,
        autonomy=AutonomyLevel.DRAFT_WRITES,
        approve=lambda r: True,
    )
    assert report.outcome == "not_executed"
    (record,) = report.audit
    assert record.decision == "denied"
    assert record.reason == "draft_only"


def test_ask_refusing_a_write_is_audited() -> None:
    # "Every write attempt is audited" includes the attempt ask() refuses (§2.2).
    report = run_loop(_script_delete(), _registry(delete_order), "delete", can_write=False)
    assert report.outcome == "refused"
    (record,) = report.audit
    assert record.decision == "refused"
    assert record.reason == "write_in_read_only"


def test_reads_are_not_audited() -> None:
    model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "2"}]
    )
    report = run_loop(model, _registry(find_orders), "count", can_write=False)
    assert report.audit == ()
    assert report.audited is False


def test_principal_reaches_the_capability_context() -> None:
    seen: list[Context] = []

    @capability(reads=True)
    def list_my_orders(ctx: Context) -> list[str]:
        "List the calling user's orders."
        seen.append(ctx)
        return []

    model = FakeModel.from_script([{"call": "list_my_orders", "args": {}}, {"final": "none"}])
    report = run_loop(
        model, _registry(list_my_orders), "my orders", can_write=False, principal="user:alice"
    )
    assert seen[0].principal == "user:alice"
    assert seen[0].trace_id == report.trace_id


def test_rls_scoped_capability_requires_a_principal() -> None:
    calls: list[str] = []

    @capability(reads=True, scope="user")
    def list_my_notes(ctx: Context) -> list[str]:
        "List the calling user's notes."
        calls.append("ran")
        return []

    model = FakeModel.from_script([{"call": "list_my_notes", "args": {}}, {"final": "none"}])
    report = run_loop(model, _registry(list_my_notes), "my notes", can_write=False)
    assert calls == []  # fail closed: scoped capability never ran without identity
    assert report.executed == ()
    # The model saw an errors-as-data message with a → next step.
    tool_texts = [m.text or "" for m in model.calls[1][0]]
    assert any("→" in t and "principal" in t for t in tool_texts)


def test_rls_cross_user_data_is_not_reachable() -> None:
    notes = {"user:alice": ["alice's plan"], "user:bob": ["bob's secret"]}

    @capability(reads=True, scope="user")
    def list_my_notes(ctx: Context) -> list[str]:
        "List the calling user's notes."
        assert ctx.principal is not None
        return notes.get(ctx.principal, [])

    model = FakeModel.from_script([{"call": "list_my_notes", "args": {}}, {"final": "1 note"}])
    run_loop(model, _registry(list_my_notes), "my notes", can_write=False, principal="user:alice")
    shown = " ".join(m.text or "" for msgs, _ in model.calls for m in msgs)
    assert "alice's plan" in shown
    assert "bob's secret" not in shown  # the other user's rows never enter the run


def test_broken_audit_sink_blocks_the_write() -> None:
    # No audit, no action (fail closed): if the write attempt cannot be recorded,
    # the write must not run.
    calls: list[str] = []

    @capability
    def update_order(order_id: int) -> None:
        "Update an order."
        calls.append("ran")

    class BrokenSink:
        def record(self, record: AuditRecord) -> None:
            raise OSError("disk full")

    model = FakeModel.from_script(
        [{"call": "update_order", "args": {"order_id": 1}}, {"final": "gave up"}]
    )
    report = run_loop(
        model,
        _registry(update_order),
        "update",
        can_write=True,
        autonomy=AutonomyLevel.TRUSTED,
        approve=lambda r: True,
        audit=BrokenSink(),
    )
    assert calls == []
    assert report.executed == ()
    shown = " ".join(m.text or "" for msgs, _ in model.calls for m in msgs)
    assert "audit" in shown and "→" in shown


def test_agent_accumulates_audit_and_carries_principal() -> None:
    agent = Agent(
        model=FakeModel.from_script(
            [{"call": "delete_order", "args": {"order_id": 1}}, {"final": "done"}]
        ),
        capabilities=[delete_order],
        approve=lambda r: True,
        principal="user:alice",
    )
    agent.run("delete order 1")
    assert [r.decision for r in agent.audit.records] == ["approved", "executed"]
    assert all(r.principal == "user:alice" for r in agent.audit.records)


def test_run_accepts_a_per_call_principal_override() -> None:
    seen: list[Context] = []

    @capability(reads=True)
    def whoami(ctx: Context) -> str:
        "Report the calling user."
        seen.append(ctx)
        return ctx.principal or "anonymous"

    agent = Agent(
        model=FakeModel.from_script([{"call": "whoami", "args": {}}, {"final": "you"}]),
        capabilities=[whoami],
        principal="user:default",
    )
    agent.ask("who am i", principal="user:override")
    assert seen[0].principal == "user:override"


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
