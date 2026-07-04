# spec/contract.md — The Reins contract (language-neutral)

> The source of truth for **what Reins does**, independent of language. The
> Python, Go, and TypeScript packages MUST all satisfy it, proven by the shared
> conformance suite in `spec/conformance/`. Where this document and `CLAUDE.md`
> disagree, **`CLAUDE.md` wins** — open an issue.
>
> **Status: living.** Sections tagged *(from Mx.y)* describe behavior delivered
> in that milestone. Conformance cases for behavior not yet built run **xfail**
> until it exists (see §7.6), so the suite reports honest per-milestone progress.

---

## 1. The two verbs (the safety boundary)

The entire user-facing surface is two verbs; everything else is safe defaults.

- **`ask(goal)`** — read-only. It MUST be *structurally impossible* for `ask()`
  to execute a write or destructive capability. `ask()` never prompts for
  approval and never blocks. *(invariant §2.1)*
- **`run(goal)`** — may read and write. Every write is gated: deterministic
  policy → human approval → audit. Reads run freely. *(invariant §2.2)*

A beginner reaching for `ask()` cannot cause damage. That is both the safety
model and the easy model in one decision.

---

## 2. Capabilities

A **capability** is one operation the agent may perform: a typed, intent-named
function plus safety metadata. The agent calls registered capabilities, never
arbitrary SQL. *(invariant §2.3)*

- **Classification** — each capability is `read`, `write`, or `destructive`:
  - DB/ORM: `SELECT` → read; `INSERT/UPDATE/DELETE/DDL` → write.
  - Functions (verb heuristic): `get/list/find/search/fetch/count/show` → read;
    `create/add/update/set/save/delete/remove/cancel/refund/send/charge` → write.
  - **Ambiguous ⇒ write.** A wrong guess may add a needless approval; it MUST
    NOT let a write run as a read. *(invariant §2.4)*
- **Override** beats the heuristic (explicit `reads` / `destructive`). *(from M2.1)*
- **Validation** — every argument is validated against the capability's schema
  *before* execution. Model output is untrusted; the schema is the prepared
  statement. *(invariant §2.6)*
- **Identity & RLS** — the caller's principal propagates into every call;
  capabilities are scoped to it, so the agent can only touch what that user
  could. *(invariants §2.7–§2.8)*

---

## 3. The Capability Linter *(from M1.3)*

Auto-generated capabilities MUST pass the linter before being shown to the model
*(invariant §2.15)*. The linter conformance cases assert these violation codes:

| code | meaning |
|---|---|
| `opaque_name` | cryptic / non-intent name (e.g. `tbl_ord_upd`) |
| `missing_description` | empty or thin description |
| `too_many_params` | too many parameters, or requires internal IDs |
| `raw_response` | returns the whole row/blob instead of trimmed fields |
| `ambiguous_overlap` | not meaningfully distinct from another capability |

`expect.lint: pass` ⇒ zero violations; `expect.lint: fail` ⇒ exactly the listed
violations (order-independent).

---

## 4. The loop — ETCSLV *(from M1.4)*

**E**ngage → (**T**hink → **C**all → **S**ense → **L**oop?) → **V**erify. The
model is the only stochastic component; everything else is deterministic and
testable with a FakeModel.

- Pre-action policy runs **before** any capability executes; denied actions
  never run. *(invariant §2.5)*
- Capability failures return **as data** so the model can adapt; only
  control-plane failures raise (typed).
- Budgets are stop conditions: a runaway loop is a bug, not an edge case
  *(invariant §2.13)*. A `Budget` caps **turns, tokens, cost (USD), and
  wall-clock time** *(cost & time from M3.3)*; each hard cap ends the run
  `stopped` / `budget_exhausted`. Time uses an injected clock (deterministic
  under test); cost is metered from token usage by a pricing table (unknown
  models cost 0 — the meter never guesses a price).
- **Context discipline** *(from M3.3)* — the system prompt is tiny, immutable,
  and policy-free. When the conversation's estimated size crosses the budget's
  soft `compact_at` limit, history is **compacted**: the oldest middle is
  dropped while the system prompt, the first user message (the goal), and the
  most recent turns are always kept, and no tool result is left without its
  originating tool call. Compaction is deterministic (no summarizer model call).
- **Code-mode is the default action interface** *(from M3.2, invariant §2.11)* —
  the model may call `run_program` with a short Python program; capabilities are
  plain functions inside it (also importable from a `capabilities` module).
  Intermediate data stays in the sandbox; only the program's printed output
  (bounded) returns to context. Direct single tool-calls remain available for
  trivial ops; `mode="simple"` removes `run_program` entirely (its name is
  reserved in code mode).
  - **The sandbox isolates computation, never authority:** every capability
    call a program makes crosses back into the harness and is gated exactly
    like a direct call — the `ask()` read-only wall, policy by autonomy,
    approval + bulk confirm, audit (no audit, no action), RLS, and schema
    validation, per call. A blocked call surfaces *inside* the program as a
    catchable `CapabilityError`; bridge-executed capabilities appear in
    `executed`.
  - Programs are bounded: source length, wall-clock timeout, per-program
    capability-call budget, and returned output size. The program's runtime
    has whitelisted stdlib imports and no `open`/`exec`/`eval`/`input`; the
    subprocess tier is an isolation aid honestly labeled as not a hard
    boundary — production uses the Docker tier (no network, read-only,
    resource-limited). *(invariant §2.9)*
- **The model boundary carries `ToolSpec`** *(from M3.1)* — name, description,
  input schema only. Policy metadata (`access` / `confirm` / `scope`) cannot
  cross to a provider even by accident: the type at `Model.complete` doesn't
  hold it. *(invariant §2.5, type-enforced)*

### 4.1 Progressive disclosure *(from M3.1)*

Never dump every capability schema or the whole data model into context
*(invariant §2.12)*.

- **Deferred capabilities** — above a small threshold (12; forceable on/off
  with `defer=`), the model sees one constant-size `search_capabilities` tool
  instead of the registry. A search's matches (bounded, best-first) are
  reported *and exposed*: they become directly callable on the next turn. The
  base context is **byte-identical** whether 13 or 10,000 capabilities are
  registered.
- **Schema-RAG** — an attached schema index is reachable only through
  `search_schema`, which returns just the relevant tables (bounded), never the
  whole data model.
- Discovery calls are **harness-internal reads**: deterministic, free at every
  autonomy level, allowed under `ask()`, never gated, never audited as writes,
  and never listed in `executed`. Their names are reserved while disclosure is
  active — a colliding user capability fails loudly at run start.
- In a golden conformance case, listing **more than 12** capabilities activates
  deferral: the scripted model must `search_capabilities` before calling one.

---

## 5. Governance *(from M2.x)*

- **Policy** *(from M2.2)* — pre-action `check_actions`, post-output
  `check_output`. Enforced in code; its constraints are **never** sent to the
  model. *(invariant §2.5)*
- **Approval** *(from M2.3)* — when the policy demands approval, the run pauses
  at an injected `ApprovalHandler` (`approve=` accepts a handler object or a
  plain `request -> bool` callable). The handler is shown the **exact calls and
  arguments** about to run. The default handler renders a preview on the
  terminal and asks `[y]es / [n]o / [s]how`; `[s]how` reveals the unabridged
  calls as JSON. The gate fails **closed**: a denied prompt, a headless run
  (no interactive terminal), an exhausted re-prompt bound, or a handler that
  raises all mean the write does not execute and the run ends `not_executed`
  with reason `approval_denied`.
- **Row-limit default / bulk confirm** *(from M2.3)* — a write whose top-level
  argument is a list of more than one item is treated as touching that many
  rows and needs explicit confirm **at every autonomy level**, including
  `TRUSTED` (`confirm_bulk=True` by default). Reads never count and never
  prompt. Real row counts arrive with the ORM adapters (M4.x); until then this
  deterministic heuristic errs toward prompting.
- **Audit** *(from M2.4)* — **every write attempt** emits a structured record:
  principal, capability, arguments (PII-redacted by key), access class,
  decision (`refused` | `denied` | `approved` | `approval_denied` |
  `executed`), reason, timestamp, trace id. Records go to a pluggable sink
  (in-memory by default). Auditing is part of the gate chain: a write that
  cannot be recorded does **not** run (no audit, no action — fail closed).
  Reads are not audited. `audited` in a run result means at least one record
  was written.
- **Identity & RLS** *(from M2.4)* — the principal set at the entry point
  propagates as a `Context` into every capability call (a `context`/`ctx`
  parameter receives it; the harness's context always beats anything
  model-supplied). A `scope`-annotated capability refuses to run without a
  principal (fail closed). Row filtering itself lives in the capability's own
  code (or the ORM adapters, M4.x).
- **Autonomy ladder** — `READ_ONLY → DRAFT_WRITES → APPROVED_WRITES (default) →
  TRUSTED`. The default never auto-executes a write. *(invariant §2.10)*

---

## 5.1 ORM auto-expose *(from M4.1)*

Point Reins at mapped models and it generates the **verbs, not the tables** — no
hand-written capabilities:

- **Reads (always, safe):** `find_<plural>` and `count_<plural>` with optional
  filters, plus `get_<singular>_by_<key>` when a lookup key exists. Lookups
  prefer a **natural key** (email / slug / name) over an internal id.
- **Writes (only with `can_write=True`, gated):** `create_<singular>`,
  `update_<singular>`, `delete_<singular>` (delete is `destructive`). Without
  `can_write`, no write capability exists — `run()` cannot write.
- Every generated capability **must pass the Capability Linter** before exposure
  (§2.15): verb-first names, real descriptions, lean parameters (capped;
  autoincrement PKs and defaulted columns are optional), and **trimmed
  responses** (bounded rows and columns — never a raw table dump). A candidate
  that cannot be made linter-clean is dropped, not shipped.
- A `SchemaIndex` is produced alongside, so the data model is available to
  progressive disclosure (§4.1), not dumped into context.
- A natural key must be **unique** — it identifies exactly one row, so
  `get_`/`update_`/`delete_` can never touch the wrong one. A hint word (email /
  slug / name) only ranks *among* the unique columns; a non-unique hint-named
  column is not a key.
- The agent runs *your* mapped classes through *your* session/ORM, inheriting
  your validation and constraints — no raw SQL (§2.3). Python ships **SQLAlchemy
  (M4.1)** and **Django (M4.2)** behind the same `build_orm` shape, so
  `Agent.from_orm(...)` is identical either way (Django needs no `db` argument —
  its connection is global). The Go/TS ports expose the same shape over their
  ORMs.

## 6. Errors (cross-cutting)

Every user-facing error ends with a `→ next step` *(invariant §2.19)*. Capability
failures are data (`ok=False`, `error`, `retryable`); control-plane failures
raise typed exceptions, never a bare `Exception`, across the loop boundary.

---

## 7. The conformance suite

One language-neutral set of cases lives in `spec/conformance/cases/*.yaml`. Each
language ships a **thin runner** that loads the cases, drives the harness with a
scripted **FakeModel**, and asserts the case's `expect` block. This is how parity
*(invariant §2.21)* and the safety invariants are locked across Python, Go, and TS.

### 7.1 Case kinds

A case is **golden** (it has a `goal`) or **linter** (it has a `capability`
block). `kind:` may be set explicitly; otherwise it is inferred from shape.

### 7.2 Golden case schema

| field | required | meaning |
|---|---|---|
| `name` | ✓ | unique identifier for the case |
| `verb` | ✓ | `ask` (read-only) or `run` (may write) |
| `goal` | ✓ | the natural-language goal handed to the agent |
| `capabilities` | ✓ | names of the capabilities available to the agent |
| `model_script` | ✓ | the scripted FakeModel decisions (§7.5) |
| `autonomy` | — | ladder level for the run: `read_only` \| `draft_writes` \| `approved_writes` (default) \| `trusted` |
| `approvals` | — | `allow` or `deny` — simulates the human at the gate. When absent, the runner uses a **deny-all** gate, so a case expecting `executed` also proves no prompt happened |
| `budget` | — | stop limits, e.g. `{ max_turns: 3 }` |
| `expect` | ✓ | the expected result (below) |

`expect` for a golden case:

| field | required | meaning |
|---|---|---|
| `outcome` | ✓ | see §7.4 |
| `reason` | — | machine-readable reason code (§7.4) |
| `executed` | — | capability names that actually ran, in order |
| `audited` | — | boolean: an audit entry was written |

### 7.3 Linter case schema

| field | required | meaning |
|---|---|---|
| `name` | ✓ | unique identifier for the case |
| `capability` | ✓ | the candidate `{ name, description, params, returns }` |
| `expect.lint` | ✓ | `pass` or `fail` |
| `expect.violations` | — | the violation codes from §3 (order-independent) |

### 7.4 Outcome & reason vocabulary

Outcomes:

| outcome | meaning |
|---|---|
| `completed` | goal met using reads only; no write executed |
| `executed` | a write ran (gated where the policy required it) and the goal was met |
| `refused` | the harness refused **before** executing (e.g. a write under `ask()`) |
| `not_executed` | a gated write was proposed but not run (approval denied) |
| `stopped` | a stop condition (e.g. budget) halted the run |

Reason codes (used with `refused` / `not_executed` / `stopped`):

| reason | meaning |
|---|---|
| `write_in_read_only` | a write was attempted under `ask()` |
| `approval_denied` | the human rejected the write at the gate |
| `budget_exhausted` | a budget / turn / time stop condition fired |

### 7.5 The FakeModel

`model_script` is the deterministic sequence of decisions a model would make.
Each step is one of:

- `{ call: <name>, args: { ... } }` — request a capability call,
- `{ program: "<source>" }` — request a code-mode program run *(from M3.2)*, or
- `{ final: "<text>" }` — produce the final answer.

The harness — **not** the script — decides whether a requested call is allowed
(policy, verb, approval). The script only encodes what the model *wants*; the
contract is about what the harness *does* with that. This is the determinism
boundary: no behavior depends on a live model.

### 7.6 xfail-until-implemented

Every case targets behavior delivered in some milestone (§4–§5). Until that
behavior exists, the runner's `drive_case` raises `HarnessNotImplemented`, which
the runner turns into an **xfail** via an imperative `pytest.xfail()` call. When
the feature lands, `drive_case` returns a real result and the case must **pass**
— and because the xfail is imperative (not a marker), an implemented-but-wrong
case is a normal **failure**, never a silent pass. Thus `make conformance` runs
and reports per-case status at every milestone, and green coverage grows
monotonically as the build proceeds.
