"""Reins core value types (M1.1).

The data the harness passes around: messages, tool calls, model responses, usage,
the capability *descriptor*, the errors-as-data ``CapabilityResult``, and the run
state/result. These are the language-neutral shapes from ``spec/contract.md`` and
the Open Agent Harness spec (``docs/open-agent-harness-spec.md`` §5); the same
names exist in the Go and TS ports (invariant §2.21).

All types are immutable (``frozen=True``, ``slots=True``) and JSON round-trip via
``reins.codec`` (``from_dict(cls, to_dict(x)) == x``). Behaviour — the loop,
read/write classification, the linter — lives in later milestones; this module is
*data only*. Fields typed ``dict[str, Any]`` / ``Any`` hold arbitrary, but strictly
JSON-serializable, values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    """Who produced a :class:`Message`."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    """Why a model turn (or a whole run) ended."""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    INTERRUPTED = "interrupted"
    ERROR = "error"


class Access(StrEnum):
    """A capability's danger classification (``spec/contract.md`` §2).

    Mutually exclusive and ordered by danger. The harness defaults to ``WRITE``
    when classification is ambiguous (invariant §2.4) — never ``READ``.
    """

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to call one capability, with its JSON arguments."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in the conversation (system / user / assistant / tool)."""

    role: Role
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Usage:
    """Token + cost accounting for a model call (or a cumulative total)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """One model turn: the assistant message, why it stopped, and its usage."""

    message: Message
    finish_reason: FinishReason
    usage: Usage


@dataclass(frozen=True, slots=True)
class Capability:
    """Descriptor for one operation the agent may perform — DATA only in M1.1.

    The ``@capability`` decorator, type-hint introspection, and the linter that
    *produce* and *check* these arrive in M1.3; here we only define the shape.
    ``input_schema`` is a JSON Schema object describing the arguments.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    access: Access = Access.WRITE
    confirm: bool = False
    idempotent: bool = False
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """Errors-as-data: the outcome of executing a capability (ENGINEERING.md §2).

    On success ``ok=True`` and ``value`` holds the (JSON) result. On failure
    ``ok=False`` and ``error`` explains why; ``retryable`` hints whether retrying
    could help. The model sees this and adapts — capability failures never raise
    across the loop boundary.
    """

    ok: bool
    value: Any = None
    error: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class RunResult:
    """The final result of a run: output message, why it ended, usage, trace id."""

    output: Message
    reason: FinishReason
    usage: Usage
    trace_id: str


@dataclass(frozen=True, slots=True)
class RunState:
    """The evolving state of a run.

    ``messages`` is the conversation history. In M1.4 a richer ``Conversation``
    type (append / compact / token_estimate) replaces this list; until then a
    plain ``list[Message]`` is the forward-compatible representation. ``started_at``
    and ``last_response`` are ``None`` until the run engages and the model first
    responds.
    """

    messages: list[Message] = field(default_factory=list)
    turn: int = 0
    cumulative_usage: Usage = field(default_factory=Usage)
    started_at: datetime | None = None
    last_response: ModelResponse | None = None
