"""The policy engine (M2.2): deterministic, code-owned governance for writes.

Reads run freely; writes are gated by the autonomy ladder
(READ_ONLY → DRAFT_WRITES → APPROVED_WRITES → TRUSTED, invariant §2.10). The policy
decides ALLOW / DENY / REQUIRE_APPROVAL from each action's access class, the active
autonomy level, and the ``confirm`` annotation.

Its constraints are enforced here, in code — they are **never** described to the model
(invariant §2.5). The default never auto-executes a write, and destructive operations are
never auto-executed at any level.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from reins.types import Access, Capability, Message, ToolCall


class AutonomyLevel(StrEnum):
    """How much the agent may do on its own (invariant §2.10). Default APPROVED_WRITES."""

    READ_ONLY = "read_only"
    DRAFT_WRITES = "draft_writes"
    APPROVED_WRITES = "approved_writes"
    TRUSTED = "trusted"


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """One capability call the model wants to make, with its resolved descriptor."""

    call: ToolCall
    capability: Capability


@runtime_checkable
class Policy(Protocol):
    def check_actions(
        self, actions: list[ProposedAction], autonomy: AutonomyLevel
    ) -> PolicyDecision: ...

    def check_output(self, output: Message) -> PolicyDecision: ...


_ALLOW = PolicyDecision(Decision.ALLOW)
_RANK = {Decision.ALLOW: 0, Decision.REQUIRE_APPROVAL: 1, Decision.DENY: 2}


class DefaultPolicy:
    """Reins's default governance: reads free, writes gated by the autonomy ladder."""

    def check_actions(
        self, actions: list[ProposedAction], autonomy: AutonomyLevel
    ) -> PolicyDecision:
        verdict = _ALLOW
        for action in actions:  # the batch takes the most restrictive decision
            candidate = self._for(action.capability, autonomy)
            if _RANK[candidate.decision] > _RANK[verdict.decision]:
                verdict = candidate
        return verdict

    def check_output(self, output: Message) -> PolicyDecision:
        return _ALLOW  # post-output checks (e.g. PII redaction) arrive in a later milestone

    @staticmethod
    def _for(capability: Capability, autonomy: AutonomyLevel) -> PolicyDecision:
        if capability.access is Access.READ:
            return _ALLOW  # reads run freely (§2.2); confirm gates only writes/destructive
        if autonomy is AutonomyLevel.READ_ONLY:
            return PolicyDecision(Decision.DENY, "write_in_read_only")
        if autonomy is AutonomyLevel.DRAFT_WRITES:
            return PolicyDecision(Decision.DENY, "draft_only")
        if autonomy is AutonomyLevel.APPROVED_WRITES:
            return PolicyDecision(Decision.REQUIRE_APPROVAL, "approval_required")
        # TRUSTED: auto-allow plain writes, but destructive or confirm-marked stay gated.
        if capability.access is Access.DESTRUCTIVE or capability.confirm:
            return PolicyDecision(Decision.REQUIRE_APPROVAL, "approval_required")
        return _ALLOW
