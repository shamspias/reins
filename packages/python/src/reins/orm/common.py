"""ORM-neutral machinery for auto-expose (M4.x).

The capability shape is identical across ORMs — intent-level names, natural-key
lookups, lean linted params, trimmed responses, a schema index — so it lives here
once. Each backend (SQLAlchemy in M4.1, Django in M4.2) supplies only two things:

- **introspection** — describe each model as a :class:`ModelInfo`, and
- **data access** — a :class:`Backend` that runs the six operations, returning
  already-trimmed rows from inside its own transaction.

:func:`build_capabilities` turns those into linted, gated capabilities plus a
:class:`reins.disclosure.SchemaIndex`. A generated capability that cannot pass the
Capability Linter (§2.15) is dropped, never shipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from reins.capability import BoundCapability
from reins.disclosure import SchemaIndex, TableInfo
from reins.errors import ReinsError
from reins.linter import CapabilityLinter
from reins.types import Access, Capability

MAX_PARAMS = 7  # keep generated capabilities lean (linter §2.15)
MAX_COLUMNS_RETURNED = 12  # trim wide rows so raw tables never flood context
DEFAULT_READ_LIMIT = 50  # bounded result sets (§2.13)
_MAX_VALUE_CHARS = 500  # trim very long text fields in responses

# Column names commonly used as human-facing natural keys, best first.
_NATURAL_KEY_HINTS = ("slug", "email", "username", "code", "key", "name", "title", "uuid")


class ORMError(ReinsError):
    """The ORM adapter cannot introspect or expose a model as configured."""


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """One column, described in ORM-neutral terms."""

    name: str
    json_type: str
    nullable: bool
    autoincrement: bool = False
    has_default: bool = False
    unique: bool = False

    @property
    def optional_on_create(self) -> bool:
        """A column the caller may omit when creating a row."""
        return self.nullable or self.autoincrement or self.has_default


@dataclass(slots=True)
class ModelInfo:
    """One mapped model, described in ORM-neutral terms."""

    model: Any
    singular: str
    plural: str
    columns: list[ColumnInfo]
    primary_key: ColumnInfo | None
    description: str = ""

    @property
    def natural_key(self) -> ColumnInfo | None:
        return _natural_key(self.columns, self.primary_key)

    @property
    def lookup_key(self) -> ColumnInfo | None:
        """The column to look a single row up by — natural key preferred over the id."""
        return self.natural_key or self.primary_key


@runtime_checkable
class Backend(Protocol):
    """Runs the six data operations for one ORM, returning trimmed rows.

    Each method owns its own transaction/session and trims rows *inside* it (via
    :func:`trim_row`), so lazy attributes are still live. ``update`` / ``delete``
    raise :class:`ORMError` when the row is missing — surfaced to the model as
    errors-as-data with a ``→`` hint.
    """

    def find(
        self, info: ModelInfo, filters: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]: ...
    def count(self, info: ModelInfo, filters: dict[str, Any]) -> int: ...
    def get(self, info: ModelInfo, key: str, value: Any) -> dict[str, Any] | None: ...
    def create(self, info: ModelInfo, values: dict[str, Any]) -> dict[str, Any]: ...
    def update(
        self, info: ModelInfo, key: str, value: Any, changes: dict[str, Any]
    ) -> dict[str, Any]: ...
    def delete(self, info: ModelInfo, key: str, value: Any) -> None: ...


def build_capabilities(
    backend: Backend,
    infos: list[ModelInfo],
    *,
    can_write: bool,
    read_limit: int,
) -> tuple[list[BoundCapability], SchemaIndex]:
    """Generate linted capabilities and a schema index from introspected models."""
    if not infos:
        raise ORMError(
            "no mapped models to expose",
            hint="pass models=[User, Order, ...] to from_orm(...)",
        )
    linter = CapabilityLinter()
    capabilities: list[BoundCapability] = []
    tables: list[TableInfo] = []
    for info in infos:
        candidates = _read_capabilities(backend, info, read_limit)
        if can_write:
            candidates.extend(_write_capabilities(backend, info))
        # Never expose a capability that fails the linter (§2.15). Verb-first names
        # (find_/count_/create_/update_/delete_) always pass; a get_ whose entity +
        # key name is too short is dropped — find_ still covers lookup.
        capabilities.extend(c for c in candidates if linter.lint_capability(c.spec).ok)
        tables.append(_table_info(info))
    return capabilities, SchemaIndex(tables=tuple(tables))


# --- read capabilities ----------------------------------------------------------


def _read_capabilities(backend: Backend, info: ModelInfo, read_limit: int) -> list[BoundCapability]:
    caps = [_find_cap(backend, info, read_limit), _count_cap(backend, info)]
    if info.lookup_key is not None:
        caps.append(_get_cap(backend, info))
    return caps


def _find_cap(backend: Backend, info: ModelInfo, read_limit: int) -> BoundCapability:
    filters = _filter_columns(info)
    spec = Capability(
        name=f"find_{info.plural}",
        description=f"Find {info.plural} matching optional filters (returns up to "
        f"{read_limit} rows, trimmed).",
        input_schema=_object_schema({c.name: c.json_type for c in filters}, required=[]),
        access=Access.READ,
    )

    def run(**kwargs: Any) -> list[dict[str, Any]]:
        return backend.find(info, _known(kwargs, filters), read_limit)

    return BoundCapability(spec=spec, func=run)


def _count_cap(backend: Backend, info: ModelInfo) -> BoundCapability:
    filters = _filter_columns(info)
    spec = Capability(
        name=f"count_{info.plural}",
        description=f"Count {info.plural} matching optional filters.",
        input_schema=_object_schema({c.name: c.json_type for c in filters}, required=[]),
        access=Access.READ,
    )

    def run(**kwargs: Any) -> int:
        return backend.count(info, _known(kwargs, filters))

    return BoundCapability(spec=spec, func=run)


def _get_cap(backend: Backend, info: ModelInfo) -> BoundCapability:
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
        return backend.get(info, key.name, kwargs[key.name])

    return BoundCapability(spec=spec, func=run)


# --- write capabilities (gated) --------------------------------------------------


def _write_capabilities(backend: Backend, info: ModelInfo) -> list[BoundCapability]:
    caps = [_create_cap(backend, info)]
    if info.lookup_key is not None:
        caps.append(_update_cap(backend, info))
        caps.append(_delete_cap(backend, info))
    return caps


def _create_cap(backend: Backend, info: ModelInfo) -> BoundCapability:
    settable = _settable_columns(info)
    required = [c.name for c in settable if not c.optional_on_create]
    spec = Capability(
        name=f"create_{info.singular}",
        description=f"Create a new {info.singular} (returns the created row, trimmed).",
        input_schema=_object_schema({c.name: c.json_type for c in settable}, required=required),
        access=Access.WRITE,
    )

    def run(**kwargs: Any) -> dict[str, Any]:
        return backend.create(info, _known(kwargs, settable))

    return BoundCapability(spec=spec, func=run)


def _update_cap(backend: Backend, info: ModelInfo) -> BoundCapability:
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
        return backend.update(info, key.name, kwargs[key.name], _known(kwargs, settable))

    return BoundCapability(spec=spec, func=run)


def _delete_cap(backend: Backend, info: ModelInfo) -> BoundCapability:
    key = info.lookup_key
    assert key is not None
    spec = Capability(
        name=f"delete_{info.singular}",
        description=f"Delete a {info.singular}, found by its {key.name}.",
        input_schema=_object_schema({key.name: key.json_type}, required=[key.name]),
        access=Access.DESTRUCTIVE,
    )

    def run(**kwargs: Any) -> dict[str, Any]:
        backend.delete(info, key.name, kwargs[key.name])
        return {"deleted": kwargs[key.name]}

    return BoundCapability(spec=spec, func=run)


# --- trimming (used by every backend inside its transaction) ---------------------


def trim_row(info: ModelInfo, row: Any) -> dict[str, Any]:
    """A bounded, JSON-safe view of a row (never a raw column dump — linter §2.15)."""
    out: dict[str, Any] = {}
    for column in info.columns[:MAX_COLUMNS_RETURNED]:
        out[column.name] = json_value(getattr(row, column.name, None))
    return out


def json_value(value: Any) -> Any:
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):  # date & datetime
        return value.isoformat()
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[:_MAX_VALUE_CHARS] + "…"


# --- column selection ------------------------------------------------------------


def _filter_columns(info: ModelInfo) -> list[ColumnInfo]:
    """Columns exposed as optional find/count filters — lean, capped at MAX_PARAMS."""
    return _prioritize(info, info.columns)[:MAX_PARAMS]


def _settable_columns(info: ModelInfo) -> list[ColumnInfo]:
    """Columns a create may set — no autoincrement PK — capped at MAX_PARAMS."""
    settable = [c for c in info.columns if not c.autoincrement]
    return _prioritize(info, settable)[:MAX_PARAMS]


def _prioritize(info: ModelInfo, columns: list[ColumnInfo]) -> list[ColumnInfo]:
    """Order columns so the most useful survive the MAX_PARAMS cap."""
    key_names = {c.name for c in (info.natural_key, info.primary_key) if c is not None}

    def rank(column: ColumnInfo) -> tuple[int, str]:
        if column.name in key_names:
            return (0, column.name)
        if not column.nullable:
            return (1, column.name)
        return (2, column.name)

    return sorted(columns, key=rank)


def _known(kwargs: dict[str, Any], columns: list[ColumnInfo]) -> dict[str, Any]:
    """Keep only recognized, provided arguments (untrusted model output, §2.6)."""
    allowed = {c.name for c in columns}
    return {k: v for k, v in kwargs.items() if k in allowed and v is not None}


def _natural_key(columns: list[ColumnInfo], pk: ColumnInfo | None) -> ColumnInfo | None:
    """A natural key must be **unique** (it identifies one row); a hint word only
    ranks among the unique string columns — it never overrides uniqueness."""
    pk_name = pk.name if pk is not None else None
    unique_strings = [
        c for c in columns if c.unique and c.json_type == "string" and c.name != pk_name
    ]
    if not unique_strings:
        return None
    by_name = {c.name: c for c in unique_strings}
    for hint in _NATURAL_KEY_HINTS:
        if hint in by_name:
            return by_name[hint]
    return unique_strings[0]


# --- schema index for disclosure -------------------------------------------------


def _table_info(info: ModelInfo) -> TableInfo:
    description = info.description.strip() or f"The {info.plural} table."
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


# --- shared name helpers ---------------------------------------------------------


def snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def pluralize(word: str) -> str:
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def first_docline(obj: Any, fallback: str) -> str:
    doc = (getattr(obj, "__doc__", None) or "").strip().splitlines()
    return doc[0].strip() if doc and doc[0].strip() else fallback
