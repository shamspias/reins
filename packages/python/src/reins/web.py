"""Instant web integrations (M4.4): drop Reins into an app your users already log in to.

:func:`endpoint` returns a WSGI application exposing ``POST /ask`` (and ``/run``
when writes are enabled) that runs an :class:`~reins.loop.Agent` and returns JSON.
It works standalone, mounts directly in Flask (Flask *is* WSGI), and mounts in
FastAPI/Starlette via ``WSGIMiddleware`` — with thin :func:`flask_blueprint` and
:func:`fastapi_router` helpers for the native feel.

Identity comes from the **existing session**: pass ``identity=lambda req: ...`` to
pull a principal from the request's cookie / JWT / header, and it flows straight
into the agent's :class:`~reins.types.Context`, so row-level-scoped capabilities
(M2.4) are enforced per user — no second auth system. ``read_only=True`` (the
default) never exposes a write path and only ever calls ``ask()``, so the endpoint
is safe to expose by construction (§2.1).

:func:`chat_widget` returns a self-contained ``<reins-chat>`` custom element that
POSTs to the endpoint with ``credentials: "include"``, so the browser's existing
cookies authenticate the call.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from reins.errors import ReinsError
from reins.loop import Agent

# A request identity resolver: given the framework's request/environ, return the
# caller's principal (or None). Kept deliberately duck-typed so the same resolver
# shape works for WSGI environ, Flask request, and FastAPI Request.
Identity = Callable[[Any], str | None]

_MAX_BODY_BYTES = 64 * 1024  # bound the request body (untrusted input, §2.13)


def run_goal(
    agent: Agent,
    goal: str,
    *,
    principal: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """Run one goal and return a JSON-safe result dict — the framework-neutral core."""
    result = agent.run(goal, principal=principal) if write else agent.ask(goal, principal=principal)
    return {"answer": result.output.text or "", "trace_id": result.trace_id}


def endpoint(
    agent: Agent,
    *,
    path: str = "/ask",
    run_path: str = "/run",
    read_only: bool = True,
    identity: Identity | None = None,
    widget: bool = True,
    widget_path: str = "/reins-chat",
) -> Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]:
    """Return a WSGI app: ``POST {path}`` runs ``ask``; ``POST {run_path}`` runs ``run``.

    ``read_only=True`` (default) omits the write path entirely. ``identity`` maps
    the WSGI ``environ`` to a principal for RLS. ``widget=True`` serves the
    ``<reins-chat>`` element at ``GET {widget_path}``.
    """

    def app(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        route = environ.get("PATH_INFO", "") or "/"

        if method == "GET" and widget and route == widget_path:
            return _respond(start_response, 200, chat_widget(path), content_type="text/html")

        if method == "POST" and route == path:
            return _handle(environ, start_response, agent, identity, write=False)

        if method == "POST" and route == run_path and not read_only:
            return _handle(environ, start_response, agent, identity, write=True)

        return _error(start_response, 404, f"no route for {method} {route}", "check the path")

    return app


def _handle(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    agent: Agent,
    identity: Identity | None,
    *,
    write: bool,
) -> Iterable[bytes]:
    goal = _read_goal(environ)
    if goal is None:
        return _error(
            start_response, 400, "expected a JSON body with a 'goal' string", 'post {"goal": ...}'
        )
    principal = identity(environ) if identity is not None else None
    try:
        payload = run_goal(agent, goal, principal=principal, write=write)
    except ReinsError as exc:
        # Hinted, user-facing failure (e.g. no model configured) — never a 500 stack.
        return _error(start_response, 400, str(exc).splitlines()[0], _hint_of(exc))
    return _respond(start_response, 200, json.dumps(payload), content_type="application/json")


def _read_goal(environ: dict[str, Any]) -> str | None:
    try:
        length = min(int(environ.get("CONTENT_LENGTH") or 0), _MAX_BODY_BYTES)
    except (TypeError, ValueError):
        return None
    body = environ["wsgi.input"].read(length) if length > 0 else b""
    try:
        data = json.loads(body or b"{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    goal = data.get("goal")
    return goal.strip() if isinstance(goal, str) and goal.strip() else None


def _respond(
    start_response: Callable[..., Any],
    status: int,
    body: str,
    *,
    content_type: str,
) -> Iterable[bytes]:
    payload = body.encode("utf-8")
    start_response(
        f"{status} {_STATUS_TEXT.get(status, 'OK')}",
        [("Content-Type", f"{content_type}; charset=utf-8"), ("Content-Length", str(len(payload)))],
    )
    return [payload]


def _error(
    start_response: Callable[..., Any], status: int, message: str, hint: str
) -> Iterable[bytes]:
    body = json.dumps({"error": message, "hint": f"→ {hint}"})
    return _respond(start_response, status, body, content_type="application/json")


def _hint_of(exc: ReinsError) -> str:
    return getattr(exc, "hint", "check the server configuration")


_STATUS_TEXT = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}


# --- framework adapters (thin; delegate to run_goal) -----------------------------


def flask_blueprint(  # pragma: no cover  (optional dep; importorskip-tested, not in CI)
    agent: Agent,
    *,
    name: str = "reins",
    path: str = "/ask",
    run_path: str = "/run",
    read_only: bool = True,
    identity: Identity | None = None,
    widget: bool = True,
    widget_path: str = "/reins-chat",
) -> Any:
    """A Flask ``Blueprint`` exposing the same routes — ``app.register_blueprint(bp)``."""
    from flask import Blueprint, Response, request

    bp = Blueprint(name, __name__)

    def _principal() -> str | None:
        return identity(request) if identity is not None else None

    @bp.post(path)
    def ask() -> Any:
        goal = _flask_goal(request)
        if goal is None:
            return Response(_bad_body(), status=400, mimetype="application/json")
        return _flask_run(agent, goal, principal=_principal(), write=False)

    if not read_only:

        @bp.post(run_path)
        def run() -> Any:
            goal = _flask_goal(request)
            if goal is None:
                return Response(_bad_body(), status=400, mimetype="application/json")
            return _flask_run(agent, goal, principal=_principal(), write=True)

    if widget:

        @bp.get(widget_path)
        def widget_route() -> Any:
            return Response(chat_widget(path), mimetype="text/html")

    return bp


def fastapi_router(  # pragma: no cover  (optional dep; importorskip-tested, not in CI)
    agent: Agent,
    *,
    path: str = "/ask",
    run_path: str = "/run",
    read_only: bool = True,
    identity: Identity | None = None,
    widget: bool = True,
    widget_path: str = "/reins-chat",
) -> Any:
    """A FastAPI ``APIRouter`` exposing the same routes — ``app.include_router(router)``."""
    from fastapi import APIRouter, Body, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    router = APIRouter()
    body_default = Body(...)

    def _principal(request: Any) -> str | None:
        return identity(request) if identity is not None else None

    async def ask(request: Any, body: Any = body_default) -> Any:
        goal = _dict_goal(body)
        if goal is None:
            return JSONResponse(json.loads(_bad_body()), status_code=400)
        return _dict_run(agent, goal, principal=_principal(request), write=False)

    async def run(request: Any, body: Any = body_default) -> Any:
        goal = _dict_goal(body)
        if goal is None:
            return JSONResponse(json.loads(_bad_body()), status_code=400)
        return _dict_run(agent, goal, principal=_principal(request), write=True)

    async def widget_route() -> Any:
        return chat_widget(path)

    # Set real (non-stringized) annotations before registering: this module uses
    # `from __future__ import annotations`, and FastAPI resolves parameter types
    # from a handler's globals — where the locally-imported Request isn't visible.
    ask.__annotations__ = {"request": Request, "body": dict, "return": Any}
    run.__annotations__ = {"request": Request, "body": dict, "return": Any}
    router.post(path)(ask)
    if not read_only:
        router.post(run_path)(run)
    if widget:
        router.get(widget_path, response_class=HTMLResponse)(widget_route)

    return router


def _flask_goal(request: Any) -> str | None:
    return _dict_goal(request.get_json(silent=True))


def _dict_goal(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    goal = data.get("goal")
    return goal.strip() if isinstance(goal, str) and goal.strip() else None


def _flask_run(agent: Agent, goal: str, *, principal: str | None, write: bool) -> Any:
    from flask import Response

    try:
        payload = run_goal(agent, goal, principal=principal, write=write)
    except ReinsError as exc:
        return Response(
            json.dumps({"error": str(exc).splitlines()[0], "hint": f"→ {_hint_of(exc)}"}),
            status=400,
            mimetype="application/json",
        )
    return Response(json.dumps(payload), mimetype="application/json")


def _dict_run(agent: Agent, goal: str, *, principal: str | None, write: bool) -> Any:
    from fastapi.responses import JSONResponse

    try:
        return JSONResponse(run_goal(agent, goal, principal=principal, write=write))
    except ReinsError as exc:
        return JSONResponse(
            {"error": str(exc).splitlines()[0], "hint": f"→ {_hint_of(exc)}"}, status_code=400
        )


def _bad_body() -> str:
    return json.dumps(
        {"error": "expected a JSON body with a 'goal' string", "hint": '→ post {"goal": ...}'}
    )


# --- the <reins-chat> web component ----------------------------------------------


def chat_widget(endpoint: str = "/ask", *, title: str = "Ask") -> str:
    """A self-contained ``<reins-chat>`` element that POSTs to ``endpoint``.

    Sends ``credentials: "include"`` so the browser's existing session cookie /
    JWT authenticates the request — the server derives identity from it. Embed the
    returned HTML anywhere; no build step, no external assets.
    """
    return _WIDGET_HTML.replace("__ENDPOINT__", endpoint).replace("__TITLE__", title)


# Inlined on purpose (no external assets, CSP-friendly). One question in, the
# answer rendered below; credentials:'include' carries the existing session.
_WIDGET_HTML = """
<reins-chat endpoint="__ENDPOINT__"></reins-chat>
<script>
customElements.define("reins-chat", class extends HTMLElement {
  connectedCallback() {
    const endpoint = this.getAttribute("endpoint") || "/ask";
    this.innerHTML = `
      <form style="display:flex;gap:.5rem"><input name="q" placeholder="__TITLE__…"
        style="flex:1;padding:.5rem" autocomplete="off"/>
        <button style="padding:.5rem 1rem">Send</button></form>
      <pre class="reins-out" style="white-space:pre-wrap;margin-top:.75rem"></pre>`;
    const form = this.querySelector("form");
    const out = this.querySelector(".reins-out");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const goal = form.q.value.trim();
      if (!goal) return;
      out.textContent = "…";
      try {
        const res = await fetch(endpoint, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          credentials: "include",
          body: JSON.stringify({goal}),
        });
        const data = await res.json();
        out.textContent = res.ok
          ? (data.answer || "(no answer)")
          : (data.error + "\\n" + (data.hint || ""));
      } catch (err) { out.textContent = "network error: " + err; }
    });
  }
});
</script>
""".strip()
