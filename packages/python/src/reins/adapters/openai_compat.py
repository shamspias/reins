"""OpenAI-compatible model adapter (M2.5).

One adapter, many providers: the Chat Completions dialect is the de-facto wire
format of the model ecosystem, so this single class serves **OpenAI, Groq, vLLM,
Ollama, Z.ai (GLM), Gemini** (via its OpenAI-compat endpoint) and any other
compatible server — only the ``base_url`` and API key differ (see
``reins.model.PROVIDERS`` for the built-in table).

Wraps the official ``openai`` SDK behind the :class:`reins.model.Model` protocol.
Optional dependency — install with ``pip install 'reins[openai]'``; the import is
lazy so the core never requires it.

This adapter is the untrusted boundary to a third party, so it is verified two
ways: the request/response mapping is unit-tested with an injected fake client
(no network), and live calls sit behind skip guards. It sends the model only each
capability's intent surface (name / description / input schema) — never policy
metadata (§2.5).
"""

from __future__ import annotations

import json
from typing import Any

from reins.errors import ModelError
from reins.model import ModelParams
from reins.types import (
    FinishReason,
    Message,
    ModelResponse,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)

# Chat Completions finish_reason -> our FinishReason.
_FINISH = {
    "stop": FinishReason.STOP,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.ERROR,
}


class OpenAICompatModel:
    """A :class:`Model` backed by any OpenAI-compatible Chat Completions API.

    ``base_url=None`` targets api.openai.com; point it at Groq, a local Ollama
    (``http://localhost:11434/v1``), vLLM, Z.ai, Gemini's compat endpoint, or any
    other compatible server. ``api_key`` falls back to the SDK's environment
    handling; local servers accept any placeholder.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._model = model
        # api.openai.com rejects max_tokens for its newest models and wants
        # max_completion_tokens; most compat servers only know max_tokens.
        self._use_max_completion_tokens = base_url is None
        if client is None:
            try:
                import openai
            except ModuleNotFoundError as exc:
                raise ModelError(
                    "the 'openai' package is required for OpenAI-compatible providers",
                    hint="install it with: pip install 'reins[openai]'",
                ) from exc
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            if base_url is not None:
                kwargs["base_url"] = base_url
            client = openai.OpenAI(**kwargs)
        self._client = client

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        params: ModelParams,
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": params.model or self._model,
            "messages": _to_chat_messages(messages),
        }
        if self._use_max_completion_tokens:
            kwargs["max_completion_tokens"] = params.max_tokens
        else:
            kwargs["max_tokens"] = params.max_tokens
        wire_tools = [_to_chat_tool(t) for t in tools]
        if wire_tools:
            kwargs["tools"] = wire_tools
        return _from_chat_response(self._client.chat.completions.create(**kwargs))


def _to_chat_tool(spec: ToolSpec) -> dict[str, Any]:
    # ToolSpec IS the intent surface — §2.5 is enforced by the type at this boundary.
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema or {"type": "object", "properties": {}},
        },
    }


def _to_chat_messages(messages: list[Message]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for m in messages:
        if m.role == Role.SYSTEM:
            turns.append({"role": "system", "content": m.text or ""})
        elif m.role == Role.USER:
            turns.append({"role": "user", "content": m.text or ""})
        elif m.role == Role.ASSISTANT:
            turn: dict[str, Any] = {"role": "assistant", "content": m.text}
            if m.tool_calls:
                turn["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m.tool_calls
                ]
            turns.append(turn)
        elif m.role == Role.TOOL:
            turns.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.text or "",
                }
            )
    return turns


def _from_chat_response(resp: Any) -> ModelResponse:
    choice = resp.choices[0]
    msg = choice.message
    tool_calls = [_to_tool_call(tc) for tc in (getattr(msg, "tool_calls", None) or [])]
    message = Message(
        role=Role.ASSISTANT,
        text=getattr(msg, "content", None) or None,
        tool_calls=tool_calls,
    )
    finish = _FINISH.get(getattr(choice, "finish_reason", None) or "", FinishReason.STOP)
    if tool_calls:
        # Some compat servers report "stop" even when tool calls are present.
        finish = FinishReason.TOOL_CALLS
    usage = Usage(
        input_tokens=getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or 0,
        output_tokens=getattr(getattr(resp, "usage", None), "completion_tokens", 0) or 0,
    )
    return ModelResponse(message=message, finish_reason=finish, usage=usage)


def _to_tool_call(tc: Any) -> ToolCall:
    raw = getattr(tc.function, "arguments", None) or "{}"
    try:
        arguments = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ModelError(
            f"provider returned malformed tool arguments for {tc.function.name!r}: {raw[:80]!r}",
            hint="retry the run; if it persists, the provider/model does not emit valid JSON",
        ) from exc
    if not isinstance(arguments, dict):
        raise ModelError(
            f"provider returned non-object tool arguments for {tc.function.name!r}",
            hint="tool arguments must be a JSON object",
        )
    return ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
