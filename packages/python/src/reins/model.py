"""The model layer (M1.2): the Model protocol, ModelParams, a scriptable FakeModel,
and provider auto-detection.

The Model is the **only** stochastic component in the harness (the determinism
boundary, ENGINEERING.md §2). Everything else — the loop, classification, policy,
linter — is deterministic and tested against :class:`FakeModel`, never a live LLM.

v1 is synchronous (CLAUDE.md §9: a sync ``run()``/``ask()`` must exist). The OAH spec
(``docs/open-agent-harness-spec.md`` §7.2) shows an async ``complete``; an async
variant can be added later without changing this contract's shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from reins.errors import ModelError
from reins.types import (
    Capability,
    FinishReason,
    Message,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)


@dataclass(frozen=True, slots=True)
class ModelParams:
    """Knobs for one model call. ``model=None`` lets the adapter pick its default.

    Sampling knobs (temperature/top_p) are intentionally omitted — the default model
    (claude-opus-4-8) rejects them — and can be added later with model-aware handling.
    """

    model: str | None = None
    max_tokens: int = 1024


@runtime_checkable
class Model(Protocol):
    """The single stochastic call.

    Implementations MUST be stateless with respect to inputs — the harness owns the
    conversation history (OAH §5.1). The adapter is shown the available capabilities
    but only ever sends the model their intent-level surface (name / description /
    input schema), never the policy metadata (invariant §2.5).
    """

    def complete(
        self,
        messages: list[Message],
        capabilities: list[Capability],
        params: ModelParams,
    ) -> ModelResponse: ...


@dataclass(slots=True)
class FakeModel:
    """A scriptable, deterministic :class:`Model` for tests (CLAUDE.md §3).

    It replays a fixed list of :class:`ModelResponse` in order. Build them directly,
    or use :meth:`from_script` for the compact ``{"call": ..., "args": ...}`` /
    ``{"final": ...}`` form the conformance cases use. Every call is recorded in
    ``calls`` so a test can assert exactly what the model was shown.
    """

    responses: list[ModelResponse]
    _index: int = 0
    calls: list[tuple[list[Message], list[Capability]]] = field(default_factory=list)

    def complete(
        self,
        messages: list[Message],
        capabilities: list[Capability],
        params: ModelParams,
    ) -> ModelResponse:
        self.calls.append((list(messages), list(capabilities)))
        if self._index >= len(self.responses):
            raise ModelError(
                f"FakeModel script exhausted after {len(self.responses)} response(s)",
                hint="add more responses to the FakeModel, or check the loop is terminating",
            )
        response = self.responses[self._index]
        self._index += 1
        return response

    @classmethod
    def from_script(cls, steps: list[dict[str, Any]]) -> FakeModel:
        """Build a FakeModel from compact steps: each is a capability call
        (``{"call": name, "args": {...}}``) or a final answer (``{"final": text}``)."""
        responses: list[ModelResponse] = []
        for i, step in enumerate(steps):
            if "final" in step:
                message = Message(role=Role.ASSISTANT, text=str(step["final"]))
                reason = FinishReason.STOP
            elif "call" in step:
                call = ToolCall(
                    id=f"call_{i}",
                    name=str(step["call"]),
                    arguments=dict(step.get("args") or {}),
                )
                message = Message(role=Role.ASSISTANT, tool_calls=[call])
                reason = FinishReason.TOOL_CALLS
            else:
                raise ModelError(
                    f"FakeModel step {i} has neither 'call' nor 'final'",
                    hint="use {'call': name, 'args': {...}} or {'final': text}",
                )
            responses.append(ModelResponse(message=message, finish_reason=reason, usage=Usage()))
        return cls(responses=responses)


def default_model() -> Model:
    """Resolve a Model from the environment (CLAUDE.md §0: Anthropic via
    ``ANTHROPIC_API_KEY``). Raises :class:`ModelError` with a ``→`` hint if no
    provider is configured."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        from reins.adapters.anthropic import AnthropicModel  # lazy: optional dependency

        return AnthropicModel()
    raise ModelError(
        "no model provider configured",
        hint="set ANTHROPIC_API_KEY, or pass an explicit Model to the agent",
    )
