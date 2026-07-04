"""Django ORM auto-expose backend (M4.2).

Introspects Django models into :class:`reins.orm.common.ModelInfo` and runs the
six data operations through the Django ORM. The capability shaping (names,
linting, trimming, schema) is shared with SQLAlchemy in :mod:`reins.orm.common` —
this module is only the Django-specific introspection + query execution, so
``Agent.from_orm(models=[User, Order])`` behaves identically to the SQLAlchemy
path.

Django is an optional dependency — install ``reins[django]``; the import is lazy
so the core never requires it.
"""

from __future__ import annotations

from typing import Any

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

__all__ = ["build_django"]

# Django internal field type → JSON type.
_INT_FIELDS = frozenset(
    {
        "AutoField",
        "BigAutoField",
        "SmallAutoField",
        "IntegerField",
        "BigIntegerField",
        "SmallIntegerField",
        "PositiveIntegerField",
        "PositiveSmallIntegerField",
        "PositiveBigIntegerField",
        "ForeignKey",
        "OneToOneField",
    }
)
_NUMBER_FIELDS = frozenset({"FloatField", "DecimalField"})
_BOOL_FIELDS = frozenset({"BooleanField", "NullBooleanField"})
_AUTO_FIELDS = frozenset({"AutoField", "BigAutoField", "SmallAutoField"})


def build_django(
    models: Any,
    *,
    can_write: bool = False,
    read_limit: int = 50,
) -> tuple[list[BoundCapability], SchemaIndex]:
    """Introspect Django ``models`` and return ``(capabilities, schema_index)``."""
    backend = _DjangoBackend()
    infos = [_introspect(model) for model in _resolve_models(models)]
    return build_capabilities(backend, infos, can_write=can_write, read_limit=read_limit)


class _DjangoBackend:
    """Runs the six operations through the Django ORM, trimming rows to dicts."""

    def find(self, info: ModelInfo, filters: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        rows = list(info.model.objects.filter(**filters)[:limit])
        return [trim_row(info, row) for row in rows]

    def count(self, info: ModelInfo, filters: dict[str, Any]) -> int:
        return int(info.model.objects.filter(**filters).count())

    def get(self, info: ModelInfo, key: str, value: Any) -> dict[str, Any] | None:
        row = info.model.objects.filter(**{key: value}).first()
        return trim_row(info, row) if row is not None else None

    def create(self, info: ModelInfo, values: dict[str, Any]) -> dict[str, Any]:
        return trim_row(info, info.model.objects.create(**values))

    def update(
        self, info: ModelInfo, key: str, value: Any, changes: dict[str, Any]
    ) -> dict[str, Any]:
        row = info.model.objects.filter(**{key: value}).first()
        if row is None:
            raise _missing(info, key, value)
        for field_name, field_value in changes.items():
            setattr(row, field_name, field_value)
        row.save()
        return trim_row(info, row)

    def delete(self, info: ModelInfo, key: str, value: Any) -> None:
        row = info.model.objects.filter(**{key: value}).first()
        if row is None:
            raise _missing(info, key, value)
        row.delete()


def _missing(info: ModelInfo, key: str, value: Any) -> ORMError:
    return ORMError(
        f"no {info.singular} with {key}={value!r}",
        hint="check the key, or find_ the row first",
    )


# --- introspection ---------------------------------------------------------------


def _introspect(model: Any) -> ModelInfo:
    meta = model._meta
    columns: list[ColumnInfo] = []
    primary_key: ColumnInfo | None = None
    for field in meta.concrete_fields:
        internal = field.get_internal_type()
        # A relation column is stored under its attname (``author_id``); plain
        # fields keep their name.
        name = field.attname if field.is_relation else field.name
        col = ColumnInfo(
            name=name,
            json_type=_json_type(internal),
            nullable=bool(field.null),
            autoincrement=bool(field.primary_key and internal in _AUTO_FIELDS),
            has_default=_has_default(field),
            unique=bool(field.unique),
        )
        columns.append(col)
        if field.primary_key and primary_key is None:
            primary_key = col
    singular = snake(model.__name__)
    plural = pluralize(singular)
    return ModelInfo(
        model=model,
        singular=singular,
        plural=plural,
        columns=columns,
        primary_key=primary_key,
        description=first_docline(model, f"The {plural} table."),
    )


def _json_type(internal: str) -> str:
    if internal in _INT_FIELDS:
        return "integer"
    if internal in _NUMBER_FIELDS:
        return "number"
    if internal in _BOOL_FIELDS:
        return "boolean"
    return "string"


def _has_default(field: Any) -> bool:
    # A model/DB default, or an auto timestamp, means the caller may omit it.
    if field.has_default():
        return True
    return bool(getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False))


def _resolve_models(models: Any) -> list[Any]:
    if models is None:
        raise ORMError(
            "no models given",
            hint="pass models=[User, Order, ...], an app label, or an AppConfig",
        )
    if isinstance(models, str):  # a Django app label
        from django.apps import apps

        return list(apps.get_app_config(models).get_models())
    if hasattr(models, "get_models"):  # an AppConfig
        return list(models.get_models())
    return list(models)
