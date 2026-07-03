"""Tests for code-mode in the loop (M3.2): run_program exposure, per-call gating
inside programs, the data-heavy token win, and the mode='simple' fallback."""

import pytest

from reins.approval import ApprovalRequest
from reins.capability import BoundCapability, capability
from reins.errors import SandboxError
from reins.loop import Agent, run_loop
from reins.model import FakeModel
from reins.policy import AutonomyLevel
from reins.registry import CapabilityRegistry
from reins.types import Context

ROWS = [{"id": i, "status": "open" if i % 2 else "closed"} for i in range(500)]


@capability(reads=True)
def find_orders(status: str = "open") -> list[dict[str, object]]:
    "Find orders by status."
    return ROWS


@capability
def delete_order(order_id: int) -> str:
    "Delete an order."
    return f"deleted {order_id}"


class SpyHandler:
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


def _context_chars(model: FakeModel) -> int:
    return sum(len(m.text or "") for msgs, _ in model.calls for m in msgs)


# --- the point of code-mode: data stays in the sandbox ----------------------------


def test_data_heavy_work_stays_far_below_simple_mode() -> None:
    program = (
        "rows = find_orders(status='open')\nprint(len([r for r in rows if r['status'] == 'open']))"
    )
    code_model = FakeModel.from_script([{"program": program}, {"final": "250 open."}])
    code_report = run_loop(code_model, _registry(find_orders), "count open", can_write=False)
    assert code_report.outcome == "completed"
    assert code_report.executed == ("find_orders",)

    simple_model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "250 open."}]
    )
    simple_report = run_loop(
        simple_model, _registry(find_orders), "count open", can_write=False, mode="simple"
    )
    assert simple_report.outcome == "completed"

    code_ctx, simple_ctx = _context_chars(code_model), _context_chars(simple_model)
    # 500 rows flow through the model in simple mode; only "250" returns in code mode.
    assert code_ctx * 5 < simple_ctx, (code_ctx, simple_ctx)


def test_program_output_returns_only_what_was_printed() -> None:
    model = FakeModel.from_script(
        [{"program": "rows = find_orders()\nprint('rows:', len(rows))"}, {"final": "500."}]
    )
    run_loop(model, _registry(find_orders), "count", can_write=False)
    tool_texts = [m.text or "" for msgs, _ in model.calls for m in msgs if m.role.value == "tool"]
    assert any("rows: 500" in t for t in tool_texts)
    assert not any("closed" in t for t in tool_texts)  # raw rows never enter context


# --- authority is not isolated: gating inside programs -----------------------------


def test_write_inside_a_program_under_ask_is_refused_as_data() -> None:
    program = (
        "try:\n"
        "    delete_order(order_id=1)\n"
        "    print('deleted')\n"
        "except CapabilityError as exc:\n"
        "    print('refused:', exc)\n"
    )
    model = FakeModel.from_script([{"program": program}, {"final": "could not."}])
    report = run_loop(
        model, _registry(find_orders, delete_order), "delete order 1", can_write=False
    )
    assert report.outcome == "completed"
    assert report.executed == ()  # the write never ran (§2.1)
    (record,) = report.audit
    assert record.decision == "refused"
    tool_texts = [m.text or "" for msgs, _ in model.calls for m in msgs if m.role.value == "tool"]
    assert any("refused:" in t and "read-only" in t for t in tool_texts)


def test_write_inside_a_program_is_gated_and_executes_when_approved() -> None:
    gate = SpyHandler(decision=True)
    model = FakeModel.from_script(
        [{"program": "print(delete_order(order_id=7))"}, {"final": "done"}]
    )
    report = run_loop(
        model, _registry(delete_order), "delete order 7", can_write=True, approve=gate
    )
    assert report.outcome == "executed"
    assert report.executed == ("delete_order",)
    assert [r.decision for r in report.audit] == ["approved", "executed"]
    assert len(gate.requests) == 1
    assert "delete_order" in gate.requests[0].preview()


def test_denied_write_inside_a_program_is_an_error_the_program_can_handle() -> None:
    gate = SpyHandler(decision=False)
    program = (
        "try:\n"
        "    delete_order(order_id=7)\n"
        "except CapabilityError as exc:\n"
        "    print('denied:', exc)\n"
    )
    model = FakeModel.from_script([{"program": program}, {"final": "not deleted."}])
    report = run_loop(model, _registry(delete_order), "delete", can_write=True, approve=gate)
    assert report.outcome == "completed"
    assert report.executed == ()
    assert [r.decision for r in report.audit] == ["approval_denied"]


def test_bulk_write_inside_a_program_needs_confirm_even_when_trusted() -> None:
    @capability
    def update_orders(order_ids: list[int], status: str) -> str:
        "Update a batch of orders."
        return "updated"

    gate = SpyHandler(decision=False)
    program = "update_orders(order_ids=[1, 2, 3], status='shipped')"
    model = FakeModel.from_script([{"program": program}, {"final": "blocked."}])
    report = run_loop(
        model,
        _registry(update_orders),
        "ship all",
        can_write=True,
        autonomy=AutonomyLevel.TRUSTED,
        approve=gate,
    )
    assert report.executed == ()
    assert gate.requests[0].bulk_count == 3


def test_rls_scoped_capability_inside_a_program_needs_a_principal() -> None:
    @capability(reads=True, scope="user")
    def list_my_notes(ctx: Context) -> list[str]:
        "List the calling user's notes."
        return ["mine"]

    program = (
        "try:\n    list_my_notes()\nexcept CapabilityError as exc:\n    print('blocked:', exc)\n"
    )
    model = FakeModel.from_script([{"program": program}, {"final": "no access."}])
    report = run_loop(model, _registry(list_my_notes), "my notes", can_write=False)
    assert report.executed == ()
    tool_texts = [m.text or "" for msgs, _ in model.calls for m in msgs if m.role.value == "tool"]
    assert any("principal" in t for t in tool_texts)


# --- mode plumbing ------------------------------------------------------------------


def test_code_mode_exposes_run_program_and_simple_does_not() -> None:
    model = FakeModel.from_script([{"final": "hi"}])
    run_loop(model, _registry(find_orders), "hello", can_write=False)
    assert "run_program" in [t.name for t in model.calls[0][1]]

    model = FakeModel.from_script([{"final": "hi"}])
    run_loop(model, _registry(find_orders), "hello", can_write=False, mode="simple")
    assert "run_program" not in [t.name for t in model.calls[0][1]]


def test_simple_mode_treats_run_program_as_an_unknown_capability() -> None:
    model = FakeModel.from_script([{"program": "print(1)"}, {"final": "ok"}])
    report = run_loop(model, _registry(find_orders), "x", can_write=False, mode="simple")
    assert report.outcome == "completed"
    tool_texts = [m.text or "" for msgs, _ in model.calls for m in msgs if m.role.value == "tool"]
    assert any("no capability named 'run_program'" in t for t in tool_texts)


def test_invalid_mode_is_a_hinted_error_at_construction() -> None:
    with pytest.raises(SandboxError, match="→"):
        Agent(capabilities=[find_orders], mode="yolo")


def test_run_program_name_collision_fails_loudly() -> None:
    @capability(reads=True, name="run_program")
    def rogue() -> None:
        "A capability squatting on the reserved name."

    model = FakeModel.from_script([{"final": "hi"}])
    with pytest.raises(SandboxError, match="→"):
        run_loop(model, _registry(rogue), "x", can_write=False)


def test_bad_code_argument_is_a_hinted_error_as_data() -> None:
    model = FakeModel.from_script([{"call": "run_program", "args": {"code": 5}}, {"final": "ok"}])
    report = run_loop(model, _registry(find_orders), "x", can_write=False)
    assert report.outcome == "completed"
    tool_texts = [m.text or "" for msgs, _ in model.calls for m in msgs if m.role.value == "tool"]
    assert any("→" in t and "code" in t for t in tool_texts)
