"""OpenAI-compatible adapter tests (M2.5).

The request/response mapping is exercised with an injected fake client — no
network and no `openai` package needed, so this runs in the gate. A real API
call is a separate test guarded by OPENAI_API_KEY + the openai package.
"""

import importlib.util
import os
from types import SimpleNamespace
from typing import Any

import pytest

from reins.adapters.openai_compat import OpenAICompatModel
from reins.errors import ModelError
from reins.model import ModelParams
from reins.types import Access, Capability, FinishReason, Message, Role, ToolCall


def _response(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str = "stop",
) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )


def _tool_call(name: str, arguments: str, call_id: str = "call_1") -> Any:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class _Client:
    """A fake OpenAI client capturing the exact request payload."""

    def __init__(self, resp: Any) -> None:
        self.kwargs: dict[str, Any] = {}
        outer = self

        class _Completions:
            def create(self, **kwargs: Any) -> Any:
                outer.kwargs = kwargs
                return resp

        self.chat = SimpleNamespace(completions=_Completions())


def _model(resp: Any, **kw: Any) -> tuple[OpenAICompatModel, _Client]:
    client = _Client(resp)
    return OpenAICompatModel("test-model", client=client, **kw), client


def test_maps_text_response() -> None:
    model, _ = _model(_response(content="hello"))
    out = model.complete([Message(role=Role.USER, text="hi")], [], ModelParams())
    assert out.message.text == "hello"
    assert out.finish_reason is FinishReason.STOP
    assert out.usage.input_tokens == 11 and out.usage.output_tokens == 7


def test_maps_tool_call_response_with_parsed_json_args() -> None:
    resp = _response(
        tool_calls=[_tool_call("refund_order", '{"order_id": 8842}')],
        finish_reason="tool_calls",
    )
    model, _ = _model(resp)
    out = model.complete([Message(role=Role.USER, text="refund")], [], ModelParams())
    assert out.finish_reason is FinishReason.TOOL_CALLS
    assert out.message.tool_calls == [
        ToolCall(id="call_1", name="refund_order", arguments={"order_id": 8842})
    ]


def test_tool_calls_win_even_when_finish_reason_says_stop() -> None:
    # Some compat servers (local llamas especially) report "stop" alongside calls.
    resp = _response(tool_calls=[_tool_call("find_orders", "{}")], finish_reason="stop")
    model, _ = _model(resp)
    out = model.complete([Message(role=Role.USER, text="x")], [], ModelParams())
    assert out.finish_reason is FinishReason.TOOL_CALLS


def test_malformed_tool_arguments_raise_hinted_model_error() -> None:
    resp = _response(tool_calls=[_tool_call("f", "{not json")], finish_reason="tool_calls")
    model, _ = _model(resp)
    with pytest.raises(ModelError, match="→"):
        model.complete([Message(role=Role.USER, text="x")], [], ModelParams())


def test_non_object_tool_arguments_raise_hinted_model_error() -> None:
    resp = _response(tool_calls=[_tool_call("f", '["not", "an", "object"]')])
    model, _ = _model(resp)
    with pytest.raises(ModelError, match="→"):
        model.complete([Message(role=Role.USER, text="x")], [], ModelParams())


def test_sends_only_safe_capability_fields() -> None:
    # Invariant §2.5: policy metadata (access/confirm/scope) never crosses the wire.
    cap = Capability(
        name="delete_order",
        description="Delete an order.",
        input_schema={"type": "object", "properties": {"order_id": {"type": "integer"}}},
        access=Access.DESTRUCTIVE,
        confirm=True,
        scope="user",
    )
    model, client = _model(_response(content="ok"))
    model.complete([Message(role=Role.USER, text="x")], [cap], ModelParams())
    (tool,) = client.kwargs["tools"]
    assert tool == {
        "type": "function",
        "function": {
            "name": "delete_order",
            "description": "Delete an order.",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "integer"}}},
        },
    }


def test_maps_full_conversation_shape() -> None:
    messages = [
        Message(role=Role.SYSTEM, text="sys"),
        Message(role=Role.USER, text="goal"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="c1", name="find_orders", arguments={"status": "open"})],
        ),
        Message(role=Role.TOOL, tool_call_id="c1", text="['o1']"),
    ]
    model, client = _model(_response(content="done"))
    model.complete(messages, [], ModelParams())
    sent = client.kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "sys"}
    assert sent[1] == {"role": "user", "content": "goal"}
    assert sent[2]["role"] == "assistant"
    assert sent[2]["tool_calls"][0]["function"]["name"] == "find_orders"
    assert sent[3] == {"role": "tool", "tool_call_id": "c1", "content": "['o1']"}


def test_openai_dot_com_uses_max_completion_tokens() -> None:
    model, client = _model(_response(content="ok"))  # base_url=None -> api.openai.com
    model.complete([Message(role=Role.USER, text="x")], [], ModelParams(max_tokens=99))
    assert client.kwargs["max_completion_tokens"] == 99
    assert "max_tokens" not in client.kwargs


def test_compat_servers_use_max_tokens() -> None:
    model, client = _model(_response(content="ok"), base_url="http://localhost:11434/v1")
    model.complete([Message(role=Role.USER, text="x")], [], ModelParams(max_tokens=99))
    assert client.kwargs["max_tokens"] == 99
    assert "max_completion_tokens" not in client.kwargs


@pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and importlib.util.find_spec("openai")),
    reason="needs OPENAI_API_KEY and the openai package",
)
def test_live_openai_call() -> None:
    model = OpenAICompatModel("gpt-5.1")
    out = model.complete([Message(role=Role.USER, text="say ok")], [], ModelParams(max_tokens=16))
    assert out.message.text
