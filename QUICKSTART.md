# Quickstart

Reins in fifteen seconds, then one rung at a time. Every rung is runnable as-is;
stop wherever you have what you need.

## 0 — Try it, no code

```bash
git clone https://github.com/shamspias/reins && cd reins
make setup && make demo
```

`make demo` needs no API key. It auto-exposes a shop's models, answers a read
question, scopes orders per logged-in user, pauses a write at an approval prompt,
and prints the audit trail — the whole story in one command.

Already have a database? Operate it straight from the terminal:

```bash
pip install "reins[sqlalchemy]"
reins inspect sqlite:///shop.db                 # no key: see what an agent could do
export ANTHROPIC_API_KEY=sk-...                 # or OPENAI_API_KEY / GEMINI_API_KEY / …
reins ask  sqlite:///shop.db "how many orders shipped last week?"
reins chat sqlite:///shop.db                     # interactive, read-only
```

## 1 — Your ORM

Point Reins at models you already have. No capabilities to write.

```python
from reins import Agent

# SQLAlchemy: pass a Session / sessionmaker / Engine
agent = Agent.from_orm(db, models=[User, Order])
print(agent.ask("how many orders are open for acme@corp.com?").output.text)

# Django: no db argument (the connection is global)
agent = Agent.from_orm(models=[User, Order])
```

Reins turns each model into intent-named verbs — `find_orders`,
`get_user_by_email`, … — and lookups prefer a unique natural key over an internal
id. `reins inspect <db-url>` shows exactly what it generated.

## 2 — Your functions

Have functions instead of (or alongside) an ORM? Decorate them, or hand Reins a
whole module.

```python
from reins import Agent, capability

@capability
def find_orders(status: str) -> list[dict]:
    """Find orders by their current status."""
    return store.orders(status=status)      # your existing code

agent = Agent(capabilities=[find_orders])
# or: Agent.from_module(my_ops_module)      # every public function becomes a capability
```

Each function is classified **read** or **write** by its name's verb
(`find_/get_/list_` → read; `create_/update_/delete_/refund_` → write), erring
toward "write" when unsure. Override when you know better:

```python
@capability(reads=True)
def export_report(month: str) -> bytes: ...

@capability(destructive=True, confirm=True)
def deactivate_user(user_id: int) -> None: ...
```

### The two verbs (this *is* the safety model)

```python
agent.ask("how many orders are stuck?")   # read-only — can never call a write
agent.run("refund order 8842")            # may write — writes are gated first
```

`ask()` is structurally read-only: a beginner reaching for it cannot cause damage.

## 3 — Turn on writes, safely

Writes are opt-in and gated: deterministic policy → human approval → audit.

```python
agent = Agent.from_orm(db, models=[User, Order], can_write=True)
agent.run("cancel and refund order 8842")
# ⚠ Reins wants to run a DESTRUCTIVE WRITE: refund_order(order_id=8842)
# Approve? [y]es / [n]o / [s]how
```

- **`approve=`** — replace the terminal prompt with your own gate (any
  `request -> bool`, or an object with `.approve`). The gate **fails closed**:
  denied, headless, or crashing all mean the write does not run.
- **`confirm_bulk=True`** (default) — anything touching more than one row needs
  an explicit confirm, at every autonomy level.
- **`principal=` / row-level security** — tell the agent who it acts for; a
  `@capability(scope="user")` sees only that caller's rows:

```python
@capability(reads=True, scope="user")
def list_my_orders(ctx):
    return store.orders(user=ctx.principal)   # scoped to the caller

agent = Agent(capabilities=[list_my_orders], principal="user:alice")
agent.ask("what did I order?", principal="user:bob")   # per-call override
```

- **audit** — every write attempt (refused / denied / approved / executed) lands
  in `agent.audit.records`, PII-redacted. Plug in your own sink with
  `Agent(audit=...)`; if a write can't be recorded, it doesn't run.

## 4 — Production

```python
from reins import Agent, Budget, DockerSandbox, endpoint

agent = Agent.from_orm(
    db, models=[Order, Invoice], can_write=True,
    model="openai:gpt-5.1",              # any provider — see the README table
    sandbox=DockerSandbox(),             # code-mode runs in a locked-down container
    budget=Budget(max_cost=0.50, max_seconds=30),
)

# a chat endpoint scoped by the user's existing session
app.wsgi_app = endpoint(agent, identity=lambda req: req.cookies.get("user_id"))
```

- **See what happened:** `print(agent.explain())` after any run, or
  `reins ask … --trace`.
- **Swap any part** — model, policy, approval gate, sandbox, audit sink — see
  [EXTENDING.md](EXTENDING.md).

## Where next

- The full feature tour and the provider table: [README.md](README.md).
- Extension points: [EXTENDING.md](EXTENDING.md).
- A complete app: [`examples/fastapi_sqlalchemy/`](examples/fastapi_sqlalchemy/).
