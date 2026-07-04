"""The demo shop's data model — ordinary SQLAlchemy you might already have.

Nothing here knows about Reins. A shared in-memory SQLite database (one
connection via ``StaticPool``, so every session sees the same rows) keeps the
example self-contained and dependency-light.
"""

from __future__ import annotations

from sqlalchemy import Numeric, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Customer(Base):
    """A customer of the shop."""

    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)


class Order(Base):
    """A customer order."""

    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String, unique=True)
    customer_email: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")
    total: Mapped[float] = mapped_column(Numeric(8, 2), default=0)


def make_engine() -> Engine:
    """A seeded in-memory shop database, shared across sessions."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Customer(email="ada@shop.test", name="Ada"),
                Customer(email="ben@shop.test", name="Ben"),
                Order(
                    ref="ORD-1",
                    customer_email="ada@shop.test",
                    status="open",
                    total=120,
                ),
                Order(
                    ref="ORD-2", customer_email="ada@shop.test", status="open", total=40
                ),
                Order(
                    ref="ORD-3",
                    customer_email="ben@shop.test",
                    status="shipped",
                    total=75,
                ),
            ]
        )
        session.commit()
    return engine
