"""The ``reins`` command-line interface (M4.3): operate a database with no code.

    reins inspect  <db-url>          # list the capabilities Reins would generate
    reins ask      <db-url> "..."    # ask one read-only question
    reins chat     <db-url>          # an interactive read-only Q&A session

``chat`` and ``ask`` are **read-only** (invariant §2.1) — they can answer
questions about your data but never change it. ``inspect`` needs no model or API
key; it just reflects the database and prints the generated capability surface
with each operation's read/write classification, so you can see exactly what an
agent would be able to do before you point one at it.

The command handlers take injectable ``model`` / ``input_fn`` / ``out`` so the
behavior is testable without a live LLM (the determinism boundary, §9).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

from reins.errors import ReinsError
from reins.loop import Agent
from reins.model import Model, resolve_model
from reins.types import Access

Printer = Callable[[str], None]


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``reins`` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    model = resolve_model(args.model) if getattr(args, "model", None) else None
    try:
        if args.command == "inspect":
            return cmd_inspect(args.db_url, can_write=args.write)
        if args.command == "ask":
            return cmd_ask(args.db_url, args.question, model=model)
        if args.command == "chat":
            return cmd_chat(args.db_url, model=model)
    except ReinsError as exc:  # hinted, user-facing — no stack trace as the last word
        print(f"reins: {exc}", file=sys.stderr)
        return 1
    return 2  # unreachable: argparse rejects unknown commands


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reins",
        description="Operate a database in plain language — safely, with no code.",
    )
    parser.add_argument("--version", action="version", version=f"reins {_version()}")
    sub = parser.add_subparsers(dest="command")

    inspect = sub.add_parser("inspect", help="list the capabilities Reins would generate")
    inspect.add_argument("db_url", help="a SQLAlchemy database URL, e.g. sqlite:///app.db")
    inspect.add_argument(
        "--write",
        action="store_true",
        help="also list the write capabilities (create/update/delete)",
    )

    ask = sub.add_parser("ask", help="ask one read-only question")
    ask.add_argument("db_url", help="a SQLAlchemy database URL")
    ask.add_argument("question", help="the question, in plain language")
    ask.add_argument("--model", help="model spec, e.g. openai:gpt-4o (default: from env)")

    chat = sub.add_parser("chat", help="an interactive read-only Q&A session")
    chat.add_argument("db_url", help="a SQLAlchemy database URL")
    chat.add_argument("--model", help="model spec, e.g. openai:gpt-4o (default: from env)")
    return parser


def cmd_inspect(db_url: str, *, can_write: bool = False, out: Printer = print) -> int:
    """Reflect the database and print the generated capability surface (no model needed)."""
    from reins.orm import build_orm
    from reins.orm.sqlalchemy import reflect_database

    engine, models = reflect_database(db_url)
    capabilities, schema = build_orm(engine, models=models, can_write=can_write)
    out(f"Reflected {len(schema.tables)} table(s) from {db_url}\n")
    for line in describe_capabilities(capabilities):
        out(line)
    if not can_write:
        out("\n(reads only — pass --write to also list create/update/delete)")
    return 0


def cmd_ask(
    db_url: str,
    question: str,
    *,
    model: Model | None = None,
    out: Printer = print,
) -> int:
    """Answer one read-only question against the reflected database."""
    agent = _agent_from_url(db_url, model=model)
    result = agent.ask(question)
    out(result.output.text or "(no answer)")
    return 0


def cmd_chat(
    db_url: str,
    *,
    model: Model | None = None,
    input_fn: Callable[[str], str] = input,
    out: Printer = print,
) -> int:
    """Interactive read-only Q&A loop over the reflected database."""
    agent = _agent_from_url(db_url, model=model)
    out(f"Connected to {db_url}. Ask a question — 'exit' or Ctrl-D to quit.")
    return repl(agent, input_fn=input_fn, out=out)


def repl(
    agent: Agent,
    *,
    write: bool = False,
    principal: str | None = None,
    input_fn: Callable[[str], str] = input,
    out: Printer = print,
) -> int:
    """An interactive prompt over an existing agent — the ``reins.cli(agent)`` loop.

    Read-only by default (``ask()``); ``write=True`` uses ``run()`` so gated
    writes prompt for approval inline. ``input_fn`` / ``out`` are injectable.
    """
    verb = agent.run if write else agent.ask
    while True:
        try:
            line = input_fn("reins> ")
        except (EOFError, KeyboardInterrupt):
            out("")
            return 0
        question = line.strip()
        if not question:
            continue
        if question in ("exit", "quit"):
            return 0
        try:
            out(verb(question, principal=principal).output.text or "(no answer)")
        except ReinsError as exc:
            out(f"error: {exc}")


def describe_capabilities(capabilities: list[Any]) -> list[str]:
    """One aligned line per capability: access class, name, params, first doc line."""
    lines: list[str] = []
    for cap in sorted(capabilities, key=lambda c: c.spec.name):
        params = ", ".join(cap.spec.input_schema.get("properties", {}))
        first_line = (cap.spec.description or "").splitlines()
        summary = first_line[0] if first_line else ""
        lines.append(f"  [{_access_label(cap.spec.access):>11}] {cap.spec.name}({params})")
        if summary:
            lines.append(f"                {summary}")
    return lines


def _access_label(access: Access) -> str:
    return access.value


def _agent_from_url(db_url: str, *, model: Model | None) -> Agent:
    from reins.orm.sqlalchemy import reflect_database

    engine, models = reflect_database(db_url)
    # chat/ask are read-only: never even generate write capabilities.
    return Agent.from_orm(engine, models=models, can_write=False, model=model)


def _version() -> str:
    from reins import __version__

    return __version__


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
