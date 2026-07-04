"""A deterministic, end-to-end walk through the flagship example — what ``make demo`` runs.

No API key and no network: a scripted :class:`~reins.model.FakeModel` stands in for
the LLM, so the *harness* behavior is what's on display — auto-exposed capabilities,
read answers, identity-scoped row-level security, and a gated write that pauses at a
real approval prompt — all against a live SQLite database.

Run it directly:  python -m examples.fastapi_sqlalchemy.demo
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from reins import TerminalApprovalHandler
from reins.console import describe_capabilities
from reins.model import FakeModel
from reins.types import Context

from .models import Order, make_engine
from .shop import build_agent, build_capabilities


def _rule(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def main() -> int:
    engine = make_engine()

    _rule("1. What Reins generated from the shop's models (zero hand-written CRUD)")
    capabilities, _schema = build_capabilities(engine)
    for line in describe_capabilities(capabilities):
        print(line)

    _rule("2. ask() — a read question, answered through a generated capability")
    reader = build_agent(
        engine,
        model=FakeModel.from_script(
            [
                {"call": "count_orders", "args": {"status": "open"}},
                {"final": "There are 2 orders currently open."},
            ]
        ),
    )
    print("Q: how many orders are open?")
    print("A:", reader.ask("how many orders are open?").output.text)

    _rule("3. Identity-scoped reads — row-level security, per logged-in user")
    my_orders = next(c for c in capabilities if c.spec.name == "find_my_orders")
    for email in ("ada@shop.test", "ben@shop.test"):
        rows = my_orders.func(ctx=Context(principal=email))
        print(f"{email} sees: {[r['ref'] for r in rows]}")
    print("→ The agent calls this same capability, scoped to whoever is logged in;")
    print("  one customer's chat can never surface another's orders.")

    _rule("4. run() — a write pauses for approval before it touches the database")
    writer = build_agent(
        engine,
        model=FakeModel.from_script(
            [
                {
                    "call": "update_order",
                    "args": {"ref": "ORD-2", "status": "cancelled"},
                },
                {"final": "Order ORD-2 has been cancelled."},
            ]
        ),
        # A real terminal gate; scripted to answer "yes" so the demo runs unattended.
        # You will see the exact preview a human would be shown.
        approve=TerminalApprovalHandler(input_fn=lambda _prompt: "y"),
    )
    print("Goal: cancel order ORD-2\n")
    result = writer.run("cancel order ORD-2")
    print("\nA:", result.output.text)

    with Session(engine) as session:
        order = session.scalar(select(Order).filter_by(ref="ORD-2"))
        assert order is not None
        print(f"Database now: ORD-2 status = {order.status!r}")

    _rule("5. The audit trail — every write attempt, PII-redacted")
    for record in writer.audit.records:  # type: ignore[attr-defined]
        print(
            f"  {record.decision:<9} {record.capability}({record.arguments}) "
            f"by {record.principal} @ {record.timestamp:%H:%M:%S}"
        )

    _rule("Run it for real")
    print("Serve the same agent over HTTP with the <reins-chat> widget:")
    print("  pip install 'reins[sqlalchemy]' fastapi uvicorn")
    print("  export ANTHROPIC_API_KEY=sk-...   # or any provider key")
    print("  uvicorn examples.fastapi_sqlalchemy.app:app --reload")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
