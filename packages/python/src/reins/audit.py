"""The audit log (M2.4): a structured record of every write attempt.

Whenever the harness blocks, gates, or executes a write-classed capability it
emits an :class:`AuditRecord` — principal, capability, arguments (PII-redacted),
decision, timestamp, trace id — to an injected :class:`AuditSink`. The default
sink is in-memory (``agent.audit.records``); production apps plug in their own
(``Agent(audit=...)``). Auditing is part of the write gate chain (policy →
approval → audit, invariant §2.2): if a write attempt cannot be recorded, the
write does not run (no audit, no action — fail closed).

Redaction is key-based and recursive: argument keys that look like credentials
or PII are masked in the audit copy only — the capability still receives the
real values. Never log secrets (ENGINEERING.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from reins.types import Access

REDACTED = "[redacted]"

# Substring match, case-insensitive, applied to every mapping key at any depth.
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "ssn",
    "card_number",
    "cvv",
)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One write attempt: who, what, with which (redacted) args, and the decision.

    ``decision`` uses the run vocabulary: ``refused`` (blocked by the ask()
    boundary), ``denied`` (blocked by policy), ``approved`` / ``approval_denied``
    (the human gate), ``executed`` (the capability was invoked).
    """

    timestamp: datetime
    principal: str | None
    capability: str
    arguments: dict[str, Any]
    access: Access
    decision: str
    reason: str | None = None
    trace_id: str = ""


@runtime_checkable
class AuditSink(Protocol):
    """Where audit records go. ``record`` must persist (or forward) one record."""

    def record(self, record: AuditRecord) -> None: ...


@dataclass(slots=True)
class MemoryAuditSink:
    """The default sink: an in-process, in-order list (``agent.audit.records``)."""

    records: list[AuditRecord] = field(default_factory=list)

    def record(self, record: AuditRecord) -> None:
        self.records.append(record)


def redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``arguments`` with sensitive-looking keys masked.

    The original mapping is never mutated — the capability must still receive
    the real values; only the audit copy is masked.
    """
    return {key: _redact_value(key, value) for key, value in arguments.items()}


def now() -> datetime:
    """The audit clock (UTC). One place to swap for an injected clock later."""
    return datetime.now(UTC)


def _redact_value(key: str, value: Any) -> Any:
    if _is_sensitive(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, v) for v in value]
    return value


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)
