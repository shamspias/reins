"""Typed errors for Reins.

Every user-facing error carries a ``.hint`` — the ``→ next step`` that invariant
§2.19 (ENGINEERING.md §2) requires — and renders it in ``str()``. This module is
the seed of the error set that M5.1 expands; the base class lives here because the
value-type codec needs it now.
"""


class ReinsError(Exception):
    """Base for all Reins errors. Carries a human ``.hint`` (the ``→`` next step)."""

    def __init__(self, message: str, *, hint: str) -> None:
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:
        return f"{super().__str__()}\n  → {self.hint}"


class SerializationError(ReinsError):
    """A value could not be (de)serialized: malformed, missing, or wrong-typed data."""


class ModelError(ReinsError):
    """A control-plane failure in the model layer: no provider configured, the
    provider/SDK is unavailable, or the model returned unusable output."""


class CapabilityError(ReinsError):
    """A capability call cannot proceed: unknown name, or arguments that fail schema
    validation. Model output is untrusted — the schema is the prepared statement
    (invariant §2.6)."""
