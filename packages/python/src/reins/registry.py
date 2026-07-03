"""The CapabilityRegistry (M1.3): the only path by which the agent reaches a
capability.

It holds bound capabilities, validates arguments against the schema *before*
executing (model output is untrusted — the schema is the prepared statement,
invariant §2.6), then runs the function and wraps the outcome as errors-as-data
(:class:`CapabilityResult`, ENGINEERING.md §2). Only control-plane misuse (e.g.
duplicate registration) raises.

Identity (M2.4): ``call`` takes the run's :class:`Context` and injects it into a
capability's ``context`` / ``ctx`` parameter — the harness's context always wins
over anything model-supplied (§2.7) — and refuses a ``scope``-annotated
capability when no principal is set (§2.8, fail closed).
"""

from __future__ import annotations

import difflib
from typing import Any

from reins.capability import BoundCapability
from reins.errors import CapabilityError
from reins.rls import scope_violation
from reins.types import Capability, CapabilityResult, Context

_JSON_PYTYPES: dict[str, type | tuple[type, ...]] = {"string": str, "array": list, "object": dict}


class CapabilityRegistry:
    """Holds capabilities by name and dispatches calls through validation."""

    def __init__(self) -> None:
        self._caps: dict[str, BoundCapability] = {}

    def register(self, cap: BoundCapability) -> None:
        name = cap.spec.name
        if name in self._caps:
            raise CapabilityError(
                f"capability {name!r} is already registered",
                hint="give each capability a unique name",
            )
        self._caps[name] = cap

    def list(self) -> list[Capability]:
        return [c.spec for c in self._caps.values()]

    def get(self, name: str) -> BoundCapability | None:
        return self._caps.get(name)

    def validate(self, name: str, args: dict[str, Any]) -> None:
        """Validate ``args`` against the capability's schema; raise CapabilityError if invalid."""
        cap = self._caps.get(name)
        if cap is None:
            raise CapabilityError(
                f"no capability named {name!r}", hint=f"available: {sorted(self._caps)}"
            )
        _validate_against_schema(cap.spec.input_schema, args, capability=name)

    def call(
        self, name: str, args: dict[str, Any], context: Context | None = None
    ) -> CapabilityResult:
        """Validate then execute, returning the outcome as data (never raising over the loop)."""
        cap = self._caps.get(name)
        if cap is None:
            close = difflib.get_close_matches(name, list(self._caps), n=3)
            fix = f"closest matches: {close}" if close else "register it with @capability"
            return CapabilityResult(ok=False, error=f"no capability named {name!r}\n  → {fix}")
        violation = scope_violation(cap.spec, context)  # RLS before anything runs (§2.8)
        if violation is not None:
            return CapabilityResult(ok=False, error=violation)
        try:
            _validate_against_schema(cap.spec.input_schema, args, capability=name)
        except CapabilityError as exc:
            return CapabilityResult(ok=False, error=str(exc))
        kwargs = dict(args)
        if cap.context_param is not None:
            # Injected last: the harness's identity always beats a model-supplied
            # "context" argument that a lax schema let through (§2.7).
            kwargs[cap.context_param] = context if context is not None else Context()
        try:
            value = cap.func(**kwargs)
        except Exception as exc:  # a capability that raises is a tool failure, surfaced as data
            return CapabilityResult(ok=False, error=str(exc), retryable=False)
        return CapabilityResult(ok=True, value=value)


def _validate_against_schema(
    schema: dict[str, Any], args: dict[str, Any], *, capability: str
) -> None:
    if not isinstance(args, dict):
        raise CapabilityError(
            f"{capability}: arguments must be a mapping", hint="pass name -> value pairs"
        )
    # Only top-level keys and their JSON types are checked; nested object/array
    # shapes are not yet validated (richer schemas land in M4).
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])
    # Fail-closed: a schema that omits additionalProperties rejects extras (§2.6).
    allow_extra = schema.get("additionalProperties") is True
    for key in required:
        if key not in args:
            raise CapabilityError(
                f"{capability}: missing required argument {key!r}", hint=f"include {key!r}"
            )
    for key, value in args.items():
        if key not in properties:
            if not allow_extra:
                raise CapabilityError(
                    f"{capability}: unexpected argument {key!r}",
                    hint=f"allowed: {sorted(properties)}",
                )
            continue
        spec = properties[key]
        expected = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(expected, str) and not _type_matches(expected, value):
            raise CapabilityError(
                f"{capability}: argument {key!r} must be of type {expected}",
                hint=f"got {type(value).__name__}",
            )


def _type_matches(expected: str, value: Any) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    pytype = _JSON_PYTYPES.get(expected)
    return pytype is None or isinstance(value, pytype)
