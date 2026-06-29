"""(De)serialization codec for the Reins value types.

``to_dict`` turns a frozen dataclass — with enum / datetime / nested-dataclass /
list / arbitrary-JSON fields — into JSON-safe primitives; ``from_dict`` reverses
it, rejecting malformed input with a typed, ``→``-hinted :class:`SerializationError`.
The pair round-trips: ``from_dict(cls, to_dict(x)) == x``.

It is a small, deliberate reflective codec (driven by ``dataclasses.fields`` +
``typing.get_type_hints``) rather than a serialization dependency — see CLAUDE.md
§0 (min-deps) and §9 (prefer dataclasses). Arbitrary-JSON fields (``dict[str, Any]``
and ``Any``) are passed through unvalidated; schema validation is M1.3's job.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from enum import Enum
from types import UnionType
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from reins.errors import SerializationError

T = TypeVar("T")


def to_dict(obj: Any) -> Any:
    """Recursively encode a dataclass / enum / datetime / list / JSON value to JSON-safe data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj  # str / int / float / bool / None pass through unchanged


def from_dict(cls: type[T], data: Any) -> T:
    """Decode ``data`` into an instance of the dataclass ``cls``, validating structure."""
    if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
        raise SerializationError(
            f"cannot decode into {cls!r}",
            hint="from_dict only builds the dataclasses in reins.types",
        )
    if not isinstance(data, dict):
        raise SerializationError(
            f"expected a mapping to build {cls.__name__}, got {type(data).__name__}",
            hint=f"pass the dict produced by to_dict() for {cls.__name__}",
        )
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            kwargs[f.name] = _decode(hints[f.name], data[f.name], f"{cls.__name__}.{f.name}")
        elif not _has_default(f):
            raise SerializationError(
                f"missing required field {f.name!r} for {cls.__name__}",
                hint=f"include {f.name!r} (see spec/contract.md / OAH §5)",
            )
    ctor: Any = cls  # avoid mypy "instantiate protocol" on the narrowed dataclass type
    return cast(T, ctor(**kwargs))


def _has_default(f: dataclasses.Field[Any]) -> bool:
    return f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING


def _decode(ann: Any, value: Any, path: str) -> Any:
    """Decode one value against its type annotation; raise SerializationError on mismatch."""
    origin = get_origin(ann)

    if origin is Union or origin is UnionType:
        args = get_args(ann)
        if value is None:
            if type(None) in args:
                return None
            raise SerializationError(
                f"{path}: got null for a required field", hint="provide a value"
            )
        non_none = [a for a in args if a is not type(None)]
        return _decode(non_none[0], value, path)

    if origin is list:
        elem_args = get_args(ann)
        elem = elem_args[0] if elem_args else Any
        if not isinstance(value, list):
            raise SerializationError(
                f"{path}: expected a list, got {type(value).__name__}", hint="provide a JSON array"
            )
        return [_decode(elem, v, f"{path}[{i}]") for i, v in enumerate(value)]

    if origin is dict:
        if not isinstance(value, dict):
            raise SerializationError(
                f"{path}: expected a mapping, got {type(value).__name__}",
                hint="provide a JSON object",
            )
        return value  # arbitrary JSON object preserved as-is

    if ann is Any:
        return value  # arbitrary JSON value preserved as-is

    if isinstance(ann, type):
        if dataclasses.is_dataclass(ann):
            return from_dict(ann, value)
        if issubclass(ann, Enum):
            try:
                return ann(value)
            except ValueError as exc:
                allowed = [e.value for e in ann]
                raise SerializationError(
                    f"{path}: {value!r} is not a valid {ann.__name__}", hint=f"use one of {allowed}"
                ) from exc
        if ann is datetime:
            if not isinstance(value, str):
                raise SerializationError(
                    f"{path}: expected an ISO-8601 string, got {type(value).__name__}",
                    hint="serialize datetimes with .isoformat()",
                )
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise SerializationError(
                    f"{path}: {value!r} is not an ISO-8601 timestamp",
                    hint="use an ISO-8601 timestamp",
                ) from exc
        if ann is bool:
            if not isinstance(value, bool):
                raise SerializationError(
                    f"{path}: expected bool, got {type(value).__name__}",
                    hint="provide true or false",
                )
            return value
        if ann is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise SerializationError(
                    f"{path}: expected int, got {type(value).__name__}", hint="provide an integer"
                )
            return value
        if ann is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SerializationError(
                    f"{path}: expected a number, got {type(value).__name__}",
                    hint="provide a number",
                )
            return float(value)
        if ann is str:
            if not isinstance(value, str):
                raise SerializationError(
                    f"{path}: expected str, got {type(value).__name__}", hint="provide a string"
                )
            return value

    return value  # unknown annotation kind — pass through (not expected for reins.types)
