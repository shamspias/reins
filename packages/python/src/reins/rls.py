"""Row-level security (M2.4): capabilities are scoped to the caller's identity.

Reins guarantees the *plumbing* of RLS: the principal set at the entry point
reaches every capability call as ``Context.principal`` (invariant §2.7), and a
capability annotated with ``scope=...`` refuses to run without one (fail
closed, invariant §2.8). The capability's own code (or the ORM adapters from
M4.x) then uses the principal to filter rows — Reins never guesses which rows
belong to whom.
"""

from __future__ import annotations

from reins.types import Capability, Context


def scope_violation(capability: Capability, context: Context | None) -> str | None:
    """Why this call must not run, or ``None`` if scoping is satisfied.

    A ``scope``-annotated capability requires an authenticated principal — an
    anonymous call to it is refused before the function is invoked.
    """
    if capability.scope is None:
        return None
    if context is None or context.principal is None:
        return (
            f"{capability.name!r} is scoped to {capability.scope!r} "
            "but the run has no principal"
            "\n  → pass principal=... to ask()/run() or Agent(principal=...)"
        )
    return None
