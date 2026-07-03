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


@dataclass(frozen=True, slots=True)
class Provider:
    """One known model provider: how to reach it and how to authenticate.

    ``kind`` picks the adapter: ``"anthropic"`` (native SDK) or ``"openai"``
    (the OpenAI-compatible Chat Completions adapter — one dialect serves OpenAI,
    Groq, vLLM, Ollama, Z.ai/GLM, and Gemini's compat endpoint). ``base_url_env``
    lets an env var override ``base_url`` for self-hosted servers. A ``None``
    ``default_model`` means the model name must be given explicitly
    (``"ollama:llama3.1"``) — local servers have no universal default.
    """

    name: str
    kind: str
    env_key: str | None = None
    base_url: str | None = None
    base_url_env: str | None = None
    default_model: str | None = None
    placeholder_key: str | None = None  # local servers want any non-empty key


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(name="anthropic", kind="anthropic", env_key="ANTHROPIC_API_KEY"),
    "openai": Provider(
        name="openai",
        kind="openai",
        env_key="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",  # honored by the SDK too; listed for discoverability
        default_model="gpt-5.1",
    ),
    "gemini": Provider(
        name="gemini",
        kind="openai",
        env_key="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-2.5-flash",
    ),
    "groq": Provider(
        name="groq",
        kind="openai",
        env_key="GROQ_API_KEY",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
    ),
    "zai": Provider(
        name="zai",
        kind="openai",
        env_key="ZAI_API_KEY",
        base_url="https://api.z.ai/api/paas/v4",
        default_model="glm-4.6",
    ),
    "ollama": Provider(
        name="ollama",
        kind="openai",
        base_url="http://localhost:11434/v1",
        base_url_env="OLLAMA_BASE_URL",
        placeholder_key="ollama",
    ),
    "vllm": Provider(
        name="vllm",
        kind="openai",
        env_key="VLLM_API_KEY",
        base_url="http://localhost:8000/v1",
        base_url_env="VLLM_BASE_URL",
        placeholder_key="EMPTY",
    ),
}

_PROVIDER_ALIASES = {"glm": "zai", "z.ai": "zai", "claude": "anthropic", "google": "gemini"}

# Bare model names ("gpt-4o", "claude-...") resolve by prefix — dev-friendly
# sugar for the unambiguous families only.
_NAME_PREFIXES = {
    "claude": "anthropic",
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "gemini": "gemini",
    "glm": "zai",
}

# Env auto-detect order for default_model() — first configured provider wins.
_DETECT_ORDER = ("anthropic", "openai", "gemini", "groq", "zai", "ollama", "vllm")


def resolve_model(spec: str) -> Model:
    """Turn a model string into a :class:`Model` — the dev-friendly entry point.

    Accepts ``"provider:model"`` (``"openai:gpt-4o"``, ``"groq:llama-3.3-70b"``,
    ``"ollama:llama3.1"``), a bare provider name (``"openai"`` — uses its default
    model), or a bare model name with a recognizable prefix (``"claude-…"``,
    ``"gpt-…"``, ``"gemini-…"``, ``"glm-…"``). Raises :class:`ModelError` with a
    ``→`` hint for anything unrecognizable.
    """
    provider_part, _, model_part = spec.partition(":")
    key = _PROVIDER_ALIASES.get(provider_part.lower(), provider_part.lower())
    if key in PROVIDERS:
        return _build(PROVIDERS[key], model_part or None)
    if not model_part:  # maybe a bare model name like "gpt-4o" or "claude-…"
        family = spec.split("-", 1)[0].lower()
        prefix = _NAME_PREFIXES.get(family)
        if prefix is not None:
            return _build(PROVIDERS[prefix], spec)
    raise ModelError(
        f"unknown model spec {spec!r}",
        hint=f"use 'provider:model' with a provider from {sorted(PROVIDERS)} "
        "(e.g. 'openai:gpt-4o', 'ollama:llama3.1'), or pass a Model instance",
    )


def default_model() -> Model:
    """Resolve a Model from the environment (CLAUDE.md §0).

    Auto-detect order: ``ANTHROPIC_API_KEY`` → ``OPENAI_API_KEY`` →
    ``GEMINI_API_KEY`` → ``GROQ_API_KEY`` → ``ZAI_API_KEY`` →
    ``OLLAMA_BASE_URL`` → ``VLLM_BASE_URL``. Raises :class:`ModelError` with a
    ``→`` hint if nothing is configured.
    """
    for name in _DETECT_ORDER:
        provider = PROVIDERS[name]
        if provider.env_key and os.environ.get(provider.env_key):
            return _build(provider, None)
        if (
            (provider.env_key is None or name in ("ollama", "vllm"))
            and provider.base_url_env is not None
            and os.environ.get(provider.base_url_env)
        ):
            # Local servers opt in by setting their base-URL env var explicitly.
            return _build(provider, None)
    raise ModelError(
        "no model provider configured",
        hint="set one of ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / "
        "GROQ_API_KEY / ZAI_API_KEY (or OLLAMA_BASE_URL / VLLM_BASE_URL for local "
        "servers), pass model='provider:model', or pass a Model instance",
    )


def _build(provider: Provider, model: str | None) -> Model:
    """Instantiate the right adapter for ``provider``, lazily importing the SDK."""
    if provider.kind == "anthropic":
        from reins.adapters.anthropic import AnthropicModel  # lazy: optional dependency

        return AnthropicModel(**({"model": model} if model else {}))
    chosen = model or provider.default_model
    if chosen is None:
        raise ModelError(
            f"provider {provider.name!r} needs an explicit model name",
            hint=f"local servers have no universal default — use '{provider.name}:<model>' "
            f"(e.g. '{provider.name}:llama3.1')",
        )
    from reins.adapters.openai_compat import OpenAICompatModel  # lazy: optional dependency

    base_url = provider.base_url
    if provider.base_url_env and os.environ.get(provider.base_url_env):
        base_url = os.environ[provider.base_url_env]
    api_key: str | None = None
    if provider.env_key:
        api_key = os.environ.get(provider.env_key) or None
    if api_key is None and provider.placeholder_key is not None:
        api_key = provider.placeholder_key
    if api_key is None and provider.env_key is not None:
        # Fail here with our hint, not later with the SDK's raw constructor error.
        raise ModelError(
            f"{provider.env_key} is not set (needed for {provider.name!r})",
            hint=f"export {provider.env_key}=..., or pick another provider/model",
        )
    return OpenAICompatModel(chosen, api_key=api_key, base_url=base_url)
