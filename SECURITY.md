# Security

Reins is infrastructure you point at your own data, so its threat model is
explicit: **treat every byte the model produces as untrusted input** — tool
names, arguments, and generated code are the new SQL injection. The harness is
built so a wrong or adversarial model output causes, at worst, a needless refusal
or approval prompt — never an unsafe write or a data leak.

## What the harness guarantees (by construction)

- **`ask()` is structurally read-only.** A write/destructive capability requested
  under `ask()` is refused before anything executes — not by prompt, in code.
- **Writes are gated:** deterministic policy → human approval → audit → execute.
  The default never auto-executes a write; the gate **fails closed** (a denied,
  headless, or crashing approval handler means the write does not run).
- **Policy lives in code, never in the prompt.** The model can't see or reason
  around the rules; the system prompt is tiny and immutable.
- **Arguments are validated against the capability schema before execution.** The
  schema is the prepared statement.
- **Identity propagates; capabilities are row-scoped.** A `scope=`-annotated
  capability refuses to run without a principal, and only sees that caller's rows.
- **No raw SQL by default.** The agent calls *your* typed functions / ORM methods,
  inheriting your validation and authorization.
- **Audit and trace are PII-redacted;** secrets are never logged. If a write
  cannot be audited, it does not run.
- **Everything is bounded:** token / cost / time / turn budgets, bounded results,
  bounded program runtime — a runaway is a stopped run, not an incident.

## Sandbox tiers (honestly labeled)

Code-mode runs the agent's program in a `Sandbox`:

| Tier | Backend | Use |
| ---- | ------- | --- |
| Development | `SubprocessSandbox` (default) | whitelisted imports, no `open`/`exec`/`eval`/net — an **isolation aid, not a hard boundary** |
| Production | `DockerSandbox` | no network, read-only rootfs, memory/pid limits, all caps dropped |

Whatever the backend, every capability call a program makes is re-gated by the
harness — the sandbox isolates *computation*, never *authority*.

## Deployment checklist

Before pointing Reins at production data:

- [ ] Expose reads with `ask()`; only reach for `run()` where writes are intended.
- [ ] Give the agent a **least-privilege database user** — grant only the tables
      and operations you actually expose, nothing more.
- [ ] Set `principal=` from your **real** authentication (session cookie / JWT),
      and annotate per-user capabilities `@capability(scope="user")`.
- [ ] On a server (no terminal), wire a real `approve=` handler — a review queue,
      a Slack confirm, an allow-list. The default terminal gate fails closed.
- [ ] Use `sandbox=DockerSandbox()` (or stricter) in production; the subprocess
      tier is for development only.
- [ ] Set a `Budget` (`max_tokens` / `max_cost` / `max_seconds`).
- [ ] Plug an audit sink (`audit=`) that persists somewhere durable; confirm your
      capability arguments don't smuggle secrets past redaction.
- [ ] Keep raw-SQL execution off (it is off by default).
- [ ] Review `agent.explain()` / `--trace` output for the flows you ship.

## Reporting a vulnerability

Please report security issues privately to **shamsuddinpias0@gmail.com** rather
than opening a public issue. We aim to acknowledge within a few days.
