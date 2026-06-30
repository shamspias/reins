"""Tests for the policy engine (M2.2): the autonomy ladder and write gating."""

from reins.policy import AutonomyLevel, Decision, DefaultPolicy, ProposedAction
from reins.types import Access, Capability, Message, Role, ToolCall


def _action(name: str, access: Access, *, confirm: bool = False) -> ProposedAction:
    spec = Capability(name=name, description=f"{name}.", access=access, confirm=confirm)
    return ProposedAction(call=ToolCall(id="c", name=name), capability=spec)


def _decide(action: ProposedAction, autonomy: AutonomyLevel) -> Decision:
    return DefaultPolicy().check_actions([action], autonomy).decision


READ = _action("find_x", Access.READ)
WRITE = _action("update_x", Access.WRITE)
DESTRUCTIVE = _action("delete_x", Access.DESTRUCTIVE)


def test_reads_allowed_at_every_level() -> None:
    for level in AutonomyLevel:
        assert _decide(READ, level) is Decision.ALLOW


def test_read_only_denies_writes() -> None:
    verdict = DefaultPolicy().check_actions([WRITE], AutonomyLevel.READ_ONLY)
    assert verdict.decision is Decision.DENY
    assert verdict.reason == "write_in_read_only"


def test_draft_writes_denies_as_draft() -> None:
    verdict = DefaultPolicy().check_actions([WRITE], AutonomyLevel.DRAFT_WRITES)
    assert verdict.decision is Decision.DENY
    assert verdict.reason == "draft_only"


def test_approved_writes_requires_approval() -> None:
    assert _decide(WRITE, AutonomyLevel.APPROVED_WRITES) is Decision.REQUIRE_APPROVAL
    assert _decide(DESTRUCTIVE, AutonomyLevel.APPROVED_WRITES) is Decision.REQUIRE_APPROVAL


def test_trusted_allows_writes_but_gates_destructive() -> None:
    assert _decide(WRITE, AutonomyLevel.TRUSTED) is Decision.ALLOW
    assert _decide(DESTRUCTIVE, AutonomyLevel.TRUSTED) is Decision.REQUIRE_APPROVAL


def test_trusted_gates_confirm_marked_capability() -> None:
    confirmed = _action("update_y", Access.WRITE, confirm=True)
    assert _decide(confirmed, AutonomyLevel.TRUSTED) is Decision.REQUIRE_APPROVAL


def test_batch_takes_the_most_restrictive_decision() -> None:
    verdict = DefaultPolicy().check_actions([READ, WRITE], AutonomyLevel.APPROVED_WRITES)
    assert verdict.decision is Decision.REQUIRE_APPROVAL


def test_confirm_is_ignored_for_reads() -> None:
    # Reads must never block (or ask() would prompt); confirm only gates writes.
    read_confirm = _action("find_z", Access.READ, confirm=True)
    for level in AutonomyLevel:
        assert _decide(read_confirm, level) is Decision.ALLOW


def test_check_output_allows_by_default() -> None:
    message = Message(role=Role.ASSISTANT, text="here is your answer")
    assert DefaultPolicy().check_output(message).decision is Decision.ALLOW
