"""The harness loop and the Agent.

Engage → (Think → Call → Sense → Loop?) → Verify — the ETCSLV lifecycle. The model is
the only stochastic step; the gate, dispatch, and stop logic are deterministic and
owned here (invariant §2.14). Code-mode is the default action interface (M3.2,
§2.11): the model may write a short sandboxed program that calls capabilities as
plain functions — bulk data stays in the sandbox, only the printed result returns —
while direct single tool-calls remain for trivial ops (``mode="simple"`` disables
programs entirely).

Safety boundary: ``ask()`` is structurally read-only — if the model requests a
write/destructive capability the run is refused before anything executes (§2.1).
``run()`` may write; a write travels policy → human approval (M2.3) → audit →
execution (M2.4). Every write attempt — refused, denied, gated, or executed — emits
a structured, PII-redacted audit record, and a write that cannot be audited does
not run (no audit, no action). All of it applies identically to calls made from
inside a program (the sandbox isolates computation, never authority). The caller's
principal propagates as a :class:`reins.types.Context` into every capability call
(§2.7); the gate and the policy fail closed, and neither is ever described to the
model (§2.5).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from reins.approval import (
    ApprovalHandler,
    ApprovalLike,
    ApprovalRequest,
    TerminalApprovalHandler,
    as_approval_handler,
    bulk_count,
)
from reins.audit import AuditRecord, AuditSink, MemoryAuditSink, now, redact_arguments
from reins.budget import Budget
from reins.capability import BoundCapability
from reins.clock import Clock, MonotonicClock
from reins.conversation import compact
from reins.disclosure import Disclosure, SchemaIndex
from reins.errors import CapabilityError, SandboxError
from reins.model import Model, ModelParams, default_model, resolve_model
from reins.policy import AutonomyLevel, Decision, DefaultPolicy, Policy, ProposedAction
from reins.pricing import Pricing
from reins.registry import CapabilityRegistry
from reins.rls import scope_violation
from reins.sandbox import Sandbox, SubprocessSandbox
from reins.stop import GoalReached, MaxTurns, StopCondition, first_stop
from reins.types import (
    Access,
    CapabilityResult,
    Context,
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunResult,
    RunState,
    ToolCall,
    ToolSpec,
    Usage,
)

SYSTEM_PROMPT = (
    "You operate an application through its registered capabilities. Use them to "
    "accomplish the user's goal, then reply with a final answer."
)

RUN_PROGRAM = "run_program"
_MAX_PROGRAM_CHARS = 20_000  # bound the model-written source (§2.13)

# Code-mode (M3.2, invariant §2.11): the model's primary action for non-trivial
# work is a short sandboxed program; direct single calls remain for trivial ops.
_RUN_PROGRAM_SPEC = ToolSpec(
    name=RUN_PROGRAM,
    description=(
        "Run a short Python program in a sandbox. Every available operation is a "
        "plain function (also importable from the 'capabilities' module) — call "
        "them, loop/filter/aggregate over their results locally, and print() only "
        "what you need back. Prefer this over many separate tool calls whenever "
        "you would otherwise pass data through yourself."
    ),
    input_schema={
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Python source"}},
        "required": ["code"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True, slots=True)
class RunReport:
    """The detailed result of one run: the run-level outcome plus telemetry.

    ``ask()`` / ``run()`` return the public :class:`RunResult`; this richer report is
    what the conformance suite asserts and (from M5.1) what ``explain()`` renders.
    ``outcome`` is one of refused / completed / executed / not_executed / stopped;
    ``audit`` carries this run's audit records and ``audited`` is their presence.
    """

    outcome: str
    output: Message
    finish: FinishReason
    usage: Usage
    turn: int
    executed: tuple[str, ...]
    reason: str | None = None
    audited: bool = False
    audit: tuple[AuditRecord, ...] = ()
    compactions: int = 0
    trace_id: str = ""


@dataclass(slots=True)
class _Tally:
    """Mutable per-run facts the loop accumulates — state changes in one place.

    ``wrote`` flips the final outcome from ``completed`` to ``executed``
    (spec/contract.md §7.4); it means write-classed user code was *invoked* — even
    a call that then failed may have had side effects, so reporting errs toward
    "a write happened" (fail-closed). Calls rejected by schema validation never
    reach user code and count for neither. ``audit`` holds the records the sink
    accepted this run.
    """

    executed: list[str] = field(default_factory=list)
    wrote: bool = False
    audit: list[AuditRecord] = field(default_factory=list)
    compactions: int = 0


@dataclass(frozen=True, slots=True)
class _RunEnv:
    """The immutable per-run wiring the loop's helpers share."""

    registry: CapabilityRegistry
    policy: Policy
    gate: ApprovalHandler
    confirm_bulk: bool
    audit: AuditSink
    context: Context
    can_write: bool
    autonomy: AutonomyLevel
    disclosure: Disclosure
    mode: str
    sandbox: Sandbox


def run_loop(
    model: Model,
    registry: CapabilityRegistry,
    goal: str,
    *,
    can_write: bool,
    autonomy: AutonomyLevel = AutonomyLevel.APPROVED_WRITES,
    policy: Policy | None = None,
    approve: ApprovalLike | None = None,
    confirm_bulk: bool = True,
    principal: str | None = None,
    audit: AuditSink | None = None,
    schema: SchemaIndex | None = None,
    defer: bool | None = None,
    mode: str = "code",
    sandbox: Sandbox | None = None,
    pricing: Pricing | None = None,
    clock: Clock | None = None,
    budget: Budget | None = None,
    params: ModelParams | None = None,
) -> RunReport:
    """Drive the ETCSLV loop to a stop condition and return a :class:`RunReport`."""
    budget = budget or Budget()
    params = params or ModelParams()
    clock = clock or MonotonicClock()
    if mode not in ("code", "simple"):
        raise SandboxError(
            f"unknown mode {mode!r}",
            hint="use mode='code' (default: the agent may write sandboxed programs) "
            "or mode='simple' (single tool calls only)",
        )
    if mode == "code" and registry.get(RUN_PROGRAM) is not None:
        raise SandboxError(
            f"capability {RUN_PROGRAM!r} collides with the built-in code-mode tool",
            hint=f"rename your capability — {RUN_PROGRAM!r} is reserved in code mode "
            "(or pass mode='simple')",
        )
    trace_id = uuid.uuid4().hex
    env = _RunEnv(
        registry=registry,
        policy=policy or DefaultPolicy(),
        gate=as_approval_handler(approve) if approve is not None else TerminalApprovalHandler(),
        confirm_bulk=confirm_bulk,
        audit=audit if audit is not None else MemoryAuditSink(),
        context=Context(principal=principal, trace_id=trace_id),
        can_write=can_write,
        autonomy=autonomy,
        disclosure=Disclosure(registry, schema=schema, defer=defer),
        mode=mode,
        # Cheap to construct: no process is spawned until a program actually runs.
        sandbox=sandbox if sandbox is not None else SubprocessSandbox(),
    )
    messages: list[Message] = [
        Message(role=Role.SYSTEM, text=SYSTEM_PROMPT),
        Message(role=Role.USER, text=goal),
    ]
    stop_conditions: list[StopCondition] = [GoalReached(), MaxTurns(budget.max_turns)]
    tally = _Tally()
    usage = Usage()
    last: ModelResponse | None = None
    turn = 0
    started_at = clock.now()

    while True:
        # E/L: pre-think budget guards — a runaway is stopped deterministically (§2.13).
        exhausted = _budget_stop(budget, usage, clock.now() - started_at)
        if exhausted is not None:
            return _report(
                "stopped",
                output=_synthetic(exhausted),
                finish=FinishReason.INTERRUPTED,
                usage=usage,
                turn=turn,
                tally=tally,
                trace_id=trace_id,
                reason="budget_exhausted",
            )

        # Context discipline (M3.3): keep the window bounded, never lose the goal.
        messages, compacted = compact(messages, compact_at=budget.compact_at)
        if compacted:
            tally.compactions += 1

        tools = env.disclosure.tools()
        if env.mode == "code":
            tools = [*tools, _RUN_PROGRAM_SPEC]
        resp = model.complete(messages, tools, params)  # T: think
        resp_usage = resp.usage
        if pricing is not None and params.model:  # fold token cost into the meter
            resp_usage = replace(
                resp_usage, cost=resp_usage.cost + pricing.cost_of(params.model, resp_usage)
            )
        usage = _add(usage, resp_usage)
        messages.append(resp.message)
        last = resp

        if resp.finish_reason == FinishReason.TOOL_CALLS and resp.message.tool_calls:  # C: call
            blocked = _act(resp.message.tool_calls, env, tally, messages, usage, turn)
            if blocked is not None:
                return blocked

        turn += 1  # S: sense
        state = RunState(
            messages=list(messages), turn=turn, cumulative_usage=usage, last_response=last
        )
        decision = first_stop(stop_conditions, state)  # L: loop?
        if decision is not None:
            if decision.reason == "goal_reached":  # V: verify + return
                output = resp.message
                if env.policy.check_output(output).decision is Decision.DENY:
                    return _report(
                        "refused",
                        output=_synthetic("Output withheld by policy.\n  → adjust the goal."),
                        finish=FinishReason.STOP,
                        usage=usage,
                        turn=turn,
                        tally=tally,
                        trace_id=trace_id,
                        reason="output_denied",
                    )
                return _report(
                    "executed" if tally.wrote else "completed",  # §7.4 outcome vocabulary
                    output=output,
                    finish=FinishReason.STOP,
                    usage=usage,
                    turn=turn,
                    tally=tally,
                    trace_id=trace_id,
                )
            return _report(
                "stopped",
                output=_synthetic(
                    "Stopped: turn budget exhausted.\n  → raise the budget or simplify the goal."
                ),
                finish=FinishReason.INTERRUPTED,
                usage=usage,
                turn=turn,
                tally=tally,
                trace_id=trace_id,
                reason="budget_exhausted",
            )


def _act(
    tool_calls: list[ToolCall],
    env: _RunEnv,
    tally: _Tally,
    messages: list[Message],
    usage: Usage,
    turn: int,
) -> RunReport | None:
    # Discovery calls (search_capabilities / search_schema) are harness-internal
    # reads: answered here, free at every autonomy level, allowed under ask() (§2.12).
    # A run_program call executes in the sandbox; every capability call the program
    # makes crosses back through _bridge_call, where the full gate chain applies.
    pending: list[ToolCall] = []
    for call in tool_calls:
        discovery = env.disclosure.answer(call)
        if discovery is not None:
            messages.append(_tool_message(call.id, discovery))
            continue
        if env.mode == "code" and call.name == RUN_PROGRAM:
            messages.append(_tool_message(call.id, _run_program(env, tally, call)))
            continue
        pending.append(call)

    # Resolve every requested call once; the gate and the executor share this list so
    # they cannot diverge on what a name resolves to.
    resolved = [(call, env.registry.get(call.name)) for call in pending]

    # ask(): structurally read-only — refuse the whole turn if any call writes, before
    # the policy is even consulted (invariant §2.1). The gate is unreachable from here.
    if not env.can_write:
        for call, cap in resolved:
            if cap is not None and cap.spec.access is not Access.READ:
                action = ProposedAction(call=call, capability=cap.spec)
                _audit_action(env, tally, action, "refused", "write_in_read_only")
                return _report(
                    "refused",
                    output=_synthetic(
                        f"Refused: {call.name!r} would write, but this is a read-only ask()."
                        "\n  → use run(...) if writing is intended."
                    ),
                    finish=FinishReason.STOP,
                    usage=usage,
                    turn=turn,
                    tally=tally,
                    trace_id=env.context.trace_id,
                    reason="write_in_read_only",
                )
    else:
        # run(): the policy governs this turn's resolvable actions by autonomy level (§2.2).
        proposed = [
            ProposedAction(call=call, capability=cap.spec)
            for call, cap in resolved
            if cap is not None
        ]
        if proposed:
            verdict = env.policy.check_actions(proposed, env.autonomy)
            if verdict.decision is Decision.DENY:
                for action in proposed:
                    if action.capability.access is not Access.READ:
                        _audit_action(env, tally, action, "denied", verdict.reason)
                outcome = "refused" if verdict.reason == "write_in_read_only" else "not_executed"
                return _report(
                    outcome,
                    output=_synthetic(
                        f"Not run ({verdict.reason or 'denied by policy'})."
                        "\n  → adjust the goal or the agent's autonomy level."
                    ),
                    finish=FinishReason.STOP,
                    usage=usage,
                    turn=turn,
                    tally=tally,
                    trace_id=env.context.trace_id,
                    reason=verdict.reason,
                )
            blocked = _gate_turn(
                proposed,
                verdict_requires=verdict.decision is Decision.REQUIRE_APPROVAL,
                verdict_reason=verdict.reason,
                env=env,
                tally=tally,
                usage=usage,
                turn=turn,
            )
            if blocked is not None:
                return blocked

    for call, cap in resolved:
        if cap is None:
            result = CapabilityResult(ok=False, error=f"no capability named {call.name!r}")
            messages.append(_tool_message(call.id, result))
            continue
        # A scope-refused or schema-invalid call never reaches user code (§2.6/§2.8),
        # so it must not be reported as executed or count as a write.
        violation = scope_violation(cap.spec, env.context)
        if violation is not None:
            messages.append(_tool_message(call.id, CapabilityResult(ok=False, error=violation)))
            continue
        try:
            env.registry.validate(call.name, call.arguments)
        except CapabilityError as exc:
            messages.append(_tool_message(call.id, CapabilityResult(ok=False, error=str(exc))))
            continue
        if cap.spec.access is not Access.READ:
            action = ProposedAction(call=call, capability=cap.spec)
            if not _audit_action(env, tally, action, "executed"):
                # No audit, no action (§2.2): an unrecordable write must not run.
                messages.append(
                    _tool_message(
                        call.id,
                        CapabilityResult(
                            ok=False,
                            error="audit sink failed — the write was not executed"
                            "\n  → fix the audit= sink (its .record raised)",
                        ),
                    )
                )
                continue
            tally.wrote = True  # invoked write code may have side effects even on error
        result = env.registry.call(call.name, call.arguments, context=env.context)
        tally.executed.append(call.name)
        messages.append(_tool_message(call.id, result))
    return None


def _gate_turn(
    proposed: list[ProposedAction],
    *,
    verdict_requires: bool,
    verdict_reason: str | None,
    env: _RunEnv,
    tally: _Tally,
    usage: Usage,
    turn: int,
) -> RunReport | None:
    """The human gate (M2.3): pause when the policy demands approval, or when the
    row-limit default catches a bulk write. Denied means nothing in the turn runs."""
    gated = [a for a in proposed if a.capability.access is not Access.READ]
    count = bulk_count(gated) if env.confirm_bulk else None
    needs_approval = verdict_requires
    reason = verdict_reason or "approval_required"
    if count is not None and not needs_approval:
        needs_approval, reason = True, "bulk_confirm"  # >1 row ⇒ explicit confirm (§2)
    if not needs_approval:
        return None
    # A custom policy may gate a turn with no write in it; fail closed by showing
    # the human the whole batch rather than an empty preview.
    request = ApprovalRequest(actions=tuple(gated or proposed), reason=reason, bulk_count=count)
    approved, failure = _decide(env.gate, request)
    decision = "approved" if approved else "approval_denied"
    recorded = [_audit_action(env, tally, a, decision, reason) for a in request.actions]
    if approved and all(recorded):
        return None
    if approved:  # approved but unrecordable: no audit, no action (§2.2)
        text = (
            "Not executed: the write was approved but could not be audited."
            "\n  → fix the audit= sink (its .record raised); no audit, no action."
        )
        report_reason = "audit_failed"
    elif failure is not None:
        # A broken handler denies fail-closed, but must not masquerade as a human "no".
        text = (
            f"Not executed: the approve= handler failed ({failure})."
            "\n  → fix the handler — it must accept an ApprovalRequest and return a bool."
        )
        report_reason = "approval_denied"
    else:
        text = (
            "Not executed: the write was not approved."
            "\n  → approve it at the prompt, or pass approve= to decide programmatically."
        )
        report_reason = "approval_denied"
    return _report(
        "not_executed",
        output=_synthetic(text),
        finish=FinishReason.STOP,
        usage=usage,
        turn=turn,
        tally=tally,
        trace_id=env.context.trace_id,
        reason=report_reason,
    )


def _run_program(env: _RunEnv, tally: _Tally, call: ToolCall) -> CapabilityResult:
    """Execute one agent-written program in the sandbox (code-mode, §2.11).

    The program's capability calls are served by :func:`_bridge_call`, so the
    sandbox isolates computation but never authority — ask()'s read-only wall,
    policy, approval, bulk confirm, audit, RLS, and schema validation all apply
    to every call the program makes.
    """
    code = call.arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        return CapabilityResult(
            ok=False,
            error="run_program: 'code' must be a non-empty string\n  → pass Python source in code=",
        )
    if len(code) > _MAX_PROGRAM_CHARS:
        return CapabilityResult(
            ok=False,
            error=f"run_program: the program is too long ({len(code)} chars, "
            f"max {_MAX_PROGRAM_CHARS})"
            "\n  → split the work into smaller programs",
        )
    names = [cap.name for cap in env.registry.list()]
    outcome = env.sandbox.run(
        code, names, lambda name, arguments: _bridge_call(env, tally, name, arguments)
    )
    if outcome.ok:
        return CapabilityResult(
            ok=True,
            value=outcome.output or "(the program produced no output — print() what you need)",
        )
    error = outcome.error or "the program failed"
    if outcome.output:
        error += f"\npartial output:\n{outcome.output}"
    return CapabilityResult(ok=False, error=error)


def _bridge_call(
    env: _RunEnv, tally: _Tally, name: str, arguments: dict[str, Any]
) -> CapabilityResult:
    """Gate and execute one capability call made from inside a program.

    Mirrors the direct-call path exactly, per call: the ask() read-only wall
    (§2.1), policy by autonomy (§2.2), approval + bulk confirm (M2.3), audit
    with no-audit-no-action (M2.4), then RLS + schema validation + execution.
    A blocked call is an error the *program* can handle; the run continues.
    """
    cap = env.registry.get(name)
    if cap is None:
        return CapabilityResult(ok=False, error=f"no capability named {name!r}")
    action = ProposedAction(
        call=ToolCall(id=f"prog_{len(tally.executed)}", name=name, arguments=arguments),
        capability=cap.spec,
    )
    if cap.spec.access is not Access.READ:
        if not env.can_write:
            _audit_action(env, tally, action, "refused", "write_in_read_only")
            return CapabilityResult(
                ok=False,
                error=f"{name!r} would write, but this is a read-only ask()"
                "\n  → use run(...) if writing is intended",
            )
        verdict = env.policy.check_actions([action], env.autonomy)
        if verdict.decision is Decision.DENY:
            _audit_action(env, tally, action, "denied", verdict.reason)
            return CapabilityResult(
                ok=False,
                error=f"{name!r} was not run ({verdict.reason or 'denied by policy'})"
                "\n  → adjust the goal or the agent's autonomy level",
            )
        needs_approval = verdict.decision is Decision.REQUIRE_APPROVAL
        reason = verdict.reason or "approval_required"
        count = bulk_count([action]) if env.confirm_bulk else None
        if count is not None and not needs_approval:
            needs_approval, reason = True, "bulk_confirm"
        if needs_approval:
            request = ApprovalRequest(actions=(action,), reason=reason, bulk_count=count)
            approved, failure = _decide(env.gate, request)
            decision = "approved" if approved else "approval_denied"
            recorded = _audit_action(env, tally, action, decision, reason)
            if not approved:
                detail = f" ({failure})" if failure else ""
                return CapabilityResult(
                    ok=False,
                    error=f"{name!r} was not approved{detail}"
                    "\n  → the human denied it, or fix the approve= handler",
                )
            if not recorded:
                return CapabilityResult(
                    ok=False,
                    error="audit sink failed — the write was not executed"
                    "\n  → fix the audit= sink (its .record raised)",
                )
    violation = scope_violation(cap.spec, env.context)
    if violation is not None:
        return CapabilityResult(ok=False, error=violation)
    try:
        env.registry.validate(name, arguments)
    except CapabilityError as exc:
        return CapabilityResult(ok=False, error=str(exc))
    if cap.spec.access is not Access.READ:
        # can_write is guaranteed here: a write under ask() was refused above.
        if not _audit_action(env, tally, action, "executed"):
            return CapabilityResult(
                ok=False,
                error="audit sink failed — the write was not executed"
                "\n  → fix the audit= sink (its .record raised)",
            )
        tally.wrote = True
    result = env.registry.call(name, arguments, context=env.context)
    tally.executed.append(name)
    return result


def _decide(gate: ApprovalHandler, request: ApprovalRequest) -> tuple[bool, str | None]:
    """Ask the gate; return ``(approved, failure)``. A handler that raises must
    never approve (fail closed) — the failure text keeps the denial honest."""
    try:
        return bool(gate.approve(request)), None
    except Exception as exc:  # any handler failure is a deny, not a crash mid-run
        return False, f"{type(exc).__name__}: {exc}"


def _audit_action(
    env: _RunEnv,
    tally: _Tally,
    action: ProposedAction,
    decision: str,
    reason: str | None = None,
) -> bool:
    """Emit one audit record; ``False`` means the sink failed (callers fail closed
    for writes about to run — a blocked action's lost record is not a safety hole)."""
    record = AuditRecord(
        timestamp=now(),
        principal=env.context.principal,
        capability=action.capability.name,
        arguments=redact_arguments(action.call.arguments),
        access=action.capability.access,
        decision=decision,
        reason=reason,
        trace_id=env.context.trace_id,
    )
    try:
        env.audit.record(record)
    except Exception:
        return False
    tally.audit.append(record)
    return True


def _budget_stop(budget: Budget, usage: Usage, elapsed: float) -> str | None:
    """The user-facing message if a hard budget is exhausted, else ``None``."""
    if budget.over_tokens(usage):
        return "Stopped: token budget exhausted.\n  → raise max_tokens or simplify the goal."
    if budget.over_cost(usage):
        return "Stopped: cost budget exhausted.\n  → raise max_cost or simplify the goal."
    if budget.over_time(elapsed):
        return "Stopped: time budget exhausted.\n  → raise max_seconds or simplify the goal."
    return None


def _report(
    outcome: str,
    *,
    output: Message,
    finish: FinishReason,
    usage: Usage,
    turn: int,
    tally: _Tally,
    trace_id: str,
    reason: str | None = None,
) -> RunReport:
    return RunReport(
        outcome=outcome,
        output=output,
        finish=finish,
        usage=usage,
        turn=turn,
        executed=tuple(tally.executed),
        reason=reason,
        audited=bool(tally.audit),
        audit=tuple(tally.audit),
        compactions=tally.compactions,
        trace_id=trace_id,
    )


def _tool_message(call_id: str, result: CapabilityResult) -> Message:
    text = str(result.value) if result.ok else (result.error or "capability error")
    return Message(role=Role.TOOL, tool_call_id=call_id, text=text)


def _synthetic(text: str) -> Message:
    return Message(role=Role.ASSISTANT, text=text)


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cost=a.cost + b.cost,
    )


class Agent:
    """The user-facing harness: wrap your capabilities, hand it a goal.

    ``ask()`` is read-only and never prompts; ``run()`` may write — writes travel
    policy → approval → audit → execution. ``approve=`` plugs in the human gate (a
    handler object or a plain ``request -> bool`` callable); the default is a
    terminal preview + ``[y]es / [n]o / [s]how``. ``confirm_bulk`` (default on)
    makes any write touching more than one row pause for explicit confirm at every
    autonomy level. ``principal=`` names who the agent acts for — it reaches every
    capability's ``context``/``ctx`` parameter and scopes ``scope``-annotated
    capabilities (RLS); ``ask()``/``run()`` accept a per-call override. ``audit=``
    plugs in a sink; the default keeps records in memory on ``agent.audit``.

    ``model`` may be a :class:`Model` instance or a string — ``"openai:gpt-4o"``,
    ``"anthropic:claude-opus-4-8"``, ``"groq:llama-3.3-70b-versatile"``,
    ``"ollama:llama3.1"``, or a bare name like ``"gpt-4o"`` (see
    ``reins.model.PROVIDERS``). With no ``model`` at all, the provider is
    auto-detected from the environment on first use.

    Context stays tiny at any scale (§2.12): above a small threshold the model
    sees one ``search_capabilities`` tool instead of every schema and discovers
    what it needs (``defer=`` forces this on/off); attach a ``schema=``
    :class:`SchemaIndex` and the data model is likewise searched, never dumped.

    Code-mode is the default action interface (§2.11): the model may write a
    short sandboxed program that calls your capabilities as plain functions —
    bulk data stays in the sandbox, only the printed result returns, and every
    call the program makes is gated exactly like a direct one. The default
    sandbox is a whitelisted-import subprocess (a development tier, honestly
    not a hard boundary); pass ``sandbox=DockerSandbox()`` in production, or
    ``mode="simple"`` to disable programs entirely.

    A ``budget`` bounds the run (turns / tokens / cost / time; it also compacts
    history once the conversation gets large, always keeping the goal); pass a
    ``pricing`` table to meter USD cost from token usage so ``budget.max_cost``
    can stop before a run gets expensive.
    """

    def __init__(
        self,
        model: Model | str | None = None,
        capabilities: Sequence[BoundCapability] = (),
        budget: Budget | None = None,
        autonomy: AutonomyLevel = AutonomyLevel.APPROVED_WRITES,
        policy: Policy | None = None,
        approve: ApprovalLike | None = None,
        confirm_bulk: bool = True,
        principal: str | None = None,
        audit: AuditSink | None = None,
        schema: SchemaIndex | None = None,
        defer: bool | None = None,
        mode: str = "code",
        sandbox: Sandbox | None = None,
        pricing: Pricing | None = None,
    ) -> None:
        # Resolve strings eagerly so a typo'd provider fails at construction with
        # a hinted error, not mid-run.
        self._model = resolve_model(model) if isinstance(model, str) else model
        self._pricing = pricing
        self._budget = budget or Budget()
        self._autonomy = autonomy
        self._policy = policy or DefaultPolicy()
        # Coerce eagerly so a misconfigured approve= fails at construction with a
        # hinted error, not at the first gated write as a silent deny.
        self._approve = None if approve is None else as_approval_handler(approve)
        self._confirm_bulk = confirm_bulk
        self._principal = principal
        self._audit = audit if audit is not None else MemoryAuditSink()
        self._schema = schema
        self._defer = defer
        if mode not in ("code", "simple"):
            raise SandboxError(
                f"unknown mode {mode!r}",
                hint="use mode='code' (default) or mode='simple'",
            )
        self._mode = mode
        self._sandbox = sandbox
        self._registry = CapabilityRegistry()
        for cap in capabilities:
            self._registry.register(cap)

    @classmethod
    def from_orm(
        cls,
        db: Any = None,
        *,
        models: Any | None = None,
        can_write: bool = False,
        read_limit: int = 50,
        **kwargs: Any,
    ) -> Agent:
        """Build an agent straight from your ORM models — zero hand-written capabilities.

        Introspects ``models`` and exposes intent-level, linted capabilities plus a
        schema index for progressive disclosure. SQLAlchemy models (or a
        declarative base) are reached through ``db`` (a ``Session``,
        ``sessionmaker``, session factory, or ``Engine``); Django models, an app
        label, or an ``AppConfig`` need no ``db`` (the connection is global), so
        call ``Agent.from_orm(models=[...])``. Reads are always available; write
        capabilities appear only with ``can_write=True`` and are gated by the
        autonomy ladder exactly like any other write. Extra keyword arguments
        (``model``, ``autonomy``, ``approve``, ``principal``, ``audit`` …) are
        forwarded to the constructor.
        """
        from reins.orm import build_orm

        capabilities, schema = build_orm(
            db, models=models, can_write=can_write, read_limit=read_limit
        )
        return cls(capabilities=capabilities, schema=schema, **kwargs)

    @property
    def audit(self) -> AuditSink:
        """This agent's audit sink (default: in-memory, see ``.records``)."""
        return self._audit

    def ask(self, goal: str, *, principal: str | None = None) -> RunResult:
        """Read-only: accomplish ``goal`` using only read capabilities."""
        return _to_run_result(
            self._loop(goal, can_write=False, autonomy=AutonomyLevel.READ_ONLY, principal=principal)
        )

    def run(self, goal: str, *, principal: str | None = None) -> RunResult:
        """Read-write: ``goal`` may write; writes are gated by the autonomy ladder."""
        return _to_run_result(
            self._loop(goal, can_write=True, autonomy=self._autonomy, principal=principal)
        )

    def _loop(
        self,
        goal: str,
        *,
        can_write: bool,
        autonomy: AutonomyLevel,
        principal: str | None = None,
    ) -> RunReport:
        model = self._model if self._model is not None else default_model()
        return run_loop(
            model,
            self._registry,
            goal,
            can_write=can_write,
            autonomy=autonomy,
            policy=self._policy,
            approve=self._approve,
            confirm_bulk=self._confirm_bulk,
            principal=principal if principal is not None else self._principal,
            audit=self._audit,
            schema=self._schema,
            defer=self._defer,
            mode=self._mode,
            sandbox=self._sandbox,
            pricing=self._pricing,
            budget=self._budget,
        )


def _to_run_result(report: RunReport) -> RunResult:
    return RunResult(
        output=report.output, reason=report.finish, usage=report.usage, trace_id=report.trace_id
    )
