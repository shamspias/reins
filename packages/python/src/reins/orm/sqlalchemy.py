"""SQLAlchemy auto-expose (M4.1).

Introspect mapped models and emit **intent-level** capabilities — never raw CRUD:

- reads (always, safe): ``find_<plural>``, ``count_<plural>``, and
  ``get_<singular>_by_<key>`` when a natural key exists;
- writes (only with ``can_write=True``, gated by the autonomy ladder):
  ``create_<singular>``, ``update_<singular>``, ``delete_<singular>``.

Names are verb-first (all ≥ 4 chars) and lookups prefer a **natural key**
(email / slug / name) over an internal integer id, so every generated
capability passes the Capability Linter (invariant §2.15) — asserted at build
time, never exposed otherwise. Responses are trimmed (bounded rows, bounded
columns) so raw tables never flood the model's context, and a
:class:`reins.disclosure.SchemaIndex` is produced for progressive disclosure.

SQLAlchemy is an optional dependency — install ``reins[sqlalchemy]``; the import
is lazy so the core never requires it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from reins.capability import BoundCapability
from reins.disclosure import SchemaIndex, TableInfo
from reins.errors import ReinsError
from reins.linter import CapabilityLinter
from reins.types import Access, Capability

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_PARAMS = 7  # keep generated capabilities lean (linter §2.15)
MAX_COLUMNS_RETURNED = 12  # trim wide rows so raw tables never flood context
DEFAULT_READ_LIMIT = 50  # bounded result sets (§2.13)
_MAX_VALUE_CHARS = 500  # trim very long text fields in responses

# Column names commonly used as human-facing natural keys, best first.
_NATURAL_KEY_HINTS = ("slug", "email", "username", "code", "key", "name", "title", "uuid")


class ORMError(ReinsError):
    """The ORM adapter cannot introspect or expose a model as configured."""


def build_orm(
    db: Any,
    *,
    models: Iterable[Any] | None = None,
    can_write: bool = False,
    read_limit: int = DEFAULT_READ_LIMIT,
) -> tuple[list[BoundCapability], SchemaIndex]:
    """Introspect ``models`` and return ``(capabilities, schema_index)``.

    ``db`` is a SQLAlchemy ``Session``, ``sessionmaker``/session factory, or
    ``Engine``; ``models`` is the mapped classes (or a declarative base to pull
    them from). Reads are always generated; writes only when ``can_write=True``.
    Every capability is linted before it is returned — a generated capability
    that would fail the linter is a bug, raised here, not shipped to the model.
    """
    session_factory = _session_factory(db)
    resolved = _resolve_models(models)
    if not resolved:
        raise ORMError(
            "no mapped models to expose",
            hint="pass models=[User, Order, ...] or a declarative Base to from_orm(...)",
        )
    linter = CapabilityLinter()
    capabilities: list[BoundCapability] = []
    tables: list[TableInfo] = []
    for model in resolved:
        info = _introspect(model)
        candidates = list(_read_capabilities(session_factory, info, read_limit))
        if can_write:
            candidates.extend(_write_capabilities(session_factory, info))
        # Never expose a capability that fails the linter (§2.15). The verb-first
        # names (find_/count_/create_/update_/delete_) always pass; a get_ whose
        # entity + key name is too short is dropped — find_ still covers lookup.
        capabilities.extend(c for c in candidates if linter.lint_capability(c.spec).ok)
        tables.append(_table_info(info))
    return capabilities, SchemaIndex(tables=tuple(tables))


# --- introspection --------------------------------------------------------------


class _ModelInfo:
    """The introspected shape of one mapped model, in Reins terms."""

    def __init__(
        self,
        model: type,
        singular: str,
        plural: str,
        columns: list[_Column],
        primary_key: _Column | None,
        natural_key: _Column | None,
    ) -> None:
        self.model = model
        self.singular = singular
        self.plural = plural
        self.columns = columns
        self.primary_key = primary_key
        self.natural_key = natural_key

    @property
    def lookup_key(self) -> _Column | None:
        """The column to look a single row up by — natural key preferred over the id."""
        return self.natural_key or self.primary_key


class _Column:
    def __init__(
        self,
        name: str,
        json_type: str,
        nullable: bool,
        autoincrement: bool,
        has_default: bool,
    ) -> None:
        self.name = name
        self.json_type = json_type
        self.nullable = nullable
        self.autoincrement = autoincrement
        self.has_default = has_default

    @property
    def optional_on_create(self) -> bool:
        """A column the caller may omit when creating a row."""
        return self.nullable or self.autoincrement or self.has_default


def _introspect(model: type) -> _ModelInfo:
    from sqlalchemy import inspect as sa_inspect

    mapper: Any = sa_inspect(model)
    columns: list[_Column] = []
    primary_key: _Column | None = None
    for column in mapper.columns:
        json_type = _json_type(column)
        # An integer single-column PK with autoincrement True/"auto" (SQLAlchemy's
        # default) is DB-assigned — never a create/update parameter.
        autoincrement = bool(
            column.primary_key and json_type == "integer" and column.autoincrement in (True, "auto")
        )
        col = _Column(
            name=column.key,
            json_type=json_type,
            nullable=bool(column.nullable),
            autoincrement=autoincrement,
            # A DB- or Python-side default means the caller may omit it on insert.
            has_default=column.default is not None or column.server_default is not None,
        )
        columns.append(col)
        if column.primary_key and primary_key is None:
            primary_key = col
    singular = _snake(model.__name__)
    plural = getattr(model, "__tablename__", None) or _pluralize(singular)
    return _ModelInfo(
        model=model,
        singular=singular,
        plural=str(plural),
        columns=columns,
        primary_key=primary_key,
        natural_key=_natural_key(model, columns, primary_key),
    )


def _natural_key(model: type, columns: list[_Column], pk: _Column | None) -> _Column | None:
    from sqlalchemy import inspect as sa_inspect

    by_name = {c.name: c for c in columns}
    unique_names = _unique_column_names(sa_inspect(model))
    # A unique, string-typed, non-PK column whose name reads like a natural key.
    for hint in _NATURAL_KEY_HINTS:
        col = by_name.get(hint)
        if col is not None and col.json_type == "string" and (pk is None or col.name != pk.name):
            return col
    for name in unique_names:
        col = by_name.get(name)
        if col is not None and col.json_type == "string" and (pk is None or name != pk.name):
            return col
    return None


def _unique_column_names(mapper: Any) -> list[str]:
    names: list[str] = []
    for column in mapper.columns:
        if column.unique:
            names.append(column.key)
    return names


# --- read capabilities ----------------------------------------------------------


def _read_capabilities(
    session_factory: Callable[[], Any], info: _ModelInfo, read_limit: int
) -> list[BoundCapability]:
    caps = [_find_cap(session_factory, info, read_limit), _count_cap(session_factory, info)]
    if info.lookup_key is not None:
        caps.append(_get_cap(session_factory, info))
    return caps


def _find_cap(
    session_factory: Callable[[], Any], info: _ModelInfo, read_limit: int
) -> BoundCapability:
    filters = _filter_columns(info)
    schema = _object_schema({c.name: c.json_type for c in filters}, required=[])
    spec = Capability(
        name=f"find_{info.plural}",
        description=f"Find {info.plural} matching optional filters (returns up to "
        f"{read_limit} rows, trimmed).",
        input_schema=schema,
        access=Access.READ,
    )

    def run(**kwargs: Any) -> list[dict[str, Any]]:
        provided = _known(kwargs, filters)
        with session_factory() as session:
            rows = _select_rows(session, info, provided, limit=read_limit)
            return [_trim_row(info, row) for row in rows]

    return BoundCapability(spec=spec, func=run)


def _count_cap(session_factory: Callable[[], Any], info: _ModelInfo) -> BoundCapability:
    filters = _filter_columns(info)
    schema = _object_schema({c.name: c.json_type for c in filters}, required=[])
    spec = Capability(
        name=f"count_{info.plural}",
        description=f"Count {info.plural} matching optional filters.",
        input_schema=schema,
        access=Access.READ,
    )

    def run(**kwargs: Any) -> int:
        provided = _known(kwargs, filters)
        with session_factory() as session:
            return _count_rows(session, info, provided)

    return BoundCapability(spec=spec, func=run)


def _get_cap(session_factory: Callable[[], Any], info: _ModelInfo) -> BoundCapability:
    key = info.lookup_key
    assert key is not None
    spec = Capability(
        name=f"get_{info.singular}_by_{key.name}",
        description=f"Get one {info.singular} by its {key.name} (returns the row, trimmed, "
        "or nothing if not found).",
        input_schema=_object_schema({key.name: key.json_type}, required=[key.name]),
        access=Access.READ,
    )

    def run(**kwargs: Any) -> dict[str, Any] | None:
        with session_factory() as session:
            rows = _select_rows(session, info, {key.name: kwargs[key.name]}, limit=1)
            return _trim_row(info, rows[0]) if rows else None

    return BoundCapability(spec=spec, func=run)


# --- write capabilities (gated) --------------------------------------------------


def _write_capabilities(
    session_factory: Callable[[], Any], info: _ModelInfo
) -> list[BoundCapability]:
    caps = [_create_cap(session_factory, info)]
    if info.lookup_key is not None:
        caps.append(_update_cap(session_factory, info))
        caps.append(_delete_cap(session_factory, info))
    return caps


def _create_cap(session_factory: Callable[[], Any], info: _ModelInfo) -> BoundCapability:
    settable = _settable_columns(info)
    required = [c.name for c in settable if not c.optional_on_create]
    spec = Capability(
        name=f"create_{info.singular}",
        description=f"Create a new {info.singular} (returns the created row, trimmed).",
        input_schema=_object_schema({c.name: c.json_type for c in settable}, required=required),
        access=Access.WRITE,
    )

    def run(**kwargs: Any) -> dict[str, Any]:
        values = _known(kwargs, settable)
        with session_factory() as session:
            obj = info.model(**values)
            session.add(obj)
            session.commit()
            return _trim_row(info, obj)

    return BoundCapability(spec=spec, func=run)


def _update_cap(session_factory: Callable[[], Any], info: _ModelInfo) -> BoundCapability:
    key = info.lookup_key
    assert key is not None
    # The key is a required param, so leave room for it under the MAX_PARAMS cap.
    non_key = [c for c in info.columns if not c.autoincrement and c.name != key.name]
    settable = _prioritize(info, non_key)[: MAX_PARAMS - 1]
    props = {key.name: key.json_type, **{c.name: c.json_type for c in settable}}
    spec = Capability(
        name=f"update_{info.singular}",
        description=f"Update an existing {info.singular}, found by its {key.name} "
        "(returns the updated row, trimmed).",
        input_schema=_object_schema(props, required=[key.name]),
        access=Access.WRITE,
    )

    def run(**kwargs: Any) -> dict[str, Any]:
        changes = _known(kwargs, settable)
        with session_factory() as session:
            rows = _select_rows(session, info, {key.name: kwargs[key.name]}, limit=1)
            if not rows:
                raise ORMError(
                    f"no {info.singular} with {key.name}={kwargs[key.name]!r}",
                    hint="check the key, or find_ the row first",
                )
            for field, value in changes.items():
                setattr(rows[0], field, value)
            session.commit()
            return _trim_row(info, rows[0])

    return BoundCapability(spec=spec, func=run)


def _delete_cap(session_factory: Callable[[], Any], info: _ModelInfo) -> BoundCapability:
    key = info.lookup_key
    assert key is not None
    spec = Capability(
        name=f"delete_{info.singular}",
        description=f"Delete a {info.singular}, found by its {key.name}.",
        input_schema=_object_schema({key.name: key.json_type}, required=[key.name]),
        access=Access.DESTRUCTIVE,
    )

    def run(**kwargs: Any) -> dict[str, Any]:
        with session_factory() as session:
            rows = _select_rows(session, info, {key.name: kwargs[key.name]}, limit=1)
            if not rows:
                raise ORMError(
                    f"no {info.singular} with {key.name}={kwargs[key.name]!r}",
                    hint="check the key, or find_ the row first",
                )
            session.delete(rows[0])
            session.commit()
            return {"deleted": kwargs[key.name]}

    return BoundCapability(spec=spec, func=run)


# --- query helpers ---------------------------------------------------------------


def _select_rows(
    session: Any, info: _ModelInfo, filters: dict[str, Any], *, limit: int
) -> list[Any]:
    from sqlalchemy import select

    stmt: Any = select(info.model).filter_by(**filters).limit(limit)
    return list(session.execute(stmt).scalars().all())


def _count_rows(session: Any, info: _ModelInfo, filters: dict[str, Any]) -> int:
    from sqlalchemy import func, select

    stmt = select(func.count()).select_from(info.model).filter_by(**filters)
    return int(session.execute(stmt).scalar_one())


def _trim_row(info: _ModelInfo, row: Any) -> dict[str, Any]:
    """A bounded, JSON-safe view of a row (never a raw column dump — linter §2.15)."""
    out: dict[str, Any] = {}
    for column in info.columns[:MAX_COLUMNS_RETURNED]:
        out[column.name] = _json_value(getattr(row, column.name, None))
    return out


def _json_value(value: Any) -> Any:
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):  # date & datetime
        return value.isoformat()
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[:_MAX_VALUE_CHARS] + "…"


# --- column selection ------------------------------------------------------------


def _filter_columns(info: _ModelInfo) -> list[_Column]:
    """Columns exposed as optional find/count filters — lean, capped at MAX_PARAMS."""
    return _prioritize(info, info.columns)[:MAX_PARAMS]


def _settable_columns(info: _ModelInfo) -> list[_Column]:
    """Columns a create/update may set — no autoincrement PK — capped at MAX_PARAMS."""
    settable = [c for c in info.columns if not c.autoincrement]
    return _prioritize(info, settable)[:MAX_PARAMS]


def _prioritize(info: _ModelInfo, columns: list[_Column]) -> list[_Column]:
    """Order columns so the most useful survive the MAX_PARAMS cap."""
    key_names = {c.name for c in (info.natural_key, info.primary_key) if c is not None}

    def rank(column: _Column) -> tuple[int, str]:
        if column.name in key_names:
            return (0, column.name)
        if not column.nullable:
            return (1, column.name)
        return (2, column.name)

    return sorted(columns, key=rank)


def _known(kwargs: dict[str, Any], columns: list[_Column]) -> dict[str, Any]:
    """Keep only recognized, provided arguments (untrusted model output, §2.6)."""
    allowed = {c.name for c in columns}
    return {k: v for k, v in kwargs.items() if k in allowed and v is not None}


# --- schema index for disclosure -------------------------------------------------


def _table_info(info: _ModelInfo) -> TableInfo:
    doc = (info.model.__doc__ or "").strip().splitlines()
    description = doc[0].strip() if doc else f"The {info.plural} table."
    return TableInfo(
        name=info.plural,
        description=description,
        columns=tuple(c.name for c in info.columns),
    )


def _object_schema(properties: dict[str, str], *, required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": jtype} for name, jtype in properties.items()},
        "required": required,
        "additionalProperties": False,
    }


# --- session resolution ----------------------------------------------------------


def _session_factory(db: Any) -> Callable[[], Any]:
    """Return a zero-arg factory yielding a context-managed :class:`Session`."""
    try:
        from sqlalchemy.engine import Engine
        from sqlalchemy.orm import Session, sessionmaker
    except ModuleNotFoundError as exc:
        raise ORMError(
            "the 'sqlalchemy' package is required for Agent.from_orm",
            hint="install it with: pip install 'reins[sqlalchemy]'",
        ) from exc

    if isinstance(db, Session):
        # An existing session: reuse it, and don't close it out from under the caller.
        @contextmanager
        def borrow() -> Iterator[Any]:
            yield db

        return borrow

    if isinstance(db, Engine):
        maker = sessionmaker(bind=db)
        return maker

    if callable(db):  # a sessionmaker or any session factory
        factory: Callable[[], Any] = db
        return factory

    raise ORMError(
        f"cannot make sessions from {type(db).__name__}",
        hint="pass a SQLAlchemy Session, sessionmaker, session factory, or Engine",
    )


def _resolve_models(models: Iterable[Any] | None) -> list[type]:
    if models is None:
        raise ORMError(
            "no models given",
            hint="pass models=[User, Order, ...] or a declarative Base",
        )
    # A declarative base: pull its mapped classes from the registry.
    registry = getattr(models, "registry", None)
    if registry is not None and hasattr(registry, "mappers"):
        return [mapper.class_ for mapper in registry.mappers]
    return list(models)


# --- JSON type mapping -----------------------------------------------------------

_PY_TO_JSON = {bool: "boolean", int: "integer", float: "number", str: "string"}


def _json_type(column: Any) -> str:
    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return "string"
    if python_type is Decimal:
        return "number"
    if isinstance(python_type, type) and issubclass(python_type, date):
        return "string"  # ISO-8601
    return _PY_TO_JSON.get(python_type, "string")


# --- name helpers ----------------------------------------------------------------


def _snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _pluralize(word: str) -> str:
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"
