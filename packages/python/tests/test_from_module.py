"""Tests for Agent.from_module and the top-level reins.ask/run one-liners (M4.3)."""

import types

import pytest

import reins
from reins import Agent, capability
from reins.errors import ReinsError
from reins.model import FakeModel
from reins.types import Access


def _shop_module() -> types.ModuleType:
    """A plain module of functions — no decorators — mimicking a user's code."""
    module = types.ModuleType("shop_ops")

    def find_orders(status: str = "open") -> list[str]:
        "Find orders by status."
        return ["o1", "o2"]

    def delete_order(order_id: int) -> str:
        "Delete an order by id."
        return f"deleted {order_id}"

    helper_from_elsewhere = _imported_helper  # simulates `from x import y`
    _private = lambda: None  # noqa: E731 — a private, must be skipped

    for fn in (find_orders, delete_order):
        fn.__module__ = "shop_ops"  # as if defined in the module
    module.find_orders = find_orders
    module.delete_order = delete_order
    module.helper_from_elsewhere = helper_from_elsewhere
    module._private = _private
    module.CONSTANT = 42  # a non-callable, must be skipped
    return module


def _imported_helper() -> None:
    "A helper imported from elsewhere — must NOT be exposed."


def test_from_module_exposes_public_functions_defined_there() -> None:
    agent = Agent.from_module(_shop_module())
    names = {c.name for c in agent._registry.list()}  # type: ignore[attr-defined]
    assert names == {"find_orders", "delete_order"}


def test_from_module_classifies_by_verb() -> None:
    agent = Agent.from_module(_shop_module())
    caps = {c.name: c for c in agent._registry.list()}  # type: ignore[attr-defined]
    assert caps["find_orders"].access is Access.READ
    assert caps["delete_order"].access is Access.DESTRUCTIVE


def test_from_module_skips_imports_privates_and_non_callables() -> None:
    agent = Agent.from_module(_shop_module())
    names = {c.name for c in agent._registry.list()}  # type: ignore[attr-defined]
    assert "helper_from_elsewhere" not in names  # imported, not defined here
    assert "_private" not in names
    assert "CONSTANT" not in names


def test_from_module_include_and_exclude() -> None:
    only = Agent.from_module(_shop_module(), include=["find_orders"])
    assert {c.name for c in only._registry.list()} == {"find_orders"}  # type: ignore[attr-defined]
    without = Agent.from_module(_shop_module(), exclude=["delete_order"])
    assert {c.name for c in without._registry.list()} == {"find_orders"}  # type: ignore[attr-defined]


def test_from_module_takes_predecorated_capabilities() -> None:
    module = types.ModuleType("decorated_ops")

    @capability(reads=True)
    def lookup_widget(sku: str) -> str:
        "Look up a widget."
        return sku

    module.lookup_widget = lookup_widget
    agent = Agent.from_module(module)
    (spec,) = agent._registry.list()  # type: ignore[attr-defined]
    assert spec.name == "lookup_widget" and spec.access is Access.READ


def test_from_module_empty_is_a_hinted_error() -> None:
    with pytest.raises(ReinsError, match="→"):
        Agent.from_module(types.ModuleType("empty_ops"))


def test_from_module_runs_end_to_end() -> None:
    module = _shop_module()
    model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "2 open"}]
    )
    agent = Agent.from_module(module, model=model)
    assert agent.ask("how many open orders?").output.text == "2 open"


# --- reins.ask / reins.run one-liners ---------------------------------------------


def test_module_level_ask_with_a_module() -> None:
    module = _shop_module()
    model = FakeModel.from_script([{"call": "find_orders", "args": {}}, {"final": "done"}])
    result = reins.ask("list orders", using=module, model=model)
    assert result.output.text == "done"


def test_module_level_run_with_a_capability_list() -> None:
    @capability(reads=True)
    def count_widgets() -> int:
        "Count widgets."
        return 3

    model = FakeModel.from_script([{"call": "count_widgets", "args": {}}, {"final": "3"}])
    result = reins.run("count widgets", using=[count_widgets], model=model)
    assert result.output.text == "3"


def test_module_level_ask_accepts_an_existing_agent() -> None:
    model = FakeModel.from_script([{"final": "hi"}])
    agent = Agent(model=model, capabilities=[])
    assert reins.ask("say hi", using=agent).output.text == "hi"


def test_options_alongside_an_existing_agent_is_an_error() -> None:
    agent = Agent(model=FakeModel.from_script([{"final": "x"}]))
    with pytest.raises(TypeError, match="→"):
        reins.ask("x", using=agent, autonomy=None)  # type: ignore[arg-type]
