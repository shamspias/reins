"""Tests for the CapabilityRegistry (M1.3): list/get + validate + call (errors-as-data)."""

import pytest

from reins.capability import BoundCapability, capability
from reins.errors import CapabilityError
from reins.registry import CapabilityRegistry
from reins.types import Capability


@capability
def find_orders(status: str) -> list[str]:
    "Find orders by status."
    return [f"order:{status}"]


def _registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(find_orders)
    return reg


def test_list_and_get() -> None:
    reg = _registry()
    assert [c.name for c in reg.list()] == ["find_orders"]
    assert reg.get("find_orders") is not None
    assert reg.get("nope") is None


def test_register_rejects_duplicate() -> None:
    reg = _registry()
    with pytest.raises(CapabilityError, match="→"):
        reg.register(find_orders)


def test_call_success_returns_result_as_data() -> None:
    res = _registry().call("find_orders", {"status": "processing"})
    assert res.ok
    assert res.value == ["order:processing"]


def test_call_unknown_capability_is_hinted_data() -> None:
    res = _registry().call("find_order", {})  # near-miss name
    assert not res.ok
    assert "→" in (res.error or "")


def test_call_missing_required_arg_is_hinted_data() -> None:
    res = _registry().call("find_orders", {})
    assert not res.ok
    assert "→" in (res.error or "")
    assert "status" in (res.error or "")


def test_call_wrong_type_is_hinted_data() -> None:
    res = _registry().call("find_orders", {"status": 123})
    assert not res.ok
    assert "→" in (res.error or "")


def test_call_unexpected_arg_is_rejected() -> None:
    res = _registry().call("find_orders", {"status": "x", "bogus": 1})
    assert not res.ok


def test_call_capability_that_raises_returns_error_data() -> None:
    @capability
    def boom(x: int) -> int:
        "Always fails."
        raise ValueError("kaboom")

    reg = CapabilityRegistry()
    reg.register(boom)
    res = reg.call("boom", {"x": 1})
    assert not res.ok
    assert "kaboom" in (res.error or "")


def test_validate_raises_typed_error_on_bad_args() -> None:
    with pytest.raises(CapabilityError, match="status"):
        _registry().validate("find_orders", {})


def test_extra_args_rejected_when_schema_omits_additional_properties() -> None:
    # Fail-closed: a schema without additionalProperties still rejects extras (§2.6).
    spec = Capability(
        name="probe",
        description="A hand-built capability.",
        input_schema={"type": "object", "properties": {"a": {"type": "string"}}},
    )
    reg = CapabilityRegistry()
    reg.register(BoundCapability(spec=spec, func=lambda a: a))
    res = reg.call("probe", {"a": "x", "extra": 1})
    assert not res.ok


def test_call_injects_context_into_a_ctx_parameter() -> None:
    from reins.types import Context

    seen: list[Context] = []

    @capability(reads=True)
    def whoami(ctx: Context) -> str:
        "Report the calling user."
        seen.append(ctx)
        return ctx.principal or "anonymous"

    reg = CapabilityRegistry()
    reg.register(whoami)
    result = reg.call("whoami", {}, context=Context(principal="user:7"))
    assert result.ok and result.value == "user:7"
    assert seen[0].principal == "user:7"


def test_call_without_context_injects_an_anonymous_context() -> None:
    from reins.types import Context

    @capability(reads=True)
    def whoami(context: Context) -> str | None:
        "Report the calling user."
        return context.principal

    reg = CapabilityRegistry()
    reg.register(whoami)
    result = reg.call("whoami", {})
    assert result.ok and result.value is None


def test_call_scoped_capability_without_principal_is_hinted_error() -> None:
    from reins.types import Context

    ran: list[str] = []

    @capability(reads=True, scope="user")
    def list_my_notes(ctx: Context) -> list[str]:
        "List the calling user's notes."
        ran.append("x")
        return []

    reg = CapabilityRegistry()
    reg.register(list_my_notes)
    result = reg.call("list_my_notes", {})
    assert result.ok is False
    assert ran == []  # fail closed: user code never ran
    assert result.error is not None and "→" in result.error


def test_injected_context_beats_a_model_supplied_context_argument() -> None:
    # The model must never be able to spoof identity (§2.7): even when a lax
    # schema lets a "ctx" argument through, the harness's context wins.
    from reins.types import Capability as CapSpec
    from reins.types import Context

    seen: list[Context] = []

    def whoami(ctx: Context) -> str:
        seen.append(ctx)
        return ctx.principal or "anonymous"

    spec = CapSpec(
        name="whoami",
        description="Report the calling user.",
        input_schema={"type": "object", "additionalProperties": True},
    )
    reg = CapabilityRegistry()
    reg.register(BoundCapability(spec=spec, func=whoami, context_param="ctx"))
    result = reg.call("whoami", {"ctx": "user:spoofed"}, context=Context(principal="user:real"))
    assert result.ok
    assert seen[0].principal == "user:real"
