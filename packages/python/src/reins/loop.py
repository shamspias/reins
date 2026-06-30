"""The harness loop (M1.4, simple mode) and the Agent.

Engage → (Think → Call → Sense → Loop?) → Verify — the ETCSLV lifecycle. The model is
the only stochastic step; the gate, dispatch, and stop logic are deterministic and
owned here (invariant §2.14). This is *simple mode*: one model turn requests capability
calls directly. Code-mode (the default action interface) arrives in M3.2.

Safety boundary: ``ask()`` is structurally read-only — if the model requests a
write/destructive capability the run is refused before anything executes (§2.1).
``run()`` may write, but write gating (policy → approval → audit) is M2.x; until then a
write under ``run()`` is not executed (fail-closed). Policy is enforced in this code,
never described to the model (§2.5).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from reins.budget import Budget
from reins.capability import BoundCapability
from reins.model import Model, ModelParams, default_model
from reins.policy import AutonomyLevel, Decision, DefaultPolicy, Policy, ProposedAction
from reins.registry import CapabilityRegistry
from reins.stop import GoalReached, MaxTurns, StopCondition, first_stop
from reins.types import (
    Access,
    CapabilityResult,
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunResult,
    RunState,
    ToolCall,
    Usage,
)

SYSTEM_PROMPT = (
    "You operate an application through its registered capabilities. Use them to "
    "accomplish the user's goal, then reply with a final answer."
)


@dataclass(frozen=True, slots=True)
class RunReport:
    """The detailed result of one run: the run-level outcome plus telemetry.

    ``ask()`` / ``run()`` return the public :class:`RunResult`; this richer report is
    what the conformance suite asserts and (from M5.1) what ``explain()`` renders.
    ``outcome`` is one of refused / completed / executed / not_executed / stopped.
    """

    outcome: str
    output: Message
    finish: FinishReason
    usage: Usage
    turn: int
    executed: tuple[str, ...]
    reason: str | None = None
    audited: bool = False
    trace_id: str = ""


def run_loop(
    model: Model,
    registry: CapabilityRegistry,
    goal: str,
    *,
    can_write: bool,
    autonomy: AutonomyLevel = AutonomyLevel.APPROVED_WRITES,
    policy: Policy | None = None,
    budget: Budget | None = None,
    params: ModelParams | None = None,
) -> RunReport:
    """Drive the ETCSLV loop to a stop condition and return a :class:`RunReport`."""
    budget = budget or Budget()
    params = params or ModelParams()
    policy = policy or DefaultPolicy()
    trace_id = uuid.uuid4().hex
    messages: list[Message] = [
        Message(role=Role.SYSTEM, text=SYSTEM_PROMPT),
        Message(role=Role.USER, text=goal),
    ]
    stop_conditions: list[StopCondition] = [GoalReached(), MaxTurns(budget.max_turns)]
    executed: list[str] = []
    usage = Usage()
    last: ModelResponse | None = None
    turn = 0

    while True:
        if budget.over_tokens(usage):  # E/L: pre-think budget guard
            return _report(
                "stopped",
                output=_synthetic(
                    "Stopped: token budget exhausted.\n  → raise max_tokens or simplify the goal."
                ),
                finish=FinishReason.INTERRUPTED,
                usage=usage,
                turn=turn,
                executed=executed,
                trace_id=trace_id,
                reason="budget_exhausted",
            )

        resp = model.complete(messages, registry.list(), params)  # T: think
        usage = _add(usage, resp.usage)
        messages.append(resp.message)
        last = resp

        if resp.finish_reason == FinishReason.TOOL_CALLS and resp.message.tool_calls:  # C: call
            blocked = _act(
                resp.message.tool_calls,
                registry,
                can_write=can_write,
                autonomy=autonomy,
                policy=policy,
                executed=executed,
                messages=messages,
                usage=usage,
                turn=turn,
                trace_id=trace_id,
            )
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
                if policy.check_output(output).decision is Decision.DENY:
                    return _report(
                        "refused",
                        output=_synthetic("Output withheld by policy.\n  → adjust the goal."),
                        finish=FinishReason.STOP,
                        usage=usage,
                        turn=turn,
                        executed=executed,
                        trace_id=trace_id,
                        reason="output_denied",
                    )
                return _report(
                    "completed",
                    output=output,
                    finish=FinishReason.STOP,
                    usage=usage,
                    turn=turn,
                    executed=executed,
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
                executed=executed,
                trace_id=trace_id,
                reason="budget_exhausted",
            )


def _act(
    tool_calls: list[ToolCall],
    registry: CapabilityRegistry,
    *,
    can_write: bool,
    autonomy: AutonomyLevel,
    policy: Policy,
    executed: list[str],
    messages: list[Message],
    usage: Usage,
    turn: int,
    trace_id: str,
) -> RunReport | None:
    # Resolve every requested call once; the gate and the executor share this list so
    # they cannot diverge on what a name resolves to.
    resolved = [(call, registry.get(call.name)) for call in tool_calls]

    # ask(): structurally read-only — refuse the whole turn if any call writes, before
    # the policy is even consulted (invariant §2.1).
    if not can_write:
        for call, cap in resolved:
            if cap is not None and cap.spec.access is not Access.READ:
                return _report(
                    "refused",
                    output=_synthetic(
                        f"Refused: {call.name!r} would write, but this is a read-only ask()."
                        "\n  → use run(...) if writing is intended."
                    ),
                    finish=FinishReason.STOP,
                    usage=usage,
                    turn=turn,
                    executed=executed,
                    trace_id=trace_id,
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
            verdict = policy.check_actions(proposed, autonomy)
            if verdict.decision is Decision.DENY:
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
                    executed=executed,
                    trace_id=trace_id,
                    reason=verdict.reason,
                )
            if verdict.decision is Decision.REQUIRE_APPROVAL:
                return _report(
                    "not_executed",
                    output=_synthetic(
                        "Not executed: this write needs approval."
                        "\n  → approval handling arrives with the human gate."
                    ),
                    finish=FinishReason.STOP,
                    usage=usage,
                    turn=turn,
                    executed=executed,
                    trace_id=trace_id,
                    reason=verdict.reason or "approval_required",
                )

    for call, cap in resolved:
        if cap is None:
            result = CapabilityResult(ok=False, error=f"no capability named {call.name!r}")
            messages.append(_tool_message(call.id, result))
            continue
        result = registry.call(call.name, call.arguments)
        executed.append(call.name)
        messages.append(_tool_message(call.id, result))
    return None


def _report(
    outcome: str,
    *,
    output: Message,
    finish: FinishReason,
    usage: Usage,
    turn: int,
    executed: list[str],
    trace_id: str,
    reason: str | None = None,
) -> RunReport:
    return RunReport(
        outcome=outcome,
        output=output,
        finish=finish,
        usage=usage,
        turn=turn,
        executed=tuple(executed),
        reason=reason,
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

    ``ask()`` is read-only and never prompts; ``run()`` may write (gated from M2.x).
    With no ``model``, the provider is resolved from the environment on first use.
    """

    def __init__(
        self,
        model: Model | None = None,
        capabilities: Sequence[BoundCapability] = (),
        budget: Budget | None = None,
        autonomy: AutonomyLevel = AutonomyLevel.APPROVED_WRITES,
        policy: Policy | None = None,
    ) -> None:
        self._model = model
        self._budget = budget or Budget()
        self._autonomy = autonomy
        self._policy = policy or DefaultPolicy()
        self._registry = CapabilityRegistry()
        for cap in capabilities:
            self._registry.register(cap)

    def ask(self, goal: str) -> RunResult:
        """Read-only: accomplish ``goal`` using only read capabilities."""
        return _to_run_result(self._loop(goal, can_write=False, autonomy=AutonomyLevel.READ_ONLY))

    def run(self, goal: str) -> RunResult:
        """Read-write: ``goal`` may write; writes are gated by the autonomy ladder."""
        return _to_run_result(self._loop(goal, can_write=True, autonomy=self._autonomy))

    def _loop(self, goal: str, *, can_write: bool, autonomy: AutonomyLevel) -> RunReport:
        model = self._model if self._model is not None else default_model()
        return run_loop(
            model,
            self._registry,
            goal,
            can_write=can_write,
            autonomy=autonomy,
            policy=self._policy,
            budget=self._budget,
        )


def _to_run_result(report: RunReport) -> RunResult:
    return RunResult(
        output=report.output, reason=report.finish, usage=report.usage, trace_id=report.trace_id
    )
