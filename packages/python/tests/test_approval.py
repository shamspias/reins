"""Tests for the human gate (M2.3): the approval request preview, the default
terminal handler (y/n/show + every fail-closed path), handler coercion, and the
bulk (row-limit) heuristic."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from reins.approval import (
    ApprovalRequest,
    TerminalApprovalHandler,
    as_approval_handler,
    bulk_count,
)
from reins.errors import ApprovalError
from reins.policy import ProposedAction
from reins.types import Access, Capability, ToolCall


def _action(name: str, access: Access, args: dict[str, Any] | None = None) -> ProposedAction:
    spec = Capability(name=name, description=f"{name}.", access=access)
    return ProposedAction(call=ToolCall(id="c1", name=name, arguments=args or {}), capability=spec)


def _request(*actions: ProposedAction, bulk: int | None = None) -> ApprovalRequest:
    return ApprovalRequest(actions=tuple(actions), reason="approval_required", bulk_count=bulk)


def _scripted_input(answers: list[str]) -> Callable[[str], str]:
    it = iter(answers)

    def fake_input(prompt: str) -> str:
        try:
            return next(it)
        except StopIteration:  # script exhausted behaves like a closed stdin
            raise EOFError from None

    return fake_input


# --- preview / detail: the human must see the exact call and args -----------


def test_preview_shows_exact_call_and_args() -> None:
    request = _request(_action("refund_payment", Access.DESTRUCTIVE, {"order_id": 8842}))
    preview = request.preview()
    assert "refund_payment" in preview
    assert "order_id=8842" in preview
    assert "⚠" in preview


def test_preview_names_the_access_class() -> None:
    write = _request(_action("update_order", Access.WRITE, {"order_id": 1}))
    destructive = _request(_action("delete_order", Access.DESTRUCTIVE, {"order_id": 1}))
    assert "WRITE" in write.preview()
    assert "DESTRUCTIVE" in destructive.preview()


def test_preview_shows_bulk_count() -> None:
    request = _request(
        _action("update_orders", Access.WRITE, {"order_ids": [1, 2, 3]}),
        bulk=3,
    )
    assert "3 items" in request.preview()


def test_preview_lists_every_action_in_a_batch() -> None:
    request = _request(
        _action("update_order", Access.WRITE, {"order_id": 1}),
        _action("delete_order", Access.DESTRUCTIVE, {"order_id": 2}),
    )
    preview = request.preview()
    assert "update_order" in preview
    assert "delete_order" in preview


def test_preview_bounds_long_argument_values() -> None:
    # Everything is bounded (ENGINEERING.md §2): a huge arg must not flood the terminal.
    request = _request(_action("update_note", Access.WRITE, {"text": "x" * 5000}))
    preview = request.preview()
    assert len(preview) < 500
    assert "…" in preview


def test_preview_bounds_model_controlled_argument_keys() -> None:
    # Argument *keys* come from the model too; a huge key must not flood the terminal.
    request = _request(_action("update_note", Access.WRITE, {"k" * 5000: 1}))
    assert len(request.preview()) < 500


def test_preview_bounds_the_number_of_listed_actions() -> None:
    actions = [_action(f"update_thing_{i}", Access.WRITE, {"thing_id": i}) for i in range(50)]
    request = _request(*actions)
    preview = request.preview()
    assert preview.count("update_thing_") < 20
    assert "more" in preview  # the elision is announced, not silent


def test_detail_shows_the_full_exact_arguments() -> None:
    # [s]how reveals the unabridged call — the acceptance bar is "exact call/args".
    long_text = "x" * 5000
    request = _request(_action("update_note", Access.WRITE, {"text": long_text, "id": 7}))
    detail = json.loads(request.detail())
    assert detail == [{"capability": "update_note", "arguments": {"text": long_text, "id": 7}}]


# --- the default terminal handler --------------------------------------------


def _terminal(answers: list[str], printed: list[str]) -> TerminalApprovalHandler:
    return TerminalApprovalHandler(input_fn=_scripted_input(answers), print_fn=printed.append)


def test_terminal_yes_approves() -> None:
    printed: list[str] = []
    request = _request(_action("delete_order", Access.DESTRUCTIVE, {"order_id": 1}))
    assert _terminal(["y"], printed).approve(request) is True
    assert _terminal(["yes"], printed).approve(request) is True


def test_terminal_no_denies() -> None:
    printed: list[str] = []
    request = _request(_action("delete_order", Access.DESTRUCTIVE, {"order_id": 1}))
    assert _terminal(["n"], printed).approve(request) is False
    assert _terminal(["no"], printed).approve(request) is False


def test_terminal_show_prints_detail_then_reprompts() -> None:
    printed: list[str] = []
    request = _request(_action("delete_order", Access.DESTRUCTIVE, {"order_id": 1}))
    assert _terminal(["s", "y"], printed).approve(request) is True
    assert any('"order_id": 1' in line for line in printed), "show must reveal the exact args"


def test_terminal_prints_the_preview_before_asking() -> None:
    printed: list[str] = []
    request = _request(_action("delete_order", Access.DESTRUCTIVE, {"order_id": 1}))
    _terminal(["n"], printed).approve(request)
    assert printed and "delete_order" in printed[0]


def test_terminal_denies_on_eof() -> None:
    # No interactive human (closed stdin / CI) must deny, never approve (§2.2).
    printed: list[str] = []
    request = _request(_action("delete_order", Access.DESTRUCTIVE))
    assert _terminal([], printed).approve(request) is False


def test_terminal_denies_on_interrupt() -> None:
    def interrupted(prompt: str) -> str:
        raise KeyboardInterrupt

    handler = TerminalApprovalHandler(input_fn=interrupted, print_fn=lambda _: None)
    assert handler.approve(_request(_action("delete_order", Access.DESTRUCTIVE))) is False


def test_terminal_denies_on_oserror() -> None:
    # pytest's captured stdin raises OSError from input(); that is a headless deny.
    def broken(prompt: str) -> str:
        raise OSError("reading from stdin while output is captured")

    handler = TerminalApprovalHandler(input_fn=broken, print_fn=lambda _: None)
    assert handler.approve(_request(_action("delete_order", Access.DESTRUCTIVE))) is False


def test_terminal_reprompts_on_unrecognized_then_accepts_answer() -> None:
    printed: list[str] = []
    request = _request(_action("delete_order", Access.DESTRUCTIVE))
    assert _terminal(["maybe", "n"], printed).approve(request) is False


def test_terminal_bounds_the_reprompt_loop() -> None:
    # A stream of garbage must terminate in a deny, not loop forever (§2.13).
    prompts = 0

    def garbage(prompt: str) -> str:
        nonlocal prompts
        prompts += 1
        return "banana"

    handler = TerminalApprovalHandler(input_fn=garbage, print_fn=lambda _: None)
    assert handler.approve(_request(_action("delete_order", Access.DESTRUCTIVE))) is False
    assert prompts <= 10


# --- pluggable approve= coercion ---------------------------------------------


def test_as_approval_handler_passes_a_handler_through() -> None:
    handler = TerminalApprovalHandler()
    assert as_approval_handler(handler) is handler


def test_as_approval_handler_rejects_non_callables_with_a_hint() -> None:
    with pytest.raises(ApprovalError, match="→"):
        as_approval_handler(True)  # type: ignore[arg-type]


def test_as_approval_handler_wraps_a_plain_callable() -> None:
    seen: list[ApprovalRequest] = []

    def decide(request: ApprovalRequest) -> bool:
        seen.append(request)
        return True

    request = _request(_action("delete_order", Access.DESTRUCTIVE))
    assert as_approval_handler(decide).approve(request) is True
    assert seen == [request]


def test_wrapped_callable_coerces_truthiness_to_bool() -> None:
    handler = as_approval_handler(lambda request: 1)  # type: ignore[arg-type, return-value]
    assert handler.approve(_request(_action("delete_order", Access.DESTRUCTIVE))) is True


# --- bulk detection (the pre-ORM row-limit default) ---------------------------


def test_bulk_count_detects_a_multi_item_list_on_a_write() -> None:
    action = _action("update_orders", Access.WRITE, {"order_ids": [1, 2, 3]})
    assert bulk_count([action]) == 3


def test_bulk_count_ignores_reads() -> None:
    # Reads run freely — a bulk-looking read must never trigger a prompt (§2.2).
    action = _action("find_orders", Access.READ, {"order_ids": [1, 2, 3]})
    assert bulk_count([action]) is None


def test_bulk_count_ignores_single_item_lists_and_scalars() -> None:
    action = _action(
        "update_order", Access.WRITE, {"order_ids": [1], "status": "shipped", "count": 5}
    )
    assert bulk_count([action]) is None


def test_bulk_count_takes_the_largest_across_the_batch() -> None:
    actions = [
        _action("update_orders", Access.WRITE, {"order_ids": [1, 2]}),
        _action("delete_orders", Access.DESTRUCTIVE, {"order_ids": [1, 2, 3, 4]}),
    ]
    assert bulk_count(actions) == 4


def test_bulk_count_checks_top_level_arguments_only() -> None:
    # Nested shapes are out of scope for the heuristic (documented); real row
    # counts arrive with the ORM adapters (M4.1).
    action = _action("update_orders", Access.WRITE, {"filter": {"ids": [1, 2, 3]}})
    assert bulk_count([action]) is None
