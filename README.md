# Reins

**A lightweight agent harness you bolt onto your app so an LLM can operate it — safely, and cheaply.**

You register functions you already have (or your ORM models), hand it a goal in plain language, and it figures out which of your operations to call and in what order — running them **through your own code (never raw SQL by default)**, with near-zero token overhead and safe-by-default writes. **No graph to draw.**

> The wedge: **Reins gives the model your _verbs_, not your _tables_.** Intent-named functions (`refund_order`) carry the meaning that raw schema and OpenAPI specs strip away.

```python
from reins import Agent

agent = Agent.from_orm(db)                 # auto-discovers your models; nothing else to write
agent.ask("revenue by month this year")    # ask()  = read-only, never writes
agent.run("refund order 8842")             # run()  = may write; the refund pauses for approval
```
or, zero code:
```bash
reins chat "postgresql://localhost/myapp"  # introspects your schema, read-only console
```

Status: **pre-build.** This repository currently contains the design and the build plan. The code is built milestone-by-milestone by following `CLAUDE.md`.

---

## This repo

```
CLAUDE.md            ← the build plan + non-negotiable rules (Claude Code reads this)
AGENTS.md            ← pointer for AI coding agents
PROMPTS.md           ← copy-paste prompts to drive the build with Claude Code
docs/
  ENGINEERING.md             ← how to reason + write robust code
  open-agent-harness-spec.md ← the general harness contract (OAH)
  reins-agent-harness-blueprint.md
  reins-easy-by-design.md    ← the developer-experience spec
  reins-v0.2-refined.md      ← competitive research + refinements
```

## Build it with Claude Code

1. Open this folder in **Claude Code**.
2. Send the onboarding prompt from `PROMPTS.md` §0 (it reads everything and confirms understanding).
3. Then `PROMPTS.md` §1 to start **M0.1**, and §2 to walk each milestone.

Principles: one milestone per PR · test-first (happy + unhappy paths) · `make check` green before "done" · safety invariants are never weakened to pass a test.

## Development

```bash
make setup     # create the local venv, install the package + dev tools, install pre-commit
make check     # the gate: ruff lint + mypy --strict + pytest + conformance
```

Python 3.11+. The package lives in `packages/python`. See `CONTRIBUTING.md` for the workflow.

## License

MIT — see `LICENSE`.
