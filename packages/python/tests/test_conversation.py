"""Tests for context discipline (M3.3): token estimation and history compaction
that always preserves the goal and never orphans a tool result."""

from reins.conversation import compact, estimate_tokens
from reins.types import Message, Role, ToolCall


def _sys() -> Message:
    return Message(role=Role.SYSTEM, text="You operate an application.")


def _goal() -> Message:
    return Message(role=Role.USER, text="reconcile every invoice from last quarter")


def _assistant_call(name: str, big: int = 0) -> Message:
    return Message(
        role=Role.ASSISTANT,
        tool_calls=[ToolCall(id=f"c_{name}", name=name, arguments={"pad": "x" * big})],
    )


def _tool_result(name: str, size: int) -> Message:
    return Message(role=Role.TOOL, tool_call_id=f"c_{name}", text="y" * size)


def _long_history(rounds: int, result_size: int = 4000) -> list[Message]:
    messages = [_sys(), _goal()]
    for i in range(rounds):
        messages.append(_assistant_call(f"step_{i}"))
        messages.append(_tool_result(f"step_{i}", result_size))
    messages.append(Message(role=Role.ASSISTANT, text="here is the answer"))
    return messages


# --- estimation -----------------------------------------------------------------


def test_estimate_grows_with_content() -> None:
    small = [Message(role=Role.USER, text="hi")]
    big = [Message(role=Role.USER, text="x" * 4000)]
    assert estimate_tokens(big) > estimate_tokens(small)
    assert estimate_tokens(big) >= 900  # ~4 chars/token


def test_estimate_counts_tool_call_arguments() -> None:
    plain = [Message(role=Role.ASSISTANT, text="ok")]
    with_call = [_assistant_call("f", big=4000)]
    assert estimate_tokens(with_call) > estimate_tokens(plain)


# --- compaction: the safety-critical behavior -------------------------------------


def test_short_history_is_untouched() -> None:
    messages = [_sys(), _goal(), Message(role=Role.ASSISTANT, text="done")]
    out, compacted = compact(messages, compact_at=24_000)
    assert compacted is False
    assert out == messages


def test_disabled_when_compact_at_is_none() -> None:
    messages = _long_history(50)
    out, compacted = compact(messages, compact_at=None)
    assert compacted is False
    assert out is messages


def test_long_history_compacts() -> None:
    messages = _long_history(50)
    out, compacted = compact(messages, compact_at=2000)
    assert compacted is True
    assert len(out) < len(messages)
    assert estimate_tokens(out) < estimate_tokens(messages)


def test_compaction_always_preserves_the_goal() -> None:
    messages = _long_history(50)
    out, _ = compact(messages, compact_at=2000)
    assert out[0].role is Role.SYSTEM
    assert any(m.role is Role.USER and "reconcile every invoice" in (m.text or "") for m in out)


def test_compaction_keeps_the_most_recent_messages() -> None:
    messages = _long_history(50)
    out, _ = compact(messages, compact_at=2000, keep_recent=6)
    assert out[-1].text == "here is the answer"
    assert messages[-3:] == out[-3:]  # the tail is intact


def test_compaction_inserts_one_elision_marker() -> None:
    messages = _long_history(50)
    out, _ = compact(messages, compact_at=2000)
    markers = [m for m in out if m.role is Role.SYSTEM and "elided" in (m.text or "")]
    assert len(markers) == 1
    assert "goal above" in markers[0].text  # type: ignore[operator]


def test_compaction_never_orphans_a_tool_result() -> None:
    # Every tool result kept must have its originating assistant tool_call kept too,
    # or the provider wire format breaks.
    messages = _long_history(50)
    out, _ = compact(messages, compact_at=2000, keep_recent=6)
    live_call_ids = {tc.id for m in out if m.role is Role.ASSISTANT for tc in m.tool_calls}
    for m in out:
        if m.role is Role.TOOL:
            assert m.tool_call_id in live_call_ids, "orphaned tool result after compaction"


def test_compaction_is_idempotent_below_threshold_after_running() -> None:
    messages = _long_history(50)
    once, _ = compact(messages, compact_at=2000)
    twice, compacted_again = compact(once, compact_at=estimate_tokens(once) + 1)
    assert compacted_again is False
    assert twice == once
