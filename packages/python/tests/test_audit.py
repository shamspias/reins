"""Tests for the audit log (M2.4): structured records, PII redaction, sinks, and
the identity Context value type."""

from datetime import UTC, datetime

from reins.audit import REDACTED, AuditRecord, MemoryAuditSink, redact_arguments
from reins.codec import from_dict, to_dict
from reins.types import Access, Context


def _record(**overrides: object) -> AuditRecord:
    values: dict[str, object] = {
        "timestamp": datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC),
        "principal": "user:alice",
        "capability": "refund_order",
        "arguments": {"order_id": 8842},
        "access": Access.DESTRUCTIVE,
        "decision": "approved",
        "reason": None,
        "trace_id": "t1",
    }
    values.update(overrides)
    return AuditRecord(**values)  # type: ignore[arg-type]


# --- redaction: never log secrets or raw PII (ENGINEERING.md §5) -------------


def test_redact_masks_sensitive_keys_case_insensitively() -> None:
    redacted = redact_arguments(
        {"password": "hunter2", "API_KEY": "sk-123", "Authorization": "Bearer x", "order_id": 1}
    )
    assert redacted["password"] == REDACTED
    assert redacted["API_KEY"] == REDACTED
    assert redacted["Authorization"] == REDACTED
    assert redacted["order_id"] == 1


def test_redact_recurses_into_nested_structures() -> None:
    redacted = redact_arguments(
        {"user": {"name": "amy", "token": "abc"}, "batch": [{"card_number": "4111"}]}
    )
    assert redacted["user"] == {"name": "amy", "token": REDACTED}
    assert redacted["batch"] == [{"card_number": REDACTED}]


def test_redact_copies_and_never_mutates_the_original() -> None:
    # The exact args must still reach the capability — only the *audit copy* is masked.
    original = {"password": "hunter2", "nested": {"secret": "s"}}
    redact_arguments(original)
    assert original == {"password": "hunter2", "nested": {"secret": "s"}}


# --- records and sinks --------------------------------------------------------


def test_memory_sink_collects_records_in_order() -> None:
    sink = MemoryAuditSink()
    first, second = _record(decision="approved"), _record(decision="executed")
    sink.record(first)
    sink.record(second)
    assert sink.records == [first, second]


def test_audit_record_round_trips_via_codec() -> None:
    record = _record()
    assert from_dict(AuditRecord, to_dict(record)) == record


def test_context_round_trips_via_codec() -> None:
    ctx = Context(principal="user:42", trace_id="abc")
    assert from_dict(Context, to_dict(ctx)) == ctx
