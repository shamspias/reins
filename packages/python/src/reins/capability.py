"""Capabilities (M1.3): turn a typed function into an intent-named, schema-bearing
operation the agent may perform.

``@capability`` introspects the function's signature and docstring to build a
:class:`reins.types.Capability` descriptor — a JSON Schema from the type hints, a
description from the docstring, and a read / write / destructive classification from
the name's leading verb (ambiguous ⇒ write, invariant §2.4). Explicit keyword
overrides beat the heuristic. The decorated object stays callable and carries its
``.spec``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints, overload

from reins.errors import CapabilityError
from reins.types import Access, Capability

# Verb heuristic for read/write classification (easy-by-design spec).
READ_VERBS = frozenset(
    {
        "get",
        "list",
        "find",
        "search",
        "fetch",
        "count",
        "show",
        "read",
        "view",
        "lookup",
        "describe",
    }
)
WRITE_VERBS = frozenset(
    {
        "create",
        "add",
        "update",
        "set",
        "save",
        "send",
        "apply",
        "edit",
        "modify",
        "insert",
        "put",
        "post",
        "make",
        "assign",
        "schedule",
        "approve",
        "register",
    }
)
DESTRUCTIVE_VERBS = frozenset(
    {
        "delete",
        "remove",
        "cancel",
        "refund",
        "charge",
        "drop",
        "deactivate",
        "purge",
        "archive",
        "revoke",
        "reset",
        "wipe",
    }
)
_SKIP_PARAMS = frozenset({"self", "cls", "context", "ctx"})
_PY_TO_JSON = {str: "string", bool: "boolean", int: "integer", float: "number"}


@dataclass(frozen=True, slots=True)
class BoundCapability:
    """A :class:`Capability` descriptor bound to its executable function."""

    spec: Capability
    func: Callable[..., Any]

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


@overload
def capability(func: Callable[..., Any]) -> BoundCapability: ...


@overload
def capability(
    *,
    reads: bool = ...,
    destructive: bool = ...,
    confirm: bool = ...,
    idempotent: bool = ...,
    scope: str | None = ...,
    name: str | None = ...,
    description: str | None = ...,
) -> Callable[[Callable[..., Any]], BoundCapability]: ...


def capability(
    func: Callable[..., Any] | None = None,
    *,
    reads: bool = False,
    destructive: bool = False,
    confirm: bool = False,
    idempotent: bool = False,
    scope: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> BoundCapability | Callable[[Callable[..., Any]], BoundCapability]:
    """Decorate a function as a capability.

    Usable bare (``@capability``) or with explicit annotations that override the
    name-based classification:

    - ``reads=True`` → READ and ``destructive=True`` → DESTRUCTIVE are mutually
      exclusive overrides (setting both is an error). With neither, the access class
      is inferred from the leading verb, defaulting to write when ambiguous (§2.4).
    - ``confirm`` (require approval for writes/destructive — reads always run freely),
      ``idempotent`` (safe to retry), and ``scope`` (row-level-security tag) ride on the
      descriptor for the policy, approval, and RLS layers.
    - ``name`` / ``description`` override the function name and docstring.
    """

    def wrap(fn: Callable[..., Any]) -> BoundCapability:
        cap_name = name or fn.__name__
        desc = description if description is not None else (inspect.getdoc(fn) or "")
        spec = Capability(
            name=cap_name,
            description=desc,
            input_schema=_schema_from_signature(fn),
            access=_classify(cap_name, reads=reads, destructive=destructive),
            confirm=confirm,
            idempotent=idempotent,
            scope=scope,
        )
        return BoundCapability(spec=spec, func=fn)

    return wrap(func) if func is not None else wrap


def classify(name: str) -> Access:
    """Classify a capability by its name's leading verb (ambiguous ⇒ write, §2.4)."""
    return _classify(name, reads=False, destructive=False)


def _classify(name: str, *, reads: bool, destructive: bool) -> Access:
    # Explicit overrides beat the heuristic; contradictory ones are a developer error.
    if reads and destructive:
        raise CapabilityError(
            f"capability {name!r} is marked both reads=True and destructive=True",
            hint="a capability is read-only or destructive, not both — pick one",
        )
    if destructive:
        return Access.DESTRUCTIVE
    if reads:
        return Access.READ
    verb = name.split("_", 1)[0].lower()
    if verb in DESTRUCTIVE_VERBS:
        return Access.DESTRUCTIVE
    if verb in READ_VERBS:
        return Access.READ
    if verb in WRITE_VERBS:
        return Access.WRITE
    return Access.WRITE  # ambiguous ⇒ write (invariant §2.4)


def _schema_from_signature(fn: Callable[..., Any]) -> dict[str, Any]:
    try:
        hints = get_type_hints(fn)
    except Exception:  # one unresolved annotation must not blank the others
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in inspect.signature(fn).parameters.items():
        if pname in _SKIP_PARAMS or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise CapabilityError(
                f"capability {fn.__name__!r} has positional-only parameter {pname!r}",
                hint="capabilities are called by keyword — remove the '/' from the signature",
            )
        # Per-parameter so a single unresolved annotation doesn't blank the rest.
        ann = hints.get(pname)
        if ann is None:
            ann = param.annotation if isinstance(param.annotation, type) else Any
        properties[pname] = _json_type(ann)
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _json_type(ann: Any) -> dict[str, Any]:
    origin = get_origin(ann)
    if origin is Union or origin is UnionType:  # X | None -> the non-None arm
        non_none = [a for a in get_args(ann) if a is not type(None)]
        return _json_type(non_none[0]) if non_none else {}
    if origin in (list, tuple, set, frozenset):
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    if isinstance(ann, type):
        if ann in _PY_TO_JSON:
            return {"type": _PY_TO_JSON[ann]}
        if ann is list:
            return {"type": "array"}
        if ann is dict:
            return {"type": "object"}
    return {}  # unknown / Any — accept anything
