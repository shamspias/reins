"""Guard the docs against API drift (M5.2).

QUICKSTART.md and EXTENDING.md promise specific public symbols and shapes. If a
rename or move breaks a documented import, this test fails — so the guides stay
runnable, not aspirational.
"""


def test_top_level_symbols_the_guides_use() -> None:
    import reins

    for name in (
        "Agent",
        "capability",
        "ask",
        "run",
        "cli",
        "endpoint",
        "chat_widget",
        "Context",
        "Budget",
        "DockerSandbox",
        "SubprocessSandbox",
        "SchemaIndex",
        "TableInfo",
        "ApprovalRequest",
        "AuditRecord",
        "TraceEvent",
    ):
        assert hasattr(reins, name), f"reins.{name} is documented but missing"


def test_agent_constructors_and_verbs_exist() -> None:
    from reins import Agent

    for name in ("from_orm", "from_module", "ask", "run", "explain", "audit", "last_report"):
        assert hasattr(Agent, name), f"Agent.{name} is documented but missing"


def test_extension_protocols_import() -> None:
    # Model
    # Approval / audit / sandbox / ORM
    from reins.approval import ApprovalHandler, ApprovalRequest  # noqa: F401
    from reins.audit import AuditRecord, AuditSink  # noqa: F401
    from reins.model import FakeModel, Model, ModelParams  # noqa: F401
    from reins.orm import build_orm  # noqa: F401

    # Policy
    from reins.policy import (  # noqa: F401
        AutonomyLevel,
        Decision,
        DefaultPolicy,
        Policy,
        PolicyDecision,
        ProposedAction,
    )
    from reins.sandbox import BridgeCall, Sandbox, SandboxOutcome  # noqa: F401
    from reins.types import Message, ModelResponse, ToolSpec  # noqa: F401


def test_documented_shapes_match() -> None:
    # ProposedAction.call.arguments / .capability.name — used in the Policy example.
    from reins.policy import Decision, PolicyDecision, ProposedAction
    from reins.types import Capability, ToolCall

    action = ProposedAction(
        call=ToolCall(id="c", name="refund_order", arguments={"amount": 50}),
        capability=Capability(name="refund_order", description="Refund."),
    )
    assert action.call.arguments["amount"] == 50
    assert action.capability.name == "refund_order"
    assert PolicyDecision(Decision.ALLOW).decision is Decision.ALLOW

    # ApprovalRequest fields used in the approval-gate example.
    from reins.approval import ApprovalRequest

    request = ApprovalRequest(actions=(action,), reason="approval_required", bulk_count=None)
    assert request.bulk_count is None
    assert "refund_order" in request.preview()
