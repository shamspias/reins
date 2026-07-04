"""Tests for the SQLAlchemy auto-expose adapter (M4.1): intent-level capabilities
generated from mapped models, all linter-clean, reads safe, writes gated."""

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Engine, String, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from reins.approval import ApprovalRequest
from reins.linter import CapabilityLinter
from reins.loop import Agent, run_loop
from reins.model import FakeModel
from reins.orm import build_orm
from reins.orm.sqlalchemy import ORMError
from reins.registry import CapabilityRegistry
from reins.types import Access


class Base(DeclarativeBase):
    pass


class User(Base):
    """A registered user of the app."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(default=True)


class Order(Base):
    """A customer order."""

    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String)
    total: Mapped[int] = mapped_column(default=0)


@pytest.fixture
def engine() -> Engine:
    eng = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add_all(
            [
                User(email="alice@x.com", name="Alice", active=True),
                User(email="bob@x.com", name="Bob", active=False),
                Order(status="open", total=100),
                Order(status="closed", total=50),
                Order(status="open", total=25),
            ]
        )
        s.commit()
    return eng


def _names(caps: list) -> set[str]:
    return {c.spec.name for c in caps}


def _by_name(caps: list, name: str):
    return next(c for c in caps if c.spec.name == name)


def _registry(caps: list) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for cap in caps:
        reg.register(cap)
    return reg


# --- what gets generated ----------------------------------------------------------


def test_reads_are_generated_with_intent_names(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User, Order])
    names = _names(caps)
    assert {"find_users", "count_users", "get_user_by_email"} <= names
    assert {"find_orders", "count_orders", "get_order_by_id"} <= names


def test_no_write_capabilities_without_can_write(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User, Order])
    for cap in caps:
        assert cap.spec.access is Access.READ
    assert not any(n.startswith(("create_", "update_", "delete_")) for n in _names(caps))


def test_writes_appear_only_with_can_write(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User], can_write=True)
    names = _names(caps)
    assert {"create_user", "update_user", "delete_user"} <= names
    assert _by_name(caps, "create_user").spec.access is Access.WRITE
    assert _by_name(caps, "delete_user").spec.access is Access.DESTRUCTIVE


def test_every_generated_capability_passes_the_linter(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User, Order], can_write=True)
    linter = CapabilityLinter()
    assert caps
    for cap in caps:
        assert linter.lint_capability(cap.spec).ok, cap.spec.name


def test_natural_key_preferred_over_internal_id(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User], can_write=True)
    # User.email is unique + string → the lookup key, not the internal id.
    assert "get_user_by_email" in _names(caps)
    assert "id" not in _by_name(caps, "get_user_by_email").spec.input_schema["properties"]
    # Order has no natural key → falls back to the primary key.
    order_caps, _ = build_orm(engine, models=[Order])
    assert "get_order_by_id" in _names(order_caps)


def test_create_omits_the_autoincrement_primary_key(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User], can_write=True)
    props = _by_name(caps, "create_user").spec.input_schema["properties"]
    assert "id" not in props  # DB-assigned, never a parameter
    assert {"email", "name"} <= set(props)


def test_capabilities_stay_lean(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User, Order], can_write=True)
    for cap in caps:
        assert len(cap.spec.input_schema.get("properties", {})) <= 7


# --- schema index -----------------------------------------------------------------


def test_schema_index_describes_each_table(engine: Engine) -> None:
    _, schema = build_orm(engine, models=[User, Order])
    tables = {t.name: t for t in schema.tables}
    assert set(tables) == {"users", "orders"}
    assert "registered user" in tables["users"].description.lower()
    assert set(tables["users"].columns) == {"id", "email", "name", "active"}


# --- functional round-trips through the registry ----------------------------------


def test_find_returns_trimmed_rows(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User])
    reg = _registry(caps)
    result = reg.call("find_users", {"active": True})
    assert result.ok
    assert [r["email"] for r in result.value] == ["alice@x.com"]
    assert isinstance(result.value[0]["active"], bool)


def test_count_with_filter(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[Order])
    reg = _registry(caps)
    assert reg.call("count_orders", {"status": "open"}).value == 2
    assert reg.call("count_orders", {}).value == 3


def test_get_by_natural_key(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User])
    reg = _registry(caps)
    hit = reg.call("get_user_by_email", {"email": "bob@x.com"})
    assert hit.ok and hit.value["name"] == "Bob"
    miss = reg.call("get_user_by_email", {"email": "nobody@x.com"})
    assert miss.ok and miss.value is None


def test_read_limit_bounds_results(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[Order], read_limit=2)
    reg = _registry(caps)
    assert len(reg.call("find_orders", {}).value) == 2


def test_create_update_delete_round_trip(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User], can_write=True)
    reg = _registry(caps)
    created = reg.call("create_user", {"email": "carol@x.com", "name": "Carol"})
    assert created.ok and created.value["email"] == "carol@x.com"
    updated = reg.call("update_user", {"email": "carol@x.com", "name": "Caroline"})
    assert updated.ok and updated.value["name"] == "Caroline"
    deleted = reg.call("delete_user", {"email": "carol@x.com"})
    assert deleted.ok and deleted.value == {"deleted": "carol@x.com"}
    assert reg.call("count_users", {"email": "carol@x.com"}).value == 0


def test_update_missing_row_is_error_as_data(engine: Engine) -> None:
    caps, _ = build_orm(engine, models=[User], can_write=True)
    reg = _registry(caps)
    result = reg.call("update_user", {"email": "ghost@x.com", "name": "X"})
    assert result.ok is False
    assert "→" in (result.error or "")


# --- session sources --------------------------------------------------------------


def test_accepts_engine_sessionmaker_and_session(engine: Engine) -> None:
    for db in (engine, sessionmaker(bind=engine), Session(engine)):
        caps, _ = build_orm(db, models=[User])
        assert _registry(caps).call("count_users", {}).value == 2


def test_bad_db_is_a_hinted_error() -> None:
    with pytest.raises(ORMError, match="→"):
        build_orm(object(), models=[User])


def test_no_models_is_a_hinted_error(engine: Engine) -> None:
    with pytest.raises(ORMError, match="→"):
        build_orm(engine, models=[])


def test_declarative_base_discovers_models(engine: Engine) -> None:
    _caps, schema = build_orm(engine, models=Base)
    assert {"users", "orders"} <= {t.name for t in schema.tables}


# --- end-to-end through the Agent -------------------------------------------------


def test_from_orm_read_answer(engine: Engine) -> None:
    model = FakeModel.from_script(
        [{"call": "count_orders", "args": {"status": "open"}}, {"final": "2 open orders."}]
    )
    agent = Agent.from_orm(engine, models=[Order], model=model)
    result = agent.ask("how many open orders?")
    assert result.output.text == "2 open orders."


def test_from_orm_read_only_cannot_write(engine: Engine) -> None:
    # can_write defaults False: no write capability exists, so run() cannot create.
    model = FakeModel.from_script(
        [{"call": "create_user", "args": {"email": "x@x.com", "name": "X"}}, {"final": "cannot"}]
    )
    agent = Agent.from_orm(engine, models=[User], model=model)
    report = run_loop(agent._model, agent._registry, "make a user", can_write=True)  # type: ignore[arg-type]
    tool_texts = [m.text or "" for msgs, _ in model.calls for m in msgs if m.role.value == "tool"]
    assert any("no capability named 'create_user'" in t for t in tool_texts)
    assert report.executed == ()


def test_from_orm_write_is_gated(engine: Engine) -> None:
    gate_requests: list[ApprovalRequest] = []

    def gate(request: ApprovalRequest) -> bool:
        gate_requests.append(request)
        return True

    model = FakeModel.from_script(
        [
            {"call": "create_user", "args": {"email": "dave@x.com", "name": "Dave"}},
            {"final": "created Dave"},
        ]
    )
    agent = Agent.from_orm(engine, models=[User], can_write=True, model=model, approve=gate)
    result = agent.run("add user Dave with email dave@x.com")
    assert result.output.text == "created Dave"
    assert len(gate_requests) == 1  # the write paused for approval
    assert "create_user" in gate_requests[0].preview()
