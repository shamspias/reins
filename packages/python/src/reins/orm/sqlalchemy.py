"""SQLAlchemy auto-expose backend (M4.1).

Introspects mapped models into :class:`reins.orm.common.ModelInfo` and runs the
six data operations through a SQLAlchemy ``Session``. All the capability shaping
(names, linting, trimming, schema) lives in :mod:`reins.orm.common`; this module
is only the SQLAlchemy-specific introspection + query execution.

SQLAlchemy is an optional dependency — install ``reins[sqlalchemy]``; the import
is lazy so the core never requires it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from reins.capability import BoundCapability
from reins.disclosure import SchemaIndex
from reins.orm.common import (
    ColumnInfo,
    ModelInfo,
    ORMError,
    build_capabilities,
    first_docline,
    pluralize,
    snake,
    trim_row,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# Re-exported for backward compatibility (M4.1 imported ORMError from here).
__all__ = ["ORMError", "build_sqlalchemy", "reflect_database"]


def reflect_database(url: str) -> tuple[Any, list[type]]:
    """Reflect an existing database into mapped classes — the zero-code path.

    Returns ``(engine, models)`` for :func:`build_sqlalchemy`. Tables without a
    primary key cannot be mapped and are skipped (SQLAlchemy automap's rule).
    """
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.ext.automap import automap_base
    except ModuleNotFoundError as exc:
        raise ORMError(
            "the 'sqlalchemy' package is required to reflect a database",
            hint="install it with: pip install 'reins[sqlalchemy]'",
        ) from exc

    engine = create_engine(url)
    base = automap_base()
    base.prepare(autoload_with=engine)
    models = list(base.classes)
    if not models:
        raise ORMError(
            f"no reflectable tables found at {url!r}",
            hint="check the URL, or ensure your tables have primary keys "
            "(automap can only map tables with a primary key)",
        )
    return engine, models


_PY_TO_JSON = {bool: "boolean", int: "integer", float: "number", str: "string"}


def build_sqlalchemy(
    db: Any,
    *,
    models: Iterable[Any] | None = None,
    can_write: bool = False,
    read_limit: int = 50,
) -> tuple[list[BoundCapability], SchemaIndex]:
    """Introspect SQLAlchemy ``models`` and return ``(capabilities, schema_index)``."""
    backend = _SqlAlchemyBackend(_session_factory(db))
    infos = [_introspect(model) for model in _resolve_models(models)]
    return build_capabilities(backend, infos, can_write=can_write, read_limit=read_limit)


class _SqlAlchemyBackend:
    """Runs the six operations against a SQLAlchemy session, trimming in-transaction."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def find(self, info: ModelInfo, filters: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return [trim_row(info, row) for row in self._select(session, info, filters, limit)]

    def count(self, info: ModelInfo, filters: dict[str, Any]) -> int:
        from sqlalchemy import func, select

        with self._session_factory() as session:
            stmt = select(func.count()).select_from(info.model).filter_by(**filters)
            return int(session.execute(stmt).scalar_one())

    def get(self, info: ModelInfo, key: str, value: Any) -> dict[str, Any] | None:
        with self._session_factory() as session:
            rows = self._select(session, info, {key: value}, 1)
            return trim_row(info, rows[0]) if rows else None

    def create(self, info: ModelInfo, values: dict[str, Any]) -> dict[str, Any]:
        with self._session_factory() as session:
            obj = info.model(**values)
            session.add(obj)
            session.commit()
            return trim_row(info, obj)

    def update(
        self, info: ModelInfo, key: str, value: Any, changes: dict[str, Any]
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            rows = self._select(session, info, {key: value}, 1)
            if not rows:
                raise _missing(info, key, value)
            for field_name, field_value in changes.items():
                setattr(rows[0], field_name, field_value)
            session.commit()
            return trim_row(info, rows[0])

    def delete(self, info: ModelInfo, key: str, value: Any) -> None:
        with self._session_factory() as session:
            rows = self._select(session, info, {key: value}, 1)
            if not rows:
                raise _missing(info, key, value)
            session.delete(rows[0])
            session.commit()

    @staticmethod
    def _select(session: Any, info: ModelInfo, filters: dict[str, Any], limit: int) -> list[Any]:
        from sqlalchemy import select

        stmt: Any = select(info.model).filter_by(**filters).limit(limit)
        return list(session.execute(stmt).scalars().all())


def _missing(info: ModelInfo, key: str, value: Any) -> ORMError:
    return ORMError(
        f"no {info.singular} with {key}={value!r}",
        hint="check the key, or find_ the row first",
    )


# --- introspection ---------------------------------------------------------------


def _introspect(model: type) -> ModelInfo:
    from sqlalchemy import inspect as sa_inspect

    mapper: Any = sa_inspect(model)
    # Uniqueness may live on the column (hand-written models) OR in a table-level
    # single-column UniqueConstraint / unique Index (which is how reflection —
    # the CLI's zero-code path — records it). Consider both.
    unique_cols = _unique_single_columns(getattr(model, "__table__", None))
    columns: list[ColumnInfo] = []
    primary_key: ColumnInfo | None = None
    for column in mapper.columns:
        json_type = _json_type(column)
        # An integer single-column PK with autoincrement True/"auto" (SQLAlchemy's
        # default) is DB-assigned — never a create/update parameter.
        autoincrement = bool(
            column.primary_key and json_type == "integer" and column.autoincrement in (True, "auto")
        )
        col = ColumnInfo(
            name=column.key,
            json_type=json_type,
            nullable=bool(column.nullable),
            autoincrement=autoincrement,
            # A DB- or Python-side default means the caller may omit it on insert.
            has_default=column.default is not None or column.server_default is not None,
            unique=bool(column.unique) or column.key in unique_cols,
        )
        columns.append(col)
        if column.primary_key and primary_key is None:
            primary_key = col
    singular = snake(model.__name__)
    table = getattr(model, "__table__", None)
    plural = (
        getattr(model, "__tablename__", None) or getattr(table, "name", None) or pluralize(singular)
    )
    return ModelInfo(
        model=model,
        singular=singular,
        plural=str(plural),
        columns=columns,
        primary_key=primary_key,
        description=first_docline(model, f"The {plural} table."),
    )


def _unique_single_columns(table: Any) -> set[str]:
    """Column names carrying a single-column UNIQUE constraint or unique index."""
    if table is None:
        return set()
    from sqlalchemy import UniqueConstraint

    names: set[str] = set()
    for constraint in getattr(table, "constraints", ()):
        cols = list(getattr(constraint, "columns", []))
        if isinstance(constraint, UniqueConstraint) and len(cols) == 1:
            names.add(cols[0].key)
    for index in getattr(table, "indexes", ()):
        cols = list(index.columns)
        if getattr(index, "unique", False) and len(cols) == 1:
            names.add(cols[0].key)
    return names


def _json_type(column: Any) -> str:
    from decimal import Decimal

    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return "string"
    if python_type is Decimal:
        return "number"
    from datetime import date

    if isinstance(python_type, type) and issubclass(python_type, date):
        return "string"  # ISO-8601
    return _PY_TO_JSON.get(python_type, "string")


# --- session + model resolution --------------------------------------------------


def _session_factory(db: Any) -> Callable[[], Any]:
    """Return a zero-arg factory yielding a context-managed ``Session``."""
    try:
        from sqlalchemy.engine import Engine
        from sqlalchemy.orm import Session, sessionmaker
    except ModuleNotFoundError as exc:
        raise ORMError(
            "the 'sqlalchemy' package is required for SQLAlchemy models",
            hint="install it with: pip install 'reins[sqlalchemy]'",
        ) from exc

    if isinstance(db, Session):
        # An existing session: reuse it, and don't close it out from under the caller.
        @contextmanager
        def borrow() -> Iterator[Any]:
            yield db

        return borrow

    if isinstance(db, Engine):
        maker: Callable[[], Any] = sessionmaker(bind=db)
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
