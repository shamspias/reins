"""Tests for the model layer (M1.2): the Model protocol and the scriptable FakeModel."""

import pytest

from reins.errors import ModelError
from reins.model import FakeModel, Model, ModelParams, default_model
from reins.types import Capability, FinishReason, Message, ModelResponse, Role, Usage


def _final(text: str) -> ModelResponse:
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, text=text),
        finish_reason=FinishReason.STOP,
        usage=Usage(),
    )


def test_fakemodel_returns_responses_in_order() -> None:
    fake = FakeModel(responses=[_final("one"), _final("two")])
    p = ModelParams()
    assert fake.complete([], [], p).message.text == "one"
    assert fake.complete([], [], p).message.text == "two"


def test_fakemodel_records_what_it_was_shown() -> None:
    fake = FakeModel(responses=[_final("hi")])
    msgs = [Message(role=Role.USER, text="hello")]
    caps = [Capability(name="find_orders", description="Find orders.")]
    fake.complete(msgs, caps, ModelParams())
    assert len(fake.calls) == 1
    shown_msgs, shown_caps = fake.calls[0]
    assert shown_msgs[0].text == "hello"
    assert shown_caps[0].name == "find_orders"


def test_fakemodel_exhausted_raises_hinted_error() -> None:
    fake = FakeModel(responses=[_final("only")])
    fake.complete([], [], ModelParams())
    with pytest.raises(ModelError, match="→"):
        fake.complete([], [], ModelParams())


def test_fakemodel_from_script_builds_calls_and_final() -> None:
    fake = FakeModel.from_script(
        [{"call": "find_orders", "args": {"status": "processing"}}, {"final": "done"}]
    )
    first = fake.complete([], [], ModelParams())
    assert first.finish_reason == FinishReason.TOOL_CALLS
    assert first.message.tool_calls[0].name == "find_orders"
    assert first.message.tool_calls[0].arguments == {"status": "processing"}
    second = fake.complete([], [], ModelParams())
    assert second.finish_reason == FinishReason.STOP
    assert second.message.text == "done"


def test_fakemodel_from_script_rejects_bad_step() -> None:
    with pytest.raises(ModelError, match="→"):
        FakeModel.from_script([{"oops": "no call or final"}])


def test_fakemodel_is_a_model() -> None:
    assert isinstance(FakeModel(responses=[]), Model)


def test_default_model_without_key_raises_hinted_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ModelError, match="→"):
        default_model()
