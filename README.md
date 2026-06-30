# Reins

**A lightweight agent harness you bolt onto your app so an LLM can operate it — safely and cheaply.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

Point Reins at functions you already have — or your ORM models — and hand it a goal in
plain language. It works out which of your operations to call and in what order, runs
them **through your own code (never raw SQL by default)**, and returns the result, with
near-zero token overhead and safe-by-default writes. There is no graph to draw: you
expose your app, and the agent operates it.

> **The wedge: Reins gives the model your _verbs_, not your _tables_.**
> Intent-named functions like `refund_order` carry the meaning that raw schemas and
> OpenAPI specs strip away — which is exactly where text-to-SQL and auto-generated tool
> layers lose their reliability.

## Example

```python
from reins import Agent, capability

@capability
def find_orders(status: str) -> list[dict]:
    """Find orders by their current status."""
    return store.orders(status=status)          # your existing code

@capability
def refund_order(order_id: int) -> dict:
    """Refund a customer's order by its public order number."""
    return payments.refund(order_id)            # your existing code

agent = Agent(capabilities=[find_orders, refund_order])

# ask() is read-only: it can never call a write capability.
result = agent.ask("how many orders are stuck in processing?")
print(result.output.text)

# run() may write: writes are gated before they execute.
agent.run("refund order 8842")
```

The model is read from your environment (`ANTHROPIC_API_KEY`), or pass one explicitly
with `Agent(model="anthropic:claude-...")`.

## Why Reins

- **Your verbs, not your tables.** The model sees intent-named operations with real
  descriptions and types — not opaque columns or hand-written JSON schemas.
- **Safe by default.** `ask()` is read-only and can never write; writes are opt-in and
  gated. Beginners cannot cause damage, and the boundary is enforced in code, not in a
  prompt the model can reason around.
- **You hold the reins.** Reins runs *your* functions, so it inherits your existing
  validation, authorization, and business rules. No raw SQL against your database.
- **Tiny and transparent.** The core is a small, readable loop you own — no hidden
  graph, no framework internals to reverse-engineer when you hit a ceiling.
- **Provider-agnostic.** Bring any model through a thin adapter; Anthropic ships in the
  box.

## How it works

1. **Register.** Decorate the functions you want to expose. Reins reads their type hints
   and docstrings to derive a schema and classify each as read or write.
2. **Discover.** The agent is shown your intent-named operations and the goal.
3. **Plan.** It decides which operations to call, and in what order, to reach the goal.
4. **Govern.** Reads run freely; writes pass through policy, approval, and audit before
   anything touches your data.
5. **Return.** Only results cross back to the model, and every decision and call is
   traceable.

## Installation

Reins requires **Python 3.11+**.

```bash
pip install reins                 # core (first PyPI release coming soon)
pip install "reins[anthropic]"    # with the Anthropic model backend
```

Until the first published release, install from source:

```bash
git clone https://github.com/shamspias/reins
pip install -e "reins/packages/python[anthropic]"
export ANTHROPIC_API_KEY=sk-...
```

## The two verbs

Reins has exactly two entry points, and they *are* the safety model:

- **`ask(goal)`** — the agent may only read. It never writes, never blocks, and never
  prompts. Use it for dashboards, search, analytics, and "what is…".
- **`run(goal)`** — the agent may read and write. Writes are detected automatically and
  gated for approval. Use it for "do something": create, update, refund, send.

Read/write classification is automatic and errs toward "write" when a name is ambiguous,
so a wrong guess only ever adds a needless approval — it can never let a write slip
through as a read. Override it explicitly when you know better:

```python
@capability(reads=True)
def export_report(month: str) -> bytes: ...

@capability(destructive=True, confirm=True)
def deactivate_user(user_id: int) -> None: ...
```

## Reins vs. graph frameworks

| Aspect              | Graph frameworks (LangChain / LangGraph) | Reins                                            |
| ------------------- | ---------------------------------------- | ------------------------------------------------ |
| Mental model        | You draw a graph of nodes and edges      | You expose functions; the agent finds the path   |
| Control flow        | Predefined by you                        | Decided by the model, within your guardrails     |
| Adding it to an app | Wrap logic in framework primitives       | Decorate functions you already have              |
| CRUD on a database  | Often raw SQL tools, or custom nodes     | Calls your typed methods, inheriting your auth   |
| Debuggability       | Debug the framework's internals          | Debug your own small loop and your own functions |
| Languages           | Python-first                             | Python today; Go and TypeScript planned          |

## Status

Reins is in **early development**, Python first. The core works today: typed capabilities
derived from your functions, the agent loop, the `ask()` / `run()` safety boundary, and
pluggable model backends. APIs may change before `1.0`.

### Roadmap

- [x] Typed capabilities from your functions — schema and read/write classification
- [x] The agent loop with `ask()` / `run()` and a strict read-only boundary
- [x] Provider-agnostic model backends (Anthropic included)
- [ ] Policy, human approval, and audit for writes
- [ ] ORM auto-exposure (SQLAlchemy, Django) and a zero-code `reins chat <db-url>`
- [ ] Code-mode execution and on-demand capability/schema discovery
- [ ] Row-level security and identity propagation
- [ ] Go and TypeScript packages, verified against one shared conformance suite

## Development

```bash
make setup     # create a local venv, install the package and dev tools, set up pre-commit
make check     # the gate: ruff + mypy --strict + tests + conformance
```

The Python package lives in `packages/python`. See [CONTRIBUTING.md](CONTRIBUTING.md) for
the workflow and conventions.

## License

[MIT](LICENSE)
