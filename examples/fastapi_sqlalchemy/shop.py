"""Wire the shop's models into a Reins agent.

Two things happen here, and they compose:

1. ``build_orm`` auto-exposes the models as intent-level capabilities
   (``find_orders``, ``get_order_by_ref``, ``update_order`` …) — zero
   hand-written code.
2. We *add* one hand-written, identity-scoped capability, ``find_my_orders``,
   which filters by the caller's principal. It shows how the ORM auto-expose and
   your own capabilities live side by side, and how row-level security (M2.4)
   scopes data to whoever is logged in.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from reins import Agent, SchemaIndex, capability
from reins.capability import BoundCapability
from reins.orm import build_orm
from reins.types import Context

from .models import Customer, Order


def build_capabilities(engine: Engine) -> tuple[list[BoundCapability], SchemaIndex]:
    """Auto-exposed model capabilities plus one identity-scoped read."""
    capabilities, schema = build_orm(engine, models=[Customer, Order], can_write=True)

    @capability(reads=True, scope="user")
    def find_my_orders(ctx: Context, status: str | None = None) -> list[dict[str, Any]]:
        """Find the calling customer's own orders (optionally filtered by status)."""
        with Session(engine) as session:
            stmt = select(Order).filter_by(customer_email=ctx.principal)
            if status is not None:
                stmt = stmt.filter_by(status=status)
            return [
                {"ref": o.ref, "status": o.status, "total": float(o.total)}
                for o in session.scalars(stmt)
            ]

    capabilities.append(find_my_orders)
    return capabilities, schema


def build_agent(engine: Engine, **kwargs: Any) -> Agent:
    """An agent over the shop. Reads run freely; writes are gated for approval.

    Extra keyword arguments (``model``, ``approve``, ``principal``, ``audit`` …)
    are forwarded — the demo scripts a ``FakeModel``; the real server (``app.py``)
    lets the model resolve from the environment.
    """
    capabilities, schema = build_capabilities(engine)
    return Agent(capabilities=capabilities, schema=schema, **kwargs)
