"""Tests for the web integration (M4.4): the WSGI endpoint, identity → RLS, the
read-only guarantee, the <reins-chat> widget, and reins.cli(agent).

The WSGI app is driven with a hand-built environ and a start_response capture —
no server, no framework, fully deterministic (the FakeModel is the only stochastic
piece). Flask/FastAPI routers share the same core and are exercised where present.
"""

import io
import json
from typing import Any

import reins
from reins import Agent, capability
from reins.model import FakeModel
from reins.types import Context

# --- helpers ----------------------------------------------------------------------


@capability(reads=True)
def find_orders(status: str = "open") -> list[str]:
    "Find orders by status."
    return ["o1", "o2"]


@capability
def delete_order(order_id: int) -> str:
    "Delete an order."
    return f"deleted {order_id}"


def _call(
    app: Any, method: str, path: str, *, body: dict[str, Any] | None = None, environ: Any = None
) -> tuple[int, dict[str, str], bytes]:
    raw = json.dumps(body).encode() if body is not None else b""
    env: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
    }
    if environ:
        env.update(environ)
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(headers)

    chunks = app(env, start_response)
    payload = b"".join(chunks)
    return captured["status"], captured["headers"], payload


def _json(payload: bytes) -> dict[str, Any]:
    return json.loads(payload)


# --- the endpoint answers reads ---------------------------------------------------


def test_ask_endpoint_answers() -> None:
    model = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "open"}}, {"final": "2 open orders."}]
    )
    app = reins.endpoint(Agent(model=model, capabilities=[find_orders]))
    status, headers, payload = _call(app, "POST", "/ask", body={"goal": "how many open?"})
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert _json(payload)["answer"] == "2 open orders."
    assert _json(payload)["trace_id"]


def test_missing_goal_is_a_hinted_400() -> None:
    app = reins.endpoint(Agent(model=FakeModel.from_script([{"final": "x"}])))
    status, _h, payload = _call(app, "POST", "/ask", body={"not_goal": "x"})
    assert status == 400
    assert "→" in _json(payload)["hint"]


def test_malformed_json_is_a_400() -> None:
    app = reins.endpoint(Agent(model=FakeModel.from_script([{"final": "x"}])))
    env = {"wsgi.input": io.BytesIO(b"{not json"), "CONTENT_LENGTH": "9"}
    status, _h, _p = _call(app, "POST", "/ask", environ=env)
    assert status == 400


def test_unknown_route_is_404() -> None:
    app = reins.endpoint(Agent(model=FakeModel.from_script([{"final": "x"}])))
    status, _h, _p = _call(app, "GET", "/nope")
    assert status == 404


# --- the read-only guarantee ------------------------------------------------------


def test_read_only_endpoint_has_no_run_path() -> None:
    app = reins.endpoint(Agent(model=FakeModel.from_script([{"final": "x"}])), read_only=True)
    status, _h, _p = _call(app, "POST", "/run", body={"goal": "delete order 1"})
    assert status == 404  # the write path does not exist at all


def test_read_only_ask_cannot_write() -> None:
    # Even routed to /ask, a write is refused by the harness (ask() is read-only).
    model = FakeModel.from_script([{"call": "delete_order", "args": {"order_id": 1}}])
    app = reins.endpoint(
        Agent(model=model, capabilities=[find_orders, delete_order]), read_only=True
    )
    status, _h, payload = _call(app, "POST", "/ask", body={"goal": "delete order 1"})
    assert status == 200  # the HTTP call succeeds…
    assert "read-only" in _json(payload)["answer"].lower()  # …but the write was refused


def test_run_path_appears_when_writes_enabled() -> None:
    model = FakeModel.from_script(
        [{"call": "delete_order", "args": {"order_id": 1}}, {"final": "deleted"}]
    )
    app = reins.endpoint(
        Agent(model=model, capabilities=[delete_order], approve=lambda r: True),
        read_only=False,
    )
    status, _h, payload = _call(app, "POST", "/run", body={"goal": "delete order 1"})
    assert status == 200
    assert _json(payload)["answer"] == "deleted"


# --- identity → RLS ---------------------------------------------------------------


def test_identity_flows_from_request_into_rls() -> None:
    notes = {"user:alice": ["alice-note"], "user:bob": ["bob-note"]}

    @capability(reads=True, scope="user")
    def list_my_notes(ctx: Context) -> list[str]:
        "List the caller's notes."
        assert ctx.principal is not None
        return notes[ctx.principal]

    model = FakeModel.from_script(
        [{"call": "list_my_notes", "args": {}}, {"final": "here are your notes"}]
    )
    app = reins.endpoint(
        Agent(model=model, capabilities=[list_my_notes]),
        identity=lambda environ: environ.get("HTTP_X_USER"),
    )
    status, _h, _p = _call(
        app, "POST", "/ask", body={"goal": "my notes"}, environ={"HTTP_X_USER": "user:alice"}
    )
    assert status == 200
    # alice's request only ever touched alice's data (bob's never entered the run).
    shown = " ".join(m.text or "" for msgs, _ in model.calls for m in msgs)
    assert "alice-note" in shown
    assert "bob-note" not in shown


def test_no_identity_means_no_principal() -> None:
    @capability(reads=True, scope="user")
    def list_my_notes(ctx: Context) -> list[str]:
        "List the caller's notes."
        return ["secret"]

    model = FakeModel.from_script([{"call": "list_my_notes", "args": {}}, {"final": "no access"}])
    app = reins.endpoint(Agent(model=model, capabilities=[list_my_notes]))  # no identity=
    status, _h, _p = _call(app, "POST", "/ask", body={"goal": "my notes"})
    assert status == 200
    # A scope-annotated capability with no principal fails closed inside the run.
    shown = " ".join(m.text or "" for msgs, _ in model.calls for m in msgs)
    assert "secret" not in shown


# --- the <reins-chat> widget ------------------------------------------------------


def test_widget_is_served_and_wired_to_the_endpoint() -> None:
    app = reins.endpoint(Agent(model=FakeModel.from_script([{"final": "x"}])), path="/api/ask")
    status, headers, payload = _call(app, "GET", "/reins-chat")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    html = payload.decode()
    assert "<reins-chat" in html
    assert 'endpoint="/api/ask"' in html
    assert 'credentials: "include"' in html  # uses the existing session cookie


def test_chat_widget_helper_targets_the_given_endpoint() -> None:
    html = reins.chat_widget("/custom/ask")
    assert 'endpoint="/custom/ask"' in html
    assert "customElements.define" in html


def test_widget_can_be_disabled() -> None:
    app = reins.endpoint(Agent(model=FakeModel.from_script([{"final": "x"}])), widget=False)
    status, _h, _p = _call(app, "GET", "/reins-chat")
    assert status == 404


# --- reins.cli(agent) -------------------------------------------------------------


def test_cli_runs_an_agent_interactively() -> None:
    model = FakeModel.from_script([{"call": "find_orders", "args": {}}, {"final": "2 orders"}])
    agent = Agent(model=model, capabilities=[find_orders])
    prompts = iter(["how many orders?", "exit"])
    out: list[str] = []
    rc = reins.cli(agent, input_fn=lambda _p: next(prompts), out=out.append)
    assert rc == 0
    assert any("2 orders" in line for line in out)
