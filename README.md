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

## See it all at once

```bash
git clone https://github.com/shamspias/reins && cd reins
make setup && make demo
```

`make demo` runs the flagship example (`examples/fastapi_sqlalchemy/`) — no API
key needed. It auto-exposes a shop's SQLAlchemy models, answers a read question,
scopes orders per logged-in user, pauses a write at an approval prompt, and prints
the audit trail. `examples/fastapi_sqlalchemy/app.py` is the same agent as a real
FastAPI server with the `<reins-chat>` widget.

## No code at all: point it at a database

Already have a database? Operate it from your terminal — no models, no Python:

```bash
pip install "reins[sqlalchemy]"
export ANTHROPIC_API_KEY=sk-...        # or any provider key

reins inspect sqlite:///shop.db        # see what Reins can do (no key needed)
reins ask     sqlite:///shop.db "how many orders shipped last week?"
reins chat    sqlite:///shop.db        # an interactive, read-only Q&A session
```

`reins chat` and `ask` are **read-only** — they answer questions but never change
your data. `reins inspect` reflects your tables and prints the exact capability
surface (with read/write classification) so you can see what an agent could do
before you ever run one.

## The adoption ladder

Start at the top rung and stop wherever you have what you need — each is a strict
superset of the one above, and nothing lower ever requires the levels below it.

| Level | You have… | You write… |
| ----- | --------- | ---------- |
| **0 — Try it** | a database | nothing — `make demo`, or `reins chat <db-url>` |
| **1 — Your ORM** | SQLAlchemy / Django models | `Agent.from_orm(db, models=[...])` |
| **2 — Your functions** | functions or a module | `@capability` / `Agent.from_module(mod)` |
| **3 — Writes, safely** | something to change | `can_write=True`, `approve=`, `principal=` (RLS) |
| **4 — Production** | users and traffic | a provider, `DockerSandbox()`, `endpoint(...)`, budgets |

Full copy-paste walkthrough: **[QUICKSTART.md](QUICKSTART.md)**. Swapping in your
own model, policy, approval gate, sandbox, or audit sink:
**[EXTENDING.md](EXTENDING.md)**.

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

### Already have an ORM? Skip the decorating.

Point Reins at your **SQLAlchemy or Django** models and it generates the
capabilities for you — intent-named verbs, not raw tables — with **zero
hand-written code**:

```python
from reins import Agent

# SQLAlchemy: pass your Session / sessionmaker / Engine
agent = Agent.from_orm(db, models=[User, Order])              # reads only, safe
agent.ask("how many orders are still open for acme@corp.com?")

# Django: no db argument — the connection is global
agent = Agent.from_orm(models=[User, Order], can_write=True)  # writes opt-in, gated
agent.run("refund order 8842 and email the customer")
```

Reins introspects each model into `find_orders` / `get_user_by_email` /
`create_user` / … (lookups prefer a **unique** natural key like email over
internal ids), builds a searchable schema index, trims every response so raw
tables never flood the context, and runs it all through **your** ORM —
inheriting your constraints and validation. The same `from_orm` works
identically for SQLAlchemy and Django.

## Why Reins

- **Your verbs, not your tables.** The model sees intent-named operations with real
  descriptions and types — not opaque columns or hand-written JSON schemas.
- **Safe by default.** `ask()` is read-only and can never write; writes are opt-in and
  gated. Beginners cannot cause damage, and the boundary is enforced in code, not in a
  prompt the model can reason around.
- **You hold the reins.** Reins runs *your* functions, so it inherits your existing
  validation, authorization, and business rules. No raw SQL against your database.
- **Tiny and transparent.** The core is a small, readable loop you own — no hidden
  graph, no framework internals to reverse-engineer when you hit a ceiling. And
  `agent.explain()` shows exactly what it did: the code it wrote, every call, each
  result.
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
pip install "reins[sqlalchemy]"   # with Agent.from_orm(...) for SQLAlchemy
pip install "reins[django]"       # with Agent.from_orm(...) for Django
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

## Drop it into your web app

Add a chat endpoint to the app your users already log in to — and let their
existing session decide what each of them can see:

```python
from reins import Agent, endpoint

agent = Agent.from_orm(db, models=[Order, Invoice])

# a WSGI app: POST /ask runs read-only; identity comes from the live session
app.wsgi_app = endpoint(                       # Flask; FastAPI via WSGIMiddleware
    agent,
    identity=lambda req: req.cookies.get("user_id"),   # your auth, reused
)
```

`endpoint()` is read-only by default (no write route exists at all). The
`identity` you pass flows straight into row-level security, so one user's chat can
never surface another's rows — no second auth system. There are native
`reins.web.flask_blueprint(agent)` and `reins.web.fastapi_router(agent)` too.

Drop the chat box on any page — one self-contained element, no build step:

```python
from reins import chat_widget
page_html += chat_widget("/ask")     # renders <reins-chat>, POSTs with your cookies
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

`Agent.from_orm(...)` builds both the capabilities and this schema index from your
models automatically, so scale is free out of the box.

## Never a black box

Ask what the agent just did and get a straight answer — the operations it found,
the code it wrote, every call and its result, each approval decision:

```python
agent.run("cancel order ORD-2")
print(agent.explain())
# ── reins trace ──
# outcome: executed  ·  turns: 2
# ⚠ approval: update_order: approved (approval_required)
# • call: update_order(ref='ORD-2', status='cancelled')
# answer: Order ORD-2 has been cancelled.
```

`reins ask <db-url> "…" --trace` prints the same. Arguments are PII-redacted in
the trace, just like the audit log.

## Reins vs. graph frameworks

| Aspect              | Graph frameworks (LangChain / LangGraph) | Reins                                            |
| ------------------- | ---------------------------------------- | ------------------------------------------------ |
| Mental model        | You draw a graph of nodes and edges      | You expose functions; the agent finds the path   |
| Control flow        | Predefined by you                        | Decided by the model, within your guardrails     |
| Adding it to an app | Wrap logic in framework primitives       | Decorate functions you already have              |
| CRUD on a database  | Often raw SQL tools, or custom nodes     | Calls your typed methods, inheriting your auth   |
| Debuggability       | Debug the framework's internals          | Debug your own small loop and your own functions |
| Languages           | Python-first                             | Python, Go, and TypeScript — one shared conformance suite |

## Status

Reins is at its first **Python alpha** (`0.1.0a1`). Everything below works today:
typed capabilities derived from your functions or ORM, the `ask()` / `run()` safety
boundary, deterministic write policy with the autonomy ladder, the human approval
gate with previews and bulk confirm, a structured audit trail, identity propagation
with row-level scoping, code-mode in a sandbox, on-demand discovery, budgets, the
zero-code CLI, instant web integration, and seven model providers behind one
interface — all under a coverage-gated, `mypy --strict` build. APIs may change
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
- [x] Budgets (turns/tokens/cost/time) and history compaction that keeps the goal
- [x] SQLAlchemy **and Django** auto-exposure — `Agent.from_orm(...)`, zero capabilities
- [x] Zero-code CLI — `reins chat` / `ask` / `inspect <db-url>` and `from_module`
- [x] Instant web integration — `reins.endpoint`, `<reins-chat>`, session-identity → RLS
- [x] A flagship end-to-end example (FastAPI + SQLAlchemy, gated writes) — `make demo`
- [x] **Go package** (`packages/go`) — same verbs, passes the shared conformance suite; capabilities + classification + linter, the `Ask`/`Run` loop, full safety (policy/approval/audit/RLS), progressive disclosure, an OpenAI-compatible adapter for all providers above, GORM `FromORM`, and a `reins inspect|ask|chat <db-url>` CLI. Remaining: a native Go code-mode sandbox.
- [x] **TypeScript package** (`packages/js`) — same verbs, **passes the shared conformance suite**; the core types + `Model`/`FakeModel`, capabilities + classification + linter, the async `ask`/`run` loop, full safety (policy/approval/audit/RLS), dual progressive disclosure, an OpenAI-compatible adapter for all providers above, and `fromDrizzle` (auto-expose Drizzle models via the optional `reins/orm` subpath). Remaining: a native TS code-mode sandbox, a CLI, and `<reins-chat>`.

## Development

```bash
make setup     # create a local venv, install the package and dev tools, set up pre-commit
make check     # the gate: ruff + mypy --strict + tests + conformance
```

The Python package lives in `packages/python`. See [CONTRIBUTING.md](CONTRIBUTING.md) for
the workflow and conventions.

## Go

The Go package (`packages/go`, module `github.com/shamspias/reins/packages/go`) mirrors
the Python verbs and passes the same conformance suite. The zero-code CLI works the same
way — point it at a SQLite database and it reflects the schema into intent-level
capabilities (no models to write):

```bash
go install github.com/shamspias/reins/packages/go/cmd/reins@latest

reins inspect ./app.db                 # list the capabilities Reins would expose (no key needed)
reins ask     ./app.db "how many active customers?"   # read-only answer
reins chat    ./app.db --model groq:llama-3.3-70b-versatile
```

`ask` and `chat` are **read-only** (writes aren't even generated); `inspect --write` reveals
the gated create/update/delete. The model is resolved from `--model` (`provider:model`) or the
environment — the same seven providers as Python. In code:

```go
model, _ := reins.ResolveModel("openai:gpt-4o")            // or reins.DefaultModel() from env

// The input schema is reflected from RefundArgs; access is classified from the name.
refund := reins.Define[RefundArgs]("refund_order", "Refund an order by its id.",
    func(ctx reins.Context, args map[string]any) (any, error) {
        return refundOrder(args["order_id"])
    })

agent, _ := reins.NewAgent(model, reins.WithCapabilities(refund))
res, _ := agent.Ask("how much did order 8842 cost?")        // Ask never writes; Run gates writes
fmt.Println(res.Output.Text)
```

Run `make go-check` for the Go gate (gofmt + vet + tests). A native Go code-mode sandbox is
the one remaining piece.

## Documentation

- **[QUICKSTART.md](QUICKSTART.md)** — the adoption ladder, rung by rung, all runnable.
- **[EXTENDING.md](EXTENDING.md)** — swap in your own model, policy, approval gate,
  sandbox, or audit sink.
- **[`examples/fastapi_sqlalchemy/`](examples/fastapi_sqlalchemy/)** — a complete app
  (`make demo`, and a real FastAPI server).
- **[SECURITY.md](SECURITY.md)** — the threat model and a production deployment checklist.

## License

[MIT](LICENSE)
