"""Reins — a lightweight agent harness you bolt onto an existing app.

Point it at functions you already have (or your ORM models), hand it a goal in
plain language, and it operates your app through your own code — safely and
cheaply. See ``CLAUDE.md`` for the build plan and the non-negotiable invariants.

The user-facing surface is tiny: build an ``Agent`` (``from_orm`` / ``from_module``
/ hand-registered ``capability`` functions) and use its two verbs — ``ask()``
(read-only) and ``run()`` (writes gated). The module-level :func:`ask` / :func:`run`
are one-line shortcuts.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from reins.approval import ApprovalHandler, ApprovalRequest, TerminalApprovalHandler
from reins.audit import AuditRecord, AuditSink, MemoryAuditSink
from reins.budget import Budget
from reins.capability import capability
from reins.disclosure import SchemaIndex, TableInfo
from reins.loop import Agent
from reins.policy import AutonomyLevel
from reins.pricing import DEFAULT_PRICING, Pricing
from reins.sandbox import DockerSandbox, Sandbox, SubprocessSandbox
from reins.types import Context, RunResult, ToolSpec, TraceEvent

__all__ = [
    "DEFAULT_PRICING",
    "Agent",
    "ApprovalHandler",
    "ApprovalRequest",
    "AuditRecord",
    "AuditSink",
    "AutonomyLevel",
    "Budget",
    "Context",
    "DockerSandbox",
    "MemoryAuditSink",
    "Pricing",
    "Sandbox",
    "SchemaIndex",
    "SubprocessSandbox",
    "TableInfo",
    "TerminalApprovalHandler",
    "ToolSpec",
    "TraceEvent",
    "__version__",
    "ask",
    "capability",
    "chat_widget",
    "cli",
    "endpoint",
    "run",
]

__version__ = "0.0.0"


def cli(
    agent: Agent,
    *,
    write: bool = False,
    principal: str | None = None,
    input_fn: Any = input,
    out: Any = print,
) -> int:
    """Run an existing agent as an interactive terminal session.

    Read-only by default; ``write=True`` uses ``run()`` so gated writes prompt for
    approval inline. Returns the process exit code.
    """
    from reins.console import repl

    return repl(agent, write=write, principal=principal, input_fn=input_fn, out=out)


def endpoint(agent: Agent, **kwargs: Any) -> Any:
    """A WSGI app exposing ``POST /ask`` (and ``/run`` unless ``read_only``) for ``agent``.

    Mounts in Flask directly and in FastAPI via ``WSGIMiddleware``; pass
    ``identity=`` to derive the caller's principal from the request for RLS. See
    :func:`reins.web.endpoint`, and :func:`reins.web.flask_blueprint` /
    :func:`reins.web.fastapi_router` for framework-native routers.
    """
    from reins.web import endpoint as _endpoint

    return _endpoint(agent, **kwargs)


def chat_widget(endpoint: str = "/ask", **kwargs: Any) -> str:
    """HTML for a self-contained ``<reins-chat>`` element that POSTs to ``endpoint``."""
    from reins.web import chat_widget as _chat_widget

    return _chat_widget(endpoint, **kwargs)


def _to_agent(using: Any, **kwargs: Any) -> Agent:
    """Coerce ``using`` — an Agent, a module of functions, or capabilities — to an Agent."""
    if isinstance(using, Agent):
        if kwargs:
            raise TypeError(
                "cannot pass Agent options alongside an existing Agent"
                "\n  → configure the Agent when you build it, or pass a module/capabilities"
            )
        return using
    if isinstance(using, ModuleType | str):
        return Agent.from_module(using, **kwargs)
    return Agent(capabilities=list(using), **kwargs)


def ask(goal: str, *, using: Any, **kwargs: Any) -> RunResult:
    """One-liner read-only run: ``reins.ask(goal, using=my_module)``."""
    return _to_agent(using, **kwargs).ask(goal)


def run(goal: str, *, using: Any, **kwargs: Any) -> RunResult:
    """One-liner read-write run: ``reins.run(goal, using=my_module)`` (writes gated)."""
    return _to_agent(using, **kwargs).run(goal)
