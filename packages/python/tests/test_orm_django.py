"""Tests for the Django ORM auto-expose adapter (M4.2): the same intent-level,
linter-clean capabilities the SQLAlchemy backend produces, over the Django ORM."""

from collections.abc import Iterator

import pytest

pytest.importorskip("django")

from django.conf import settings

if not settings.configured:
    settings.configure(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        INSTALLED_APPS=["django.contrib.contenttypes"],
        DEFAULT_AUTO_FIELD="django.db.models.AutoField",
        USE_TZ=True,
    )

import django

django.setup()

from django.db import connection, models

from reins.linter import CapabilityLinter
from reins.loop import Agent, run_loop
from reins.model import FakeModel
from reins.orm import build_orm
from reins.registry import CapabilityRegistry
from reins.types import Access


class Customer(models.Model):
    """A customer of the shop."""

    email = models.CharField(max_length=200, unique=True)
    name = models.CharField(max_length=200)
    tier = models.CharField(max_length=50, default="free")

    class Meta:
        app_label = "contenttypes"


class Note(models.Model):
    """A free-floating note with no natural key."""

    body = models.TextField()

    class Meta:
        app_label = "contenttypes"


@pytest.fixture(autouse=True)
def _database() -> Iterator[None]:
    with connection.schema_editor() as editor:
        editor.create_model(Customer)
        editor.create_model(Note)
    Customer.objects.create(email="a@x.com", name="Ada", tier="pro")
    Customer.objects.create(email="b@x.com", name="Ben", tier="free")
    Note.objects.create(body="first")
    try:
        yield
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(Customer)
            editor.delete_model(Note)


def _names(caps: list) -> set[str]:
    return {c.spec.name for c in caps}


def _by_name(caps: list, name: str):
    return next(c for c in caps if c.spec.name == name)


def _registry(caps: list) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for cap in caps:
        reg.register(cap)
    return reg


# --- what gets generated (routed through the shared dispatcher) -------------------


def test_dispatch_routes_django_models_to_the_django_backend() -> None:
    caps, _ = build_orm(models=[Customer])  # no db needed for Django
    assert {"find_customers", "count_customers", "get_customer_by_email"} <= _names(caps)


def test_note_without_natural_key_falls_back_to_pk() -> None:
    caps, _ = build_orm(models=[Note])
    assert "get_note_by_id" in _names(caps)


def test_no_writes_without_can_write() -> None:
    caps, _ = build_orm(models=[Customer])
    assert all(c.spec.access is Access.READ for c in caps)
    assert not any(n.startswith(("create_", "update_", "delete_")) for n in _names(caps))


def test_writes_appear_only_with_can_write() -> None:
    caps, _ = build_orm(models=[Customer], can_write=True)
    assert {"create_customer", "update_customer", "delete_customer"} <= _names(caps)
    assert _by_name(caps, "delete_customer").spec.access is Access.DESTRUCTIVE


def test_every_generated_capability_passes_the_linter() -> None:
    caps, _ = build_orm(models=[Customer, Note], can_write=True)
    linter = CapabilityLinter()
    assert caps
    for cap in caps:
        assert linter.lint_capability(cap.spec).ok, cap.spec.name


def test_natural_key_preferred_over_internal_id() -> None:
    caps, _ = build_orm(models=[Customer])
    assert "get_customer_by_email" in _names(caps)
    assert "id" not in _by_name(caps, "get_customer_by_email").spec.input_schema["properties"]


def test_create_omits_autoincrement_pk_and_defaulted_column() -> None:
    caps, _ = build_orm(models=[Customer], can_write=True)
    create = _by_name(caps, "create_customer").spec.input_schema
    assert "id" not in create["properties"]  # DB-assigned
    assert "tier" in create["properties"]
    assert set(create["required"]) == {"email", "name"}  # tier has a default → optional


# --- schema index -----------------------------------------------------------------


def test_schema_index_describes_each_table() -> None:
    _, schema = build_orm(models=[Customer, Note])
    tables = {t.name: t for t in schema.tables}
    assert set(tables) == {"customers", "notes"}
    assert "customer of the shop" in tables["customers"].description.lower()
    assert set(tables["customers"].columns) == {"id", "email", "name", "tier"}


# --- functional round-trips through the registry ----------------------------------


def test_find_and_count_with_filters() -> None:
    caps, _ = build_orm(models=[Customer])
    reg = _registry(caps)
    pro = reg.call("find_customers", {"tier": "pro"})
    assert pro.ok and [r["email"] for r in pro.value] == ["a@x.com"]
    assert reg.call("count_customers", {}).value == 2
    assert reg.call("count_customers", {"tier": "free"}).value == 1


def test_get_by_natural_key() -> None:
    caps, _ = build_orm(models=[Customer])
    reg = _registry(caps)
    hit = reg.call("get_customer_by_email", {"email": "b@x.com"})
    assert hit.ok and hit.value["name"] == "Ben"
    miss = reg.call("get_customer_by_email", {"email": "nobody@x.com"})
    assert miss.ok and miss.value is None


def test_create_update_delete_round_trip() -> None:
    caps, _ = build_orm(models=[Customer], can_write=True)
    reg = _registry(caps)
    created = reg.call("create_customer", {"email": "c@x.com", "name": "Cid"})
    assert created.ok and created.value["email"] == "c@x.com"
    assert created.value["tier"] == "free"  # model default applied
    updated = reg.call("update_customer", {"email": "c@x.com", "tier": "pro"})
    assert updated.ok and updated.value["tier"] == "pro"
    deleted = reg.call("delete_customer", {"email": "c@x.com"})
    assert deleted.ok and deleted.value == {"deleted": "c@x.com"}
    assert reg.call("count_customers", {"email": "c@x.com"}).value == 0


def test_update_missing_row_is_error_as_data() -> None:
    caps, _ = build_orm(models=[Customer], can_write=True)
    reg = _registry(caps)
    result = reg.call("update_customer", {"email": "ghost@x.com", "name": "X"})
    assert result.ok is False and "→" in (result.error or "")


def test_app_label_resolves_models() -> None:
    # An app label pulls every model registered under that app.
    _caps, schema = build_orm(models="contenttypes")
    names = {t.name for t in schema.tables}
    assert {"customers", "notes"} <= names


# --- end-to-end through the Agent -------------------------------------------------


def test_from_orm_needs_no_db_for_django() -> None:
    model = FakeModel.from_script(
        [{"call": "count_customers", "args": {"tier": "pro"}}, {"final": "1 pro customer."}]
    )
    agent = Agent.from_orm(models=[Customer], model=model)
    assert agent.ask("how many pro customers?").output.text == "1 pro customer."


def test_from_orm_read_only_cannot_write() -> None:
    model = FakeModel.from_script(
        [{"call": "create_customer", "args": {"email": "z@x.com", "name": "Z"}}, {"final": "no"}]
    )
    agent = Agent.from_orm(models=[Customer], model=model)
    report = run_loop(agent._model, agent._registry, "make one", can_write=True)  # type: ignore[arg-type]
    tool_texts = [m.text or "" for msgs, _ in model.calls for m in msgs if m.role.value == "tool"]
    assert any("no capability named 'create_customer'" in t for t in tool_texts)
    assert report.executed == ()


def test_from_orm_write_is_gated_and_executes_when_approved() -> None:
    approvals: list[str] = []
    model = FakeModel.from_script(
        [
            {"call": "create_customer", "args": {"email": "dave@x.com", "name": "Dave"}},
            {"final": "created Dave"},
        ]
    )
    agent = Agent.from_orm(
        models=[Customer],
        can_write=True,
        model=model,
        approve=lambda r: approvals.append("asked") or True,
    )
    result = agent.run("add customer Dave dave@x.com")
    assert result.output.text == "created Dave"
    assert approvals == ["asked"]
    assert Customer.objects.filter(email="dave@x.com").exists()
