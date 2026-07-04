"""The real server: the shop agent over HTTP, with the ``<reins-chat>`` widget.

    uvicorn examples.fastapi_sqlalchemy.app:app --reload

``POST /ask`` answers read-only questions; ``POST /run`` performs gated writes.
The caller's identity comes from an ``X-User`` header (stand in for your real
session cookie / JWT) and flows into row-level security, so ``find_my_orders``
only ever returns the caller's own orders. The home page embeds the chat widget.

Reads run freely; writes are gated — with no interactive terminal on a server,
the default approval handler fails closed, so wire your own ``approve=`` handler
(a review queue, a Slack confirm, an allow-list) for real write access.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from reins import chat_widget
from reins.web import fastapi_router

from .models import make_engine
from .shop import build_agent


def _identity(request: Request) -> str | None:
    """Derive the caller's principal from the request — your existing auth goes here."""
    return request.headers.get("X-User")


def create_app() -> FastAPI:
    engine = make_engine()
    # No model= → resolved from the environment (ANTHROPIC_API_KEY, etc.) on first use.
    agent = build_agent(engine)

    app = FastAPI(title="Reins shop demo")
    # read_only=False exposes POST /run for gated writes; drop it for a read-only app.
    app.include_router(fastapi_router(agent, read_only=False, identity=_identity))

    @app.get("/", response_class=HTMLResponse)
    def home() -> Any:
        return (
            "<!doctype html><meta charset='utf-8'><title>Reins shop demo</title>"
            "<h1>Reins shop demo</h1>"
            "<p>Ask about customers and orders. (Set an <code>X-User</code> header "
            "in a real client to scope <code>find_my_orders</code>.)</p>"
            + chat_widget("/ask")
        )

    return app


app = create_app()
