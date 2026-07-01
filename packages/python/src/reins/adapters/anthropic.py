"""Anthropic model adapter (M1.2).

Wraps the official ``anthropic`` SDK behind the :class:`reins.model.Model` protocol.
Optional dependency — install with ``pip install 'reins[anthropic]'``; the import is
lazy so the core never requires it. Defaults to ``claude-opus-4-8``.

This adapter is the untrusted boundary to a third party, so it is verified two ways:
the request/response mapping is unit-tested with an injected fake client (no network),
and a live call is behind a skip guard. It sends the model only each capability's
intent surface (name / description / input schema) — never policy metadata (§2.5).
"""

from __future__ import annotations

from typing import Any

from reins.errors import ModelError
from reins.model import ModelParams
from reins.types import (
    Capability,
    FinishReason,
    Message,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)

DEFAULT_MODEL = "claude-opus-4-8"

# Anthropic stop_reason -> our FinishReason.
_FINISH = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "tool_use": FinishReason.TOOL_CALLS,
    "pause_turn": FinishReason.TOOL_CALLS,
    "max_tokens": FinishReason.LENGTH,
    "refusal": FinishReason.ERROR,
}


class AnthropicModel:
    """A :class:`Model` backed by Anthropic's Messages API."""

    def __init__(self, model: str = DEFAULT_MODEL, client: Any | None = None) -> None:
        self._model = model
        if client is None:
            try:
                import anthropic
            except ModuleNotFoundError as exc:
                raise ModelError(
                    "the 'anthropic' package is required for AnthropicModel",
                    hint="install it with: pip install 'reins[anthropic]'",
                ) from exc
            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self._client = client

    def complete(
        self,
        messages: list[Message],
        capabilities: list[Capability],
        params: ModelParams,
    ) -> ModelResponse:
        system, turns = _to_anthropic_messages(messages)
        tools = [_to_anthropic_tool(c) for c in capabilities]
        kwargs: dict[str, Any] = {
            "model": params.model or self._model,
            "max_tokens": params.max_tokens,
            "messages": turns,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        return _from_anthropic_response(self._client.messages.create(**kwargs))


def _to_anthropic_tool(cap: Capability) -> dict[str, Any]:
    # Only the intent surface crosses to the model — never access/confirm/scope (§2.5).
    return {
        "name": cap.name,
        "description": cap.description,
        "input_schema": cap.input_schema or {"type": "object", "properties": {}},
    }


def _to_anthropic_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []
    for m in messages:
        if m.role == Role.SYSTEM:
            if m.text:
                system_parts.append(m.text)
        elif m.role == Role.USER:
            turns.append({"role": "user", "content": m.text or ""})
        elif m.role == Role.ASSISTANT:
            content: list[dict[str, Any]] = []
            if m.text:
                content.append({"type": "text", "text": m.text})
            for tc in m.tool_calls:
                content.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                )
            turns.append({"role": "assistant", "content": content or (m.text or "")})
        elif m.role == Role.TOOL:
            turns.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id or "",
                            "content": m.text or "",
                        }
                    ],
                }
            )
    return "\n\n".join(system_parts), turns


def _from_anthropic_response(resp: Any) -> ModelResponse:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
            )
    message = Message(
        role=Role.ASSISTANT,
        text="".join(text_parts) or None,
        tool_calls=tool_calls,
    )
    finish = _FINISH.get(getattr(resp, "stop_reason", None) or "", FinishReason.STOP)
    usage = Usage(
        input_tokens=getattr(resp.usage, "input_tokens", 0),
        output_tokens=getattr(resp.usage, "output_tokens", 0),
    )
    return ModelResponse(message=message, finish_reason=finish, usage=usage)
