"""Round-trip + edge-case tests for the M1.1 value types.

Every type must satisfy ``from_dict(cls, to_dict(x)) == x``, with optional fields
present and absent, empty lists, every enum value, nested objects, arbitrary-JSON
fields, and malformed input rejected with a typed, ``→``-hinted SerializationError.
"""

import json
from datetime import UTC, datetime

import pytest

from reins.codec import from_dict, to_dict
from reins.errors import SerializationError
from reins.types import (
    Access,
    Capability,
    CapabilityResult,
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunResult,
    RunState,
    ToolCall,
    Usage,
)

_MSG_MIN = Message(role=Role.USER)
_MSG_MAX = Message(
    role=Role.ASSISTANT,
    text="refunding now",
    tool_calls=[ToolCall(id="c1", name="refund_payment", arguments={"order_id": 8842})],
    tool_call_id=None,
    meta={"trace": "abc", "nested": {"k": [1, 2, 3]}},
)
_MSG_TOOL = Message(role=Role.TOOL, tool_call_id="c1", text="ok")

# (id, instance) at minimal (defaults) and maximal (all fields, nested) population.
ROUND_TRIP_CASES = [
    ("toolcall_min", ToolCall(id="c1", name="find_orders")),
    ("toolcall_args", ToolCall(id="c2", name="f", arguments={"a": 1, "b": [True, None, "x"]})),
    ("message_min", _MSG_MIN),
    ("message_max", _MSG_MAX),
    ("message_tool", _MSG_TOOL),
    ("usage_default", Usage()),
    ("usage_full", Usage(input_tokens=10, output_tokens=20, cost=0.0123)),
    (
        "modelresponse",
        ModelResponse(
            message=_MSG_MAX, finish_reason=FinishReason.TOOL_CALLS, usage=Usage(1, 2, 0.5)
        ),
    ),
    ("capability_min", Capability(name="find_orders", description="Find orders.")),
    (
        "capability_max",
        Capability(
            name="refund_order",
            description="Refund a customer's order by its public number.",
            input_schema={"type": "object", "properties": {"order_id": {"type": "integer"}}},
            access=Access.DESTRUCTIVE,
            confirm=True,
            idempotent=False,
            scope="orders",
        ),
    ),
    ("capresult_ok", CapabilityResult(ok=True, value={"count": 42})),
    ("capresult_scalar", CapabilityResult(ok=True, value=42)),
    ("capresult_err", CapabilityResult(ok=False, error="not found", retryable=True)),
    (
        "runresult",
        RunResult(output=_MSG_MIN, reason=FinishReason.STOP, usage=Usage(), trace_id="t1"),
    ),
    ("runstate_empty", RunState()),
    (
        "runstate_full",
        RunState(
            messages=[_MSG_MIN, _MSG_MAX],
            turn=3,
            cumulative_usage=Usage(5, 6, 1.0),
            started_at=datetime(2026, 6, 29, 12, 0, tzinfo=UTC),
            last_response=ModelResponse(_MSG_MAX, FinishReason.STOP, Usage()),
        ),
    ),
]


@pytest.mark.parametrize(
    "instance", [c[1] for c in ROUND_TRIP_CASES], ids=[c[0] for c in ROUND_TRIP_CASES]
)
def test_round_trip(instance: object) -> None:
    assert from_dict(type(instance), to_dict(instance)) == instance


@pytest.mark.parametrize("role", list(Role))
def test_every_role_round_trips(role: Role) -> None:
    msg = Message(role=role)
    assert from_dict(Message, to_dict(msg)) == msg


@pytest.mark.parametrize("reason", list(FinishReason))
def test_every_finish_reason_round_trips(reason: FinishReason) -> None:
    rr = RunResult(output=Message(role=Role.ASSISTANT), reason=reason, usage=Usage(), trace_id="t")
    assert from_dict(RunResult, to_dict(rr)) == rr


@pytest.mark.parametrize("access", list(Access))
def test_every_access_round_trips(access: Access) -> None:
    cap = Capability(name="x", description="d", access=access)
    assert from_dict(Capability, to_dict(cap)) == cap


def test_to_dict_is_json_safe() -> None:
    blob = to_dict(_MSG_MAX)
    # Survives a trip through real JSON text, not just Python dicts.
    assert from_dict(Message, json.loads(json.dumps(blob))) == _MSG_MAX


def test_optional_absent_uses_default() -> None:
    assert from_dict(Message, {"role": "user"}) == Message(role=Role.USER)


def test_optional_present_as_none() -> None:
    restored = from_dict(Message, {"role": "user", "text": None, "tool_call_id": None})
    assert restored.text is None
    assert restored.tool_call_id is None


def test_empty_list_round_trips() -> None:
    msg = Message(role=Role.ASSISTANT, tool_calls=[])
    assert from_dict(Message, to_dict(msg)).tool_calls == []


def test_arbitrary_json_value_preserved() -> None:
    result = CapabilityResult(ok=True, value={"rows": [{"id": 1}, {"id": 2}], "n": 2})
    assert from_dict(CapabilityResult, to_dict(result)) == result


# --- unhappy paths: malformed input -> typed, →-hinted SerializationError ---


def test_rejects_non_mapping() -> None:
    with pytest.raises(SerializationError, match="→"):
        from_dict(Message, ["not", "a", "dict"])


def test_rejects_missing_required_field() -> None:
    with pytest.raises(SerializationError, match="role"):
        from_dict(Message, {})


def test_rejects_invalid_enum_value() -> None:
    with pytest.raises(SerializationError, match="→"):
        from_dict(Message, {"role": "wizard"})


def test_rejects_wrong_scalar_type() -> None:
    with pytest.raises(SerializationError, match="int"):
        from_dict(Usage, {"input_tokens": "lots"})


def test_rejects_wrong_type_in_optional() -> None:
    # `text: str | None` present but wrong-typed must still be rejected.
    with pytest.raises(SerializationError, match="str"):
        from_dict(Message, {"role": "user", "text": 123})


def test_rejects_bad_datetime() -> None:
    with pytest.raises(SerializationError, match="→"):
        from_dict(RunState, {"started_at": "not-a-timestamp"})


def test_rejects_nested_bad_enum() -> None:
    bad = {"message": {"role": "user"}, "finish_reason": "nope", "usage": {}}
    with pytest.raises(SerializationError, match="→"):
        from_dict(ModelResponse, bad)


def test_rejects_non_dataclass_target() -> None:
    with pytest.raises(SerializationError, match="→"):
        from_dict(int, {"x": 1})
