# Contributing to Reins

Reins is built **one milestone per PR**, in the order defined in `CLAUDE.md` §10.
Quality is enforced per milestone by a single gate — never deferred.

## Setup

```bash
make setup     # create packages/python/.venv, install the package + dev tools,
               # and install the pre-commit hook (prefers `uv`, falls back to pip)
```

Requires Python 3.11+.

## The gate

```bash
make check     # lint + types + test + conformance — must be green before "done"
```

Individual targets: `make lint` (ruff check + format), `make types`
(mypy --strict on `src/`), `make test` (pytest), `make conformance` (the
cross-language golden + capability-linter cases).

## How we work

1. Read `docs/ENGINEERING.md` (how to reason + write robust code) before coding.
2. Restate the milestone's acceptance criteria; name the edge cases and the
   `CLAUDE.md` §2 invariants that apply.
3. **Test-first** — write the happy *and* unhappy paths (denied write, injected
   args, budget exhaustion, malformed model output), plus a `spec/conformance`
   case for any cross-language behavior. Then make them pass with the smallest
   correct code.
4. `make check` green. Re-read your diff as a hostile reviewer; harden against
   the failure modes in `CLAUDE.md` §2 (fail closed, validate input, bound every
   loop/retry/timeout, honor cancellation, never log secrets or PII).
5. Update docs and `explain()` / trace if behavior changed. Every new error path
   ends with a `→ next step`.

## Non-negotiables

The safety, efficiency, and ease invariants in `CLAUDE.md` §2 are never weakened
to make a test or demo pass. If a task seems to require it, stop and open a
discussion instead.

## Commits & PRs

- **Conventional Commits** (`feat:`, `fix:`, `chore:`, `docs:`, `test:` …),
  scoped to the milestone — e.g. `chore(m0.1): repo + tooling scaffold`.
- One milestone per PR; tick its checkbox in `CLAUDE.md` §10 once the §7
  Definition of Done passes.
