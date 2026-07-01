"""Anthropic adapter tests (M1.2).

The request/response mapping is exercised with an injected fake client — no network
and no `anthropic` package needed, so this runs in the gate. The real API call is a
separate test guarded by ANTHROPIC_API_KEY + the anthropic package being installed.
"""

import importlib.util
import os

import pytest

from reins.adapters.anthropic import DEFAULT_MODEL, AnthropicModel
from reins.model import ModelParams
from reins.types import Access, Capability, FinishReason, Message, Role


class _Block:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 11
    output_tokens = 7


class _Resp:
    def __init__(self, content: list[object], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Messages:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.captured: dict[str, object] = {}

    def create(self, **kwargs: object) -> _Resp:
        self.captured = kwargs
        return self._resp


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self.messages = _Messages(resp)


def test_maps_text_response() -> None:
    client = _Client(_Resp([_Block(type="text", text="hello")], "end_turn"))
    model = AnthropicModel(client=client)
    out = model.complete([Message(role=Role.USER, text="hi")], [], ModelParams())
    assert out.message.text == "hello"
    assert out.finish_reason == FinishReason.STOP
    assert out.usage.input_tokens == 11


def test_maps_tool_use_response() -> None:
    block = _Block(type="tool_use", id="t1", name="refund", input={"order_id": 8842})
    client = _Client(_Resp([block], "tool_use"))
    model = AnthropicModel(client=client)
    out = model.complete([], [], ModelParams())
    assert out.finish_reason == FinishReason.TOOL_CALLS
    assert out.message.tool_calls[0].name == "refund"
    assert out.message.tool_calls[0].arguments == {"order_id": 8842}


def test_sends_only_safe_capability_fields() -> None:
    client = _Client(_Resp([_Block(type="text", text="ok")], "end_turn"))
    model = AnthropicModel(client=client)
    cap = Capability(
        name="refund_order",
        description="Refund an order.",
        input_schema={"type": "object", "properties": {}},
        access=Access.DESTRUCTIVE,
        confirm=True,
        scope="orders",
    )
    model.complete([], [cap], ModelParams())
    sent_tool = client.messages.captured["tools"][0]  # type: ignore[index]
    # invariant §2.5: policy metadata is never exposed to the model
    assert set(sent_tool) == {"name", "description", "input_schema"}


def test_default_model_constant() -> None:
    assert DEFAULT_MODEL == "claude-opus-4-8"


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") and importlib.util.find_spec("anthropic")),
    reason="live test: needs ANTHROPIC_API_KEY and the anthropic package",
)
def test_live_anthropic_call() -> None:
    model = AnthropicModel()
    out = model.complete(
        [Message(role=Role.USER, text="Reply with the single word: pong")],
        [],
        ModelParams(max_tokens=16),
    )
    assert out.message.text
    assert out.usage.output_tokens > 0
