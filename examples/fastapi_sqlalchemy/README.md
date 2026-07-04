# Flagship example — FastAPI + SQLAlchemy

A tiny shop (customers and orders) operated by a Reins agent, showing the whole
story end to end:

- **auto-expose** — `build_orm` turns the SQLAlchemy models into intent-level
  capabilities (`find_orders`, `get_order_by_ref`, `update_order`, …) with zero
  hand-written CRUD;
- **compose** — one hand-written, identity-scoped capability (`find_my_orders`)
  lives right alongside the generated ones;
- **`ask()`** answers read questions; **`run()`** performs writes that are
  **gated for approval** before they touch the database;
- **row-level security** scopes data to whoever is logged in;
- **audit** records every write attempt, PII-redacted;
- a **`<reins-chat>`** widget on the home page, and a JSON API, both scoped by the
  request's identity.

## The deterministic walkthrough (no API key)

```bash
make demo          # from the repo root
# or:  python -m examples.fastapi_sqlalchemy.demo
```

It uses a scripted `FakeModel`, so it needs no network and no API key — you see
the harness itself: the generated capabilities, a read answer, identity-scoped
reads, a write pausing at a real approval prompt, and the audit trail.

## The real server

```bash
pip install "reins[sqlalchemy]" fastapi uvicorn
export ANTHROPIC_API_KEY=sk-...          # or OPENAI_API_KEY / GEMINI_API_KEY / …
uvicorn examples.fastapi_sqlalchemy.app:app --reload
```

Then open <http://localhost:8000/> for the chat widget, or call the API:

```bash
curl -s localhost:8000/ask -H 'Content-Type: application/json' \
     -H 'X-User: ada@shop.test' -d '{"goal": "what are my orders?"}'
```

The `X-User` header stands in for your real session cookie / JWT — swap
`_identity` in `app.py` for however your app already authenticates. Writes
(`POST /run`) are gated: on a server there is no terminal to approve at, so the
default handler fails closed — wire your own `approve=` (a review queue, a Slack
confirm, an allow-list) for real write access.

## Files

- `models.py` — plain SQLAlchemy models + a seeded in-memory database.
- `shop.py` — `build_orm` auto-expose + the scoped `find_my_orders` capability.
- `demo.py` — the deterministic walkthrough (what `make demo` runs).
- `app.py` — the FastAPI server with the JSON API and the chat widget.
