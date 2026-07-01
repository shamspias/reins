"""Tests for @capability introspection + classification (M1.3)."""

import pytest

from reins.capability import BoundCapability, capability
from reins.errors import CapabilityError
from reins.types import Access


def test_typed_function_becomes_clean_capability() -> None:
    @capability
    def find_orders(status: str, limit: int = 10) -> list[str]:
        """Find orders by status."""
        return []

    assert isinstance(find_orders, BoundCapability)
    spec = find_orders.spec
    assert spec.name == "find_orders"
    assert spec.description == "Find orders by status."
    assert spec.access == Access.READ
    props = spec.input_schema["properties"]
    assert props["status"] == {"type": "string"}
    assert props["limit"] == {"type": "integer"}
    assert spec.input_schema["required"] == ["status"]
    assert spec.input_schema["additionalProperties"] is False


def test_decorated_capability_is_still_callable() -> None:
    @capability
    def get_total(a: int, b: int) -> int:
        "Return the total."
        return a + b

    assert get_total(2, 3) == 5


def test_write_verb_classifies_as_write() -> None:
    @capability
    def create_order(item: str) -> None:
        "Create an order."

    assert create_order.spec.access == Access.WRITE


def test_destructive_verb_classifies_as_destructive() -> None:
    @capability
    def delete_order(order_id: int) -> None:
        "Delete an order."

    assert delete_order.spec.access == Access.DESTRUCTIVE


def test_ambiguous_verb_defaults_to_write() -> None:
    @capability
    def process_order(order_id: int) -> None:
        "Process an order."

    assert process_order.spec.access == Access.WRITE  # invariant §2.4


def test_explicit_override_beats_heuristic() -> None:
    @capability(reads=True)
    def sync_inventory() -> None:
        "Sync inventory (read-only snapshot)."

    assert sync_inventory.spec.access == Access.READ


def test_destructive_override_beats_read_verb() -> None:
    @capability(destructive=True)
    def find_and_purge(days: int) -> None:
        "Find stale records and purge them."

    assert find_and_purge.spec.access == Access.DESTRUCTIVE  # override beats 'find'


def test_reads_override_beats_destructive_verb() -> None:
    @capability(reads=True)
    def delete_preview(order_id: int) -> dict:
        "Preview what a delete would remove (read-only)."
        return {}

    assert delete_preview.spec.access == Access.READ  # override beats 'delete'


def test_conflicting_overrides_raise() -> None:
    with pytest.raises(CapabilityError, match="both"):

        @capability(reads=True, destructive=True)
        def confused() -> None:
            "Contradictory annotations."


def test_decorator_arguments_set_metadata() -> None:
    @capability(destructive=True, confirm=True, idempotent=True, scope="orders")
    def wipe_table(table: str) -> None:
        "Wipe a table."

    spec = wipe_table.spec
    assert spec.access == Access.DESTRUCTIVE
    assert spec.confirm is True
    assert spec.idempotent is True
    assert spec.scope == "orders"


def test_optional_param_is_not_required() -> None:
    @capability
    def find_users(active: bool | None = None) -> list[str]:
        "Find users, optionally filtered by active flag."
        return []

    assert find_users.spec.input_schema["required"] == []
    assert find_users.spec.input_schema["properties"]["active"] == {"type": "boolean"}


def test_missing_docstring_yields_empty_description() -> None:
    @capability
    def find_things() -> list[str]:
        return []

    assert find_things.spec.description == ""


def test_positional_only_parameter_is_rejected() -> None:
    with pytest.raises(CapabilityError, match="positional-only"):

        @capability
        def find_by(a: int, /, b: int = 0) -> int:
            "Bad signature for a capability."
            return a + b
