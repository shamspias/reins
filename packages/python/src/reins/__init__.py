"""Reins — a lightweight agent harness you bolt onto an existing app.

Point it at functions you already have (or your ORM models), hand it a goal in
plain language, and it operates your app through your own code — safely and
cheaply. See ``CLAUDE.md`` for the build plan and the non-negotiable invariants.

This is the M0.1 scaffold: the package is intentionally empty of features. The
public surface (``Agent``, ``capability``, ``ask`` / ``run``, ...) is added from
M1.1 onward, one milestone per release.
"""

from reins.approval import ApprovalHandler, ApprovalRequest, TerminalApprovalHandler
from reins.audit import AuditRecord, AuditSink, MemoryAuditSink
from reins.capability import capability
from reins.disclosure import SchemaIndex, TableInfo
from reins.loop import Agent
from reins.policy import AutonomyLevel
from reins.types import Context, ToolSpec

__all__ = [
    "Agent",
    "ApprovalHandler",
    "ApprovalRequest",
    "AuditRecord",
    "AuditSink",
    "AutonomyLevel",
    "Context",
    "MemoryAuditSink",
    "SchemaIndex",
    "TableInfo",
    "TerminalApprovalHandler",
    "ToolSpec",
    "__version__",
    "capability",
]

__version__ = "0.0.0"
