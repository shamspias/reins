"""Framework-adapter tests for the web integration (M4.4).

Flask and FastAPI are optional and not in the dev deps, so these are guarded by
importorskip — they run wherever the framework is installed and skip elsewhere
(the live-integration pattern). The shared core is fully covered in test_web.py.
"""

from typing import Any

import pytest

from reins import Agent, capability
from reins.model import FakeModel
from reins.types import Context


@capability(reads=True)
def find_orders(status: str = "open") -> list[str]:
    "Find orders by status."
    return ["o1", "o2", "o3"]


@capability(reads=True, scope="user")
def whoami(ctx: Context) -> str:
    "Report the caller."
    return ctx.principal or "anonymous"


def _reader() -> Agent:
    return Agent(
        model=FakeModel.from_script(
            [{"call": "find_orders", "args": {"status": "open"}}, {"final": "3 open."}]
        ),
        capabilities=[find_orders],
    )


def _identity_agent() -> Agent:
    return Agent(
        model=FakeModel.from_script([{"call": "whoami", "args": {}}, {"final": "done"}]),
        capabilities=[whoami],
    )


# --- Flask ------------------------------------------------------------------------


def test_flask_blueprint_serves_ask_and_widget() -> None:
    pytest.importorskip("flask")
    from flask import Flask

    from reins.web import flask_blueprint

    app = Flask(__name__)
    app.register_blueprint(flask_blueprint(_reader()))
    client = app.test_client()

    answered = client.post("/ask", json={"goal": "how many open?"})
    assert answered.status_code == 200
    assert answered.get_json()["answer"] == "3 open."

    widget = client.get("/reins-chat")
    assert widget.status_code == 200
    assert b"<reins-chat" in widget.data


def test_flask_read_only_has_no_run_route() -> None:
    pytest.importorskip("flask")
    from flask import Flask

    from reins.web import flask_blueprint

    app = Flask(__name__)
    app.register_blueprint(flask_blueprint(_reader(), read_only=True))
    assert app.test_client().post("/run", json={"goal": "x"}).status_code == 404


def test_flask_identity_flows_from_request() -> None:
    pytest.importorskip("flask")
    from flask import Flask

    from reins.web import flask_blueprint

    agent = _identity_agent()
    app = Flask(__name__)
    app.register_blueprint(flask_blueprint(agent, identity=lambda req: req.headers.get("X-User")))
    app.test_client().post("/ask", json={"goal": "who"}, headers={"X-User": "user:alice"})
    shown = _model_context(agent)
    assert "user:alice" in shown


# --- FastAPI ----------------------------------------------------------------------


def test_fastapi_router_serves_ask_and_widget() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from reins.web import fastapi_router

    api = FastAPI()
    api.include_router(fastapi_router(_reader()))
    client = TestClient(api)

    answered = client.post("/ask", json={"goal": "how many open?"})
    assert answered.status_code == 200
    assert answered.json()["answer"] == "3 open."

    widget = client.get("/reins-chat")
    assert widget.status_code == 200
    assert "<reins-chat" in widget.text


def test_fastapi_identity_flows_from_request() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from reins.web import fastapi_router

    agent = _identity_agent()
    api = FastAPI()
    api.include_router(fastapi_router(agent, identity=lambda req: req.headers.get("x-user")))
    TestClient(api).post("/ask", json={"goal": "who"}, headers={"x-user": "user:bob"})
    assert "user:bob" in _model_context(agent)


def _model_context(agent: Any) -> str:
    model = agent._model  # type: ignore[attr-defined]
    return " ".join(m.text or "" for msgs, _ in model.calls for m in msgs)
