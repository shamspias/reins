"""Provider resolution tests (M2.5): `provider:model` strings, bare-name prefix
sniffing, env auto-detect order, and hinted failures.

The SDKs are stubbed into sys.modules so no provider package (or network) is
needed — resolution logic is what's under test, not the SDKs.
"""

import sys
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from reins.errors import ModelError
from reins.model import PROVIDERS, default_model, resolve_model

_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "ZAI_API_KEY",
    "OLLAMA_BASE_URL",
    "VLLM_API_KEY",
    "VLLM_BASE_URL",
)


class _StubOpenAI:
    """Captures the constructor kwargs the resolver passes to the SDK."""

    last_kwargs: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        _StubOpenAI.last_kwargs = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: None))


class _StubAnthropic:
    def __init__(self, **kwargs: Any) -> None:
        self.messages = SimpleNamespace(create=lambda **k: None)


@pytest.fixture(autouse=True)
def _clean_env_and_stub_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_StubOpenAI))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=_StubAnthropic))
    _StubOpenAI.last_kwargs = {}


def test_provider_colon_model_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ok")
    model = resolve_model("openai:gpt-4o")
    assert type(model).__name__ == "OpenAICompatModel"
    assert model._model == "gpt-4o"  # type: ignore[attr-defined]


def test_bare_provider_uses_its_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    model = resolve_model("groq")
    assert model._model == PROVIDERS["groq"].default_model  # type: ignore[attr-defined]
    assert _StubOpenAI.last_kwargs["base_url"] == "https://api.groq.com/openai/v1"


def test_bare_model_name_sniffs_the_family(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ZAI_API_KEY"):
        monkeypatch.setenv(var, "k")
    assert type(resolve_model("gpt-4o")).__name__ == "OpenAICompatModel"
    assert type(resolve_model("claude-opus-4-8")).__name__ == "AnthropicModel"
    assert type(resolve_model("gemini-2.5-pro")).__name__ == "OpenAICompatModel"
    assert type(resolve_model("glm-4.6")).__name__ == "OpenAICompatModel"


def test_glm_and_zai_aliases_resolve_to_zai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "zk")
    resolve_model("glm:glm-4.6")
    assert _StubOpenAI.last_kwargs["base_url"] == "https://api.z.ai/api/paas/v4"
    assert _StubOpenAI.last_kwargs["api_key"] == "zk"


def test_ollama_needs_an_explicit_model_name() -> None:
    with pytest.raises(ModelError, match="→"):
        resolve_model("ollama")


def test_ollama_with_model_uses_local_base_and_placeholder_key() -> None:
    resolve_model("ollama:llama3.1")
    assert _StubOpenAI.last_kwargs["base_url"] == "http://localhost:11434/v1"
    assert _StubOpenAI.last_kwargs["api_key"] == "ollama"


def test_base_url_env_overrides_the_local_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/v1")
    resolve_model("ollama:llama3.1")
    assert _StubOpenAI.last_kwargs["base_url"] == "http://gpu-box:11434/v1"


def test_unknown_spec_is_a_hinted_error() -> None:
    with pytest.raises(ModelError, match="→") as exc:
        resolve_model("banana:split")
    assert "provider" in str(exc.value)


def test_missing_api_key_is_our_hinted_error_not_the_sdks() -> None:
    with pytest.raises(ModelError, match="→") as exc:
        resolve_model("openai:gpt-4o")  # no OPENAI_API_KEY in the cleaned env
    assert "OPENAI_API_KEY" in str(exc.value)


def test_detect_prefers_anthropic_then_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    monkeypatch.setenv("OPENAI_API_KEY", "ok")
    assert type(default_model()).__name__ == "AnthropicModel"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert type(default_model()).__name__ == "OpenAICompatModel"


def test_detect_walks_the_documented_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    monkeypatch.setenv("ZAI_API_KEY", "zk")
    model = default_model()  # groq comes before zai in the chain
    assert model._model == PROVIDERS["groq"].default_model  # type: ignore[attr-defined]


def test_detect_ollama_by_explicit_base_url_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    # Auto-detected local servers still need a model name — hinted, not silent.
    with pytest.raises(ModelError, match="→"):
        default_model()


def test_no_provider_is_a_hinted_error_listing_options() -> None:
    with pytest.raises(ModelError, match="→") as exc:
        default_model()
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OLLAMA_BASE_URL"):
        assert var in str(exc.value)


def test_agent_accepts_a_model_string(monkeypatch: pytest.MonkeyPatch) -> None:
    from reins import Agent

    monkeypatch.setenv("OPENAI_API_KEY", "ok")
    agent = Agent(model="openai:gpt-4o")
    assert type(agent._model).__name__ == "OpenAICompatModel"  # type: ignore[attr-defined]


def test_agent_rejects_a_typoed_model_string_at_construction() -> None:
    from reins import Agent

    with pytest.raises(ModelError, match="→"):
        Agent(model="opnai:gpt-4o")
