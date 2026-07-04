# Extending Reins

Every stochastic or environment-specific part of Reins is a small, injected
interface — a `Protocol` you can implement and pass in. The loop, policy, and
gating stay deterministic and owned by the harness; you swap the edges.

Contents: [Model](#a-model-any-llm) · [Capabilities](#capabilities-your-own-and-auto-exposed)
· [Policy](#policy-your-write-rules) · [Approval gate](#approval-gate) ·
[Audit sink](#audit-sink) · [Sandbox](#sandbox-where-code-mode-runs) ·
[ORM backend](#orm-backend)

---

## A Model (any LLM)

`Model` is the **only** nondeterministic component. Implement one method and pass
it as `Agent(model=...)`. The harness hands you the conversation and the tool
surface (intent-level `ToolSpec` only — never policy metadata) and expects one
`ModelResponse`.

```python
from reins.model import Model, ModelParams
from reins.types import Message, ModelResponse, ToolSpec

class MyModel:
    def complete(
        self, messages: list[Message], tools: list[ToolSpec], params: ModelParams
    ) -> ModelResponse:
        ...   # call your provider; map its reply to a ModelResponse

agent = Agent(model=MyModel(), capabilities=[...])
```

Built-in providers ship already (`Agent(model="openai:gpt-4o")`, etc.); write a
`Model` only for a provider Reins doesn't cover. For tests, use the scriptable
`reins.model.FakeModel` — no network, fully deterministic.

## Capabilities: your own and auto-exposed

A capability is any typed function wrapped by `@capability`. To mix hand-written
capabilities with ORM auto-expose, build the generated set and append:

```python
from reins import Agent, capability
from reins.orm import build_orm
from reins.types import Context

capabilities, schema = build_orm(engine, models=[Order], can_write=True)

@capability(reads=True, scope="user")
def find_my_orders(ctx: Context) -> list[dict]:
    """The caller's own orders."""
    return store.orders(user=ctx.principal)

capabilities.append(find_my_orders)
agent = Agent(capabilities=capabilities, schema=schema)
```

A `context` / `ctx` parameter is injected by the harness (never seen by the model)
and carries the caller's `principal` for row-level security.

## Policy: your write rules

The default policy implements the autonomy ladder (reads free; writes gated by
`READ_ONLY → DRAFT_WRITES → APPROVED_WRITES → TRUSTED`). Replace it to encode your
own rules — e.g. auto-allow small refunds, always gate deletes. Policy is
enforced in code and **never** shown to the model.

```python
from reins.policy import AutonomyLevel, Decision, DefaultPolicy, PolicyDecision, ProposedAction
from reins.types import Access, Message

class RefundUnder100Policy(DefaultPolicy):
    def check_actions(
        self, actions: list[ProposedAction], autonomy: AutonomyLevel
    ) -> PolicyDecision:
        for a in actions:
            if a.capability.name == "refund_order" and a.call.arguments.get("amount", 0) < 100:
                return PolicyDecision(Decision.ALLOW)
        return super().check_actions(actions, autonomy)   # fall back to the ladder

agent = Agent(capabilities=[...], policy=RefundUnder100Policy())
```

Return `Decision.ALLOW`, `DENY` (with a reason), or `REQUIRE_APPROVAL`. When in
doubt, deny or require approval — never fail open.

## Approval gate

Replace the terminal prompt with your own gate: a review queue, a Slack confirm,
an allow-list. Any `request -> bool` callable works, or an object with `.approve`.

```python
from reins import ApprovalRequest

def gate(request: ApprovalRequest) -> bool:
    # request.actions, request.reason, request.bulk_count, request.preview()
    return request.bulk_count is None          # e.g. auto-approve single-row writes

agent = Agent(capabilities=[...], approve=gate)
```

The gate **fails closed**: if it raises or denies, the write does not run. A
non-callable `approve=` is rejected at construction, not silently.

## Audit sink

Every write attempt is recorded. The default keeps records in memory
(`agent.audit.records`); send them anywhere by implementing `record`.

```python
from reins.audit import AuditRecord

class LoggingSink:
    def record(self, record: AuditRecord) -> None:
        my_logger.info("%s %s by %s", record.decision, record.capability, record.principal)

agent = Agent(capabilities=[...], audit=LoggingSink())
```

Arguments arrive PII-redacted. **No audit, no action:** if `record` raises, the
write it would have logged does not execute.

## Sandbox: where code-mode runs

Code-mode programs run in a `Sandbox`. `SubprocessSandbox` (default) is a
development-tier isolation aid; `DockerSandbox` is the production tier (no network,
read-only, resource-limited). Implement your own for E2B / Modal / a microVM:

```python
from reins.sandbox import SandboxOutcome, BridgeCall

class MySandbox:
    def run(
        self, program: str, capabilities: list[str], call: BridgeCall, *, timeout: float = 10.0
    ) -> SandboxOutcome:
        ...   # execute `program` in isolation; route each capability call through `call`

agent = Agent(capabilities=[...], sandbox=MySandbox())
```

Whatever the backend, every capability call a program makes is routed back through
the harness's gate chain — the sandbox isolates *computation*, never *authority*.

## ORM backend

SQLAlchemy and Django ship in `reins.orm`. Both are thin adapters over
`reins.orm.common` (a `Backend` protocol + the shared capability generation), so a
new ORM means implementing six operations (`find` / `count` / `get` / `create` /
`update` / `delete`) that return trimmed rows, plus introspection into
`ModelInfo`. See `reins/orm/sqlalchemy.py` for the reference backend.

---

Everything here is optional — the defaults are safe and complete. Reach for an
extension point only when your environment demands it.
