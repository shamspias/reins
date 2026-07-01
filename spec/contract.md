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
- Budgets (token / turn / time) are stop conditions: a runaway loop is a bug,
  not an edge case. *(invariant §2.13)*
- Code-mode is the default action interface; simple single-call mode is the
  fallback for trivial ops. *(invariant §2.11)*

---

## 5. Governance *(from M2.x)*

- **Policy** — pre-action `check_actions`, post-output `check_output`. Enforced
  in code; its constraints are **never** sent to the model. *(invariant §2.5)*
- **Approval** — destructive ops pause for human approval with a rendered
  preview; anything touching more than one row needs explicit confirm.
- **Audit** — every write attempt is logged (principal, capability, args,
  decision, timestamp; PII-redacted).
- **Autonomy ladder** — `READ_ONLY → DRAFT_WRITES → APPROVED_WRITES (default) →
  TRUSTED`. The default never auto-executes a write. *(invariant §2.10)*

---

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
| `approvals` | — | `allow` or `deny` — simulates the human at the gate |
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
| `executed` | a write ran (after approval) and the goal was met |
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
Each step is either:

- `{ call: <name>, args: { ... } }` — request a capability call, or
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
