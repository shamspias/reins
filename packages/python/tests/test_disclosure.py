"""Tests for dual progressive disclosure (M3.1): deferred capabilities behind
search, schema-RAG, the constant tiny base context, and the loop integration."""

import json

import pytest

from reins.capability import BoundCapability, capability
from reins.codec import to_dict
from reins.disclosure import (
    DEFER_AFTER,
    Disclosure,
    SchemaIndex,
    TableInfo,
)
from reins.errors import DisclosureError
from reins.loop import run_loop
from reins.model import FakeModel
from reins.registry import CapabilityRegistry
from reins.types import Access, Capability, ToolCall

# --- fixtures -----------------------------------------------------------------


def _stub(name: str, description: str | None = None) -> BoundCapability:
    spec = Capability(
        name=name,
        description=description or f"{name.replace('_', ' ').capitalize()}.",
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
        access=Access.READ,
    )
    return BoundCapability(spec=spec, func=lambda **kwargs: [])


def _registry(count: int, prefix: str = "find_thing") -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for i in range(count):
        reg.register(_stub(f"{prefix}_{i}"))
    return reg


def _search(disclosure: Disclosure, query: str) -> str:
    result = disclosure.answer(
        ToolCall(id="c1", name="search_capabilities", arguments={"query": query})
    )
    assert result is not None
    return result.value if result.ok else (result.error or "")


WIDE_SCHEMA = SchemaIndex(
    tables=(
        TableInfo("orders", "Customer orders.", ("id", "status", "total")),
        TableInfo("users", "Registered users.", ("id", "email", "name")),
        TableInfo("refunds", "Issued refunds.", ("id", "order_id", "amount")),
    )
)


# --- the tiny constant base context (invariant §2.12) ---------------------------


def test_below_threshold_everything_is_exposed() -> None:
    disclosure = Disclosure(_registry(DEFER_AFTER))
    assert not disclosure.deferred
    assert len(disclosure.tools()) == DEFER_AFTER


def test_above_threshold_only_the_search_surface_is_exposed() -> None:
    disclosure = Disclosure(_registry(DEFER_AFTER + 1))
    assert disclosure.deferred
    assert [t.name for t in disclosure.tools()] == ["search_capabilities"]


def test_base_context_is_byte_identical_at_any_scale() -> None:
    # 100 vs 1000 capabilities + a wide schema: the base surface must not grow.
    small = Disclosure(_registry(100), schema=WIDE_SCHEMA)
    large = Disclosure(_registry(1000), schema=WIDE_SCHEMA)
    assert to_dict(small.tools()) == to_dict(large.tools())


def test_base_context_stays_under_the_ceiling() -> None:
    disclosure = Disclosure(_registry(1000), schema=WIDE_SCHEMA)
    payload = json.dumps(to_dict(disclosure.tools()))
    assert len(payload) < 2000, "base context must stay tiny no matter the scale"


def test_defer_can_be_forced_on_and_off() -> None:
    assert Disclosure(_registry(2), defer=True).deferred
    assert not Disclosure(_registry(100), defer=False).deferred
    assert len(Disclosure(_registry(100), defer=False).tools()) == 100


# --- search_capabilities --------------------------------------------------------


def test_search_finds_by_keyword_and_exposes_the_match() -> None:
    reg = _registry(20)
    reg.register(_stub("refund_payment", "Refund a captured payment."))
    disclosure = Disclosure(reg)
    out = _search(disclosure, "refund payment")
    assert "refund_payment" in out
    assert "refund_payment" in [t.name for t in disclosure.tools()]


def test_search_matches_description_words_too() -> None:
    reg = _registry(20)
    reg.register(_stub("issue_credit", "Refund a customer by store credit."))
    assert "issue_credit" in _search(Disclosure(reg), "refund")


def test_search_results_are_bounded() -> None:
    disclosure = Disclosure(_registry(50, prefix="find_order"))
    out = _search(disclosure, "order")
    assert out.count("find_order") <= 8


def test_search_no_match_is_a_hinted_error() -> None:
    result = Disclosure(_registry(20)).answer(
        ToolCall(id="c1", name="search_capabilities", arguments={"query": "xyzzyplugh"})
    )
    assert result is not None and result.ok is False
    assert "→" in (result.error or "")


def test_search_rejects_a_missing_or_non_string_query() -> None:
    disclosure = Disclosure(_registry(20))
    for arguments in ({}, {"query": 5}, {"query": "   "}):
        result = disclosure.answer(
            ToolCall(id="c1", name="search_capabilities", arguments=arguments)
        )
        assert result is not None and result.ok is False
        assert "→" in (result.error or "")


def test_search_is_not_intercepted_when_not_deferred() -> None:
    disclosure = Disclosure(_registry(2))  # everything exposed; no discovery tool
    result = disclosure.answer(
        ToolCall(id="c1", name="search_capabilities", arguments={"query": "x"})
    )
    assert result is None


def test_meta_name_collision_fails_loudly() -> None:
    reg = _registry(DEFER_AFTER + 1)
    reg.register(_stub("search_capabilities", "A user capability with a reserved name."))
    with pytest.raises(DisclosureError, match="→"):
        Disclosure(reg)


# --- search_schema (schema-RAG) --------------------------------------------------


def test_schema_search_returns_only_relevant_tables() -> None:
    disclosure = Disclosure(_registry(2), schema=WIDE_SCHEMA)
    result = disclosure.answer(
        ToolCall(id="c1", name="search_schema", arguments={"query": "refunds"})
    )
    assert result is not None and result.ok
    assert "refunds" in result.value
    assert "users" not in result.value  # irrelevant tables never enter context


def test_schema_search_is_available_even_when_capabilities_are_not_deferred() -> None:
    disclosure = Disclosure(_registry(2), schema=WIDE_SCHEMA)
    assert "search_schema" in [t.name for t in disclosure.tools()]


def test_no_schema_means_no_schema_tool() -> None:
    disclosure = Disclosure(_registry(2))
    assert "search_schema" not in [t.name for t in disclosure.tools()]
    assert (
        disclosure.answer(ToolCall(id="c", name="search_schema", arguments={"query": "x"})) is None
    )


def test_schema_no_match_is_a_hinted_error() -> None:
    disclosure = Disclosure(_registry(2), schema=WIDE_SCHEMA)
    result = disclosure.answer(
        ToolCall(id="c1", name="search_schema", arguments={"query": "warehouses"})
    )
    assert result is not None and result.ok is False and "→" in (result.error or "")


def test_table_rendering_bounds_the_column_list() -> None:
    wide = SchemaIndex(tables=(TableInfo("big", "Wide table.", tuple(f"c{i}" for i in range(60))),))
    result = Disclosure(_registry(2), schema=wide).answer(
        ToolCall(id="c1", name="search_schema", arguments={"query": "big"})
    )
    assert result is not None and result.ok
    assert "…" in result.value
    assert result.value.count("c5") < 30  # not all 60 columns


# --- loop integration -------------------------------------------------------------


def _many_read_caps() -> list[BoundCapability]:
    caps = [_stub(f"find_thing_{i}") for i in range(DEFER_AFTER)]

    @capability(reads=True)
    def find_refunds(status: str = "issued") -> list[str]:
        "Find refunds by status."
        return ["r1", "r2", "r3"]

    caps.append(find_refunds)
    return caps


def test_model_discovers_and_uses_a_capability_by_search() -> None:
    reg = CapabilityRegistry()
    for cap in _many_read_caps():
        reg.register(cap)
    model = FakeModel.from_script(
        [
            {"call": "search_capabilities", "args": {"query": "refunds"}},
            {"call": "find_refunds", "args": {"status": "issued"}},
            {"final": "3 refunds."},
        ]
    )
    report = run_loop(model, reg, "how many refunds?", can_write=False)
    assert report.outcome == "completed"
    assert report.executed == ("find_refunds",)  # discovery is not a user capability
    # Turn 1: the model saw only the constant base surface (search + code-mode).
    assert [t.name for t in model.calls[0][1]] == ["search_capabilities", "run_program"]
    # Turn 2: the discovered capability is now directly callable.
    assert "find_refunds" in [t.name for t in model.calls[1][1]]


def test_discovery_needs_no_approval_and_no_write_access() -> None:
    prompts: list[str] = []

    def gate(request: object) -> bool:
        prompts.append("asked")
        return False

    reg = CapabilityRegistry()
    for cap in _many_read_caps():
        reg.register(cap)
    model = FakeModel.from_script(
        [
            {"call": "search_capabilities", "args": {"query": "refunds"}},
            {"final": "found them"},
        ]
    )
    report = run_loop(model, reg, "look around", can_write=True, approve=gate)
    assert report.outcome == "completed"
    assert prompts == []  # discovery is a harness-internal read: never gated
    assert report.audit == ()  # and never audited as a write attempt
