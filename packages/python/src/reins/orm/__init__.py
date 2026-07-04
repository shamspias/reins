"""ORM auto-expose (M4.x): turn mapped models into safe, intent-level capabilities.

The wedge in one import: point Reins at models you already have and it emits the
*verbs* (``find_orders``, ``create_user``) — never the raw tables — plus a schema
index for progressive disclosure. Reads are always exposed and safe; writes appear
only when you opt in, and are gated exactly like any other capability.

SQLAlchemy ships here in M4.1; Django ORM follows in M4.2 behind the same
:func:`build_orm` shape.
"""

from reins.orm.sqlalchemy import build_orm

__all__ = ["build_orm"]
