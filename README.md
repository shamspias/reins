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

The model is auto-detected from your environment (set any of `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`), or named
explicitly: `Agent(model="openai:gpt-4o")`, `Agent(model="ollama:llama3.1")`, or just
`Agent(model="claude-opus-4-8")`.

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
- **Any model.** Anthropic, OpenAI, Gemini, Groq, Z.ai (GLM), and local servers
  (Ollama, vLLM) ship in the box; anything OpenAI-compatible works too.

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
pip install "reins[anthropic]"    # with the Anthropic backend
pip install "reins[openai]"       # with OpenAI / Gemini / Groq / Z.ai / Ollama / vLLM
```

Until the first published release, install from source:

```bash
git clone https://github.com/shamspias/reins
pip install -e "reins/packages/python[anthropic,openai]"
export ANTHROPIC_API_KEY=sk-...   # or any provider key from the table below
```

## Bring any model

One line picks the provider — everything else stays identical:

```python
Agent(model="anthropic:claude-opus-4-8")   # or just "claude-opus-4-8"
Agent(model="openai:gpt-5.1")              # or just "gpt-5.1"
Agent(model="gemini:gemini-2.5-flash")
Agent(model="groq:llama-3.3-70b-versatile")
Agent(model="zai:glm-4.6")                 # Z.ai / GLM ("glm:..." works too)
Agent(model="ollama:llama3.1")             # local, no API key needed
Agent(model="vllm:Qwen/Qwen2.5-72B")       # your own vLLM server
```

| Provider | Env var for the key | Notes |
| -------- | ------------------- | ----- |
| Anthropic | `ANTHROPIC_API_KEY` | native adapter |
| OpenAI | `OPENAI_API_KEY` | `OPENAI_BASE_URL` points it at any compatible proxy |
| Google Gemini | `GEMINI_API_KEY` | via Gemini's OpenAI-compatible endpoint |
| Groq | `GROQ_API_KEY` | |
| Z.ai (GLM) | `ZAI_API_KEY` | |
| Ollama | — (none) | local; `OLLAMA_BASE_URL` overrides `http://localhost:11434/v1` |
| vLLM | `VLLM_API_KEY` (optional) | `VLLM_BASE_URL` overrides `http://localhost:8000/v1` |

With no `model=` at all, Reins auto-detects from the environment in that table's
order. Every resolution failure tells you exactly what to set next.

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

## Approvals: the human gate

When `run()` reaches a write that needs approval, it pauses and shows you exactly
what it wants to do before anything executes:

```
⚠ Reins wants to run a DESTRUCTIVE WRITE: refund_order(order_id=8842)
Approve? [y]es / [n]o / [s]how
```

`[s]how` reveals the unabridged calls and arguments as JSON. The gate **fails
closed**: denying, running headless (no terminal), or an approval handler that
crashes all mean the write does not run — and the attempt is recorded.

The terminal prompt is just the default. Plug in your own gate — any
`request -> bool` callable or an object with an `approve` method:

```python
agent = Agent(
    capabilities=[find_orders, refund_order],
    approve=lambda request: request.bulk_count is None,  # e.g. auto-deny bulk writes
)
```

Anything touching **more than one row** needs an explicit confirm at every
autonomy level — even for agents trusted to write freely (`confirm_bulk=True`
by default).

## Identity, row-level security, and the audit trail

Tell the agent who it's acting for, and every capability knows it — your existing
per-user logic keeps working, and one user's agent can never see another's rows:

```python
from reins import Agent, Context, capability

@capability(reads=True, scope="user")
def list_my_orders(ctx: Context) -> list[dict]:
    """List the calling user's orders."""
    return store.orders(user=ctx.principal)      # scoped to the caller — RLS

agent = Agent(capabilities=[list_my_orders], principal="user:alice")
agent.ask("what did I order last week?")          # ctx.principal == "user:alice"
agent.ask("…", principal="user:bob")              # per-call override (e.g. per request)
```

A `scope`-annotated capability **refuses to run** if no principal is set — fail
closed, with a message telling you what to pass. And every write attempt — refused,
denied, approved, or executed — lands in a structured, PII-redacted audit trail:

```python
for r in agent.audit.records:
    print(r.timestamp, r.principal, r.decision, r.capability, r.arguments)
# 2026-07-03 …  user:alice  approved  update_password  {'user_id': 7, 'password': '[redacted]'}
# 2026-07-03 …  user:alice  executed  update_password  {'user_id': 7, 'password': '[redacted]'}
```

Plug in your own sink with `Agent(audit=...)` — if a write can't be audited, it
doesn't run.

## Code-mode: perfect work, near-zero tokens

For anything beyond a trivial call, the agent doesn't ping-pong data through the
model — it **writes a short program** that runs in a sandbox, calls your
capabilities as plain functions, and prints back only the answer:

```python
# what the model writes (and you can inspect):
overdue = find_orders(status="overdue")
print(f"{len(overdue)} overdue orders, {sum(r['total'] for r in overdue)} owed")
```

A thousand rows flow from your function into the sandbox, get aggregated there,
and a **single line** returns to the model. Crucially, the sandbox isolates
computation, **never authority**: every call a program makes passes the same
policy → approval → audit → row-level-security chain as a direct call — a
gated write pauses at your terminal even mid-program.

The default sandbox is a whitelisted-import subprocess (great for development;
honestly labeled: not a hard security boundary). For production:

```python
from reins import Agent, DockerSandbox

agent = Agent(capabilities=[...], sandbox=DockerSandbox())  # no network, read-only
agent = Agent(capabilities=[...], mode="simple")            # or: no programs at all
```

## Scale without paying for it

Registered 10 capabilities or 10,000? The model's base context is **byte-identical**:
above a small threshold, Reins shows the model one `search_capabilities` tool instead
of every schema — it discovers just the operations it needs, which then become
directly callable. Attach a schema index and your data model works the same way:

```python
from reins import Agent, SchemaIndex, TableInfo

agent = Agent(
    capabilities=every_operation_in_your_app,   # hundreds? fine.
    schema=SchemaIndex(tables=(
        TableInfo("orders", "Customer orders.", ("id", "status", "total")),
        TableInfo("refunds", "Issued refunds.", ("id", "order_id", "amount")),
    )),
)
agent.ask("how many refunds this week?")
# model: search_capabilities("refunds") → finds find_refunds → calls it. ~700 chars
# of base context — the same whether you registered 20 operations or 20,000.
```

(The ORM adapters, next on the roadmap, will build both the capabilities and the
schema index from your models automatically.)

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
derived from your functions, the agent loop, the `ask()` / `run()` safety boundary,
deterministic write policy with the autonomy ladder, the human approval gate with
previews and bulk confirm, a structured audit trail, identity propagation with
row-level scoping, and seven model providers behind one interface. APIs may change
before `1.0`.

### Roadmap

- [x] Typed capabilities from your functions — schema and read/write classification
- [x] The agent loop with `ask()` / `run()` and a strict read-only boundary
- [x] Deterministic write policy and the autonomy ladder
- [x] Human approval gate — previews, `[y]/[n]/[s]how`, pluggable `approve=`, bulk confirm
- [x] Structured audit log, identity propagation, and row-level scoping
- [x] Any model: Anthropic, OpenAI, Gemini, Groq, Z.ai/GLM, Ollama, vLLM
- [x] On-demand capability & schema discovery — constant context at any scale
- [x] Code-mode execution — sandboxed programs, data never transits the model
- [ ] ORM auto-exposure (SQLAlchemy, Django) and a zero-code `reins chat <db-url>`
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
