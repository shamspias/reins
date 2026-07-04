"""ORM auto-expose (M4.x): turn mapped models into safe, intent-level capabilities.

The wedge in one import: point Reins at models you already have and it emits the
*verbs* (``find_orders``, ``create_user``) — never the raw tables — plus a schema
index for progressive disclosure. Reads are always exposed and safe; writes appear
only when you opt in, and are gated exactly like any other capability.

:func:`build_orm` dispatches on the models it is given: SQLAlchemy mapped classes
(or a declarative base) go to the SQLAlchemy backend (M4.1); Django models, an app
label, or an ``AppConfig`` go to the Django backend (M4.2). Both produce the same
capability shape, so ``Agent.from_orm(...)`` behaves identically either way.
"""

from __future__ import annotations

from typing import Any

from reins.capability import BoundCapability
from reins.disclosure import SchemaIndex
from reins.orm.common import DEFAULT_READ_LIMIT, ORMError

__all__ = ["ORMError", "build_orm"]


def build_orm(
    db: Any = None,
    *,
    models: Any | None = None,
    can_write: bool = False,
    read_limit: int = DEFAULT_READ_LIMIT,
) -> tuple[list[BoundCapability], SchemaIndex]:
    """Introspect ``models`` and return ``(capabilities, schema_index)``.

    ``models`` selects the backend: SQLAlchemy mapped classes / a declarative
    ``Base`` use ``db`` (a ``Session`` / ``sessionmaker`` / ``Engine``); Django
    models, an app label, or an ``AppConfig`` ignore ``db`` (the connection is
    global). Reads are always generated; writes only with ``can_write=True``.
    """
    if models is None:
        raise ORMError(
            "no models given",
            hint="pass models=[User, Order, ...] (or a Base / Django app label) to from_orm(...)",
        )
    if _is_django(models):
        from reins.orm.django import build_django

        return build_django(models, can_write=can_write, read_limit=read_limit)
    from reins.orm.sqlalchemy import build_sqlalchemy

    return build_sqlalchemy(db, models=models, can_write=can_write, read_limit=read_limit)


def _is_django(models: Any) -> bool:
    """True if ``models`` targets Django, without importing Django unless present."""
    if isinstance(models, str) or hasattr(models, "get_models"):
        return True  # app label / AppConfig
    try:
        from django.db.models import Model
    except ModuleNotFoundError:
        return False
    candidate: Any = None
    if isinstance(models, type):  # a single class (Django model or SQLAlchemy Base)
        candidate = models
    elif isinstance(models, list | tuple) and models:
        candidate = models[0]
    return isinstance(candidate, type) and issubclass(candidate, Model)
