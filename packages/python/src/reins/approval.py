"""The human gate (M2.3): pause a gated write for approval before it executes.

The policy (M2.2) decides *whether* approval is needed; this module decides *how*
the human is asked. The handler is injected (``Agent(approve=...)``) like every
other dependency, so tests script the decision deterministically; the default is
:class:`TerminalApprovalHandler` — a preview plus ``[y]es / [n]o / [s]how`` on the
terminal. Everything fails closed (invariant §2.2, ENGINEERING.md §2): no
interactive human, an exhausted re-prompt bound, or a handler that raises all deny.
Reads never reach this module — ``ask()`` refuses writes before the policy runs,
and the loop only gates write/destructive actions.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from reins.errors import ApprovalError
from reins.policy import ProposedAction
from reins.types import Access

# The preview is rendered from model-controlled data (call names, argument keys
# and values), so every dimension of it is bounded (ENGINEERING.md §2).
_VALUE_WIDTH = 48  # bound each rendered argument value
_LINE_WIDTH = 160  # bound each rendered call line (keys are model-controlled too)
_MAX_PREVIEW_ACTIONS = 8  # bound the listed actions; [s]how reveals the rest
_MAX_PROMPTS = 5  # bound the terminal re-prompt loop (invariant §2.13)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """One gated batch: the exact calls about to run, and why they are gated.

    ``actions`` holds the write/destructive actions of the turn (reads run freely
    and are never what an approval is about). ``reason`` is ``approval_required``
    (the policy demanded the gate) or ``bulk_confirm`` (the row-limit default
    caught a multi-row write). ``bulk_count`` is how many items the largest bulk
    argument touches, when bulk was detected.
    """

    actions: tuple[ProposedAction, ...]
    reason: str
    bulk_count: int | None = None

    def preview(self) -> str:
        """The human-facing rendering: exact capability calls, every dimension bounded."""
        if len(self.actions) == 1:
            lines = [
                f"⚠ Reins wants to run a {_access_word(self.actions[0])}: "
                f"{_render_call(self.actions[0])}"
            ]
        else:
            lines = [f"⚠ Reins wants to run {len(self.actions)} gated operations:"]
            shown = self.actions[:_MAX_PREVIEW_ACTIONS]
            lines += [
                f"    {_render_call(action)}  [{_access_word(action).lower()}]" for action in shown
            ]
            if len(self.actions) > len(shown):
                lines.append(f"    … and {len(self.actions) - len(shown)} more — [s]how lists all.")
        if self.bulk_count is not None:
            lines.append(f"  This will touch up to {self.bulk_count:,} items.")
        return "\n".join(lines)

    def detail(self) -> str:
        """The unabridged calls as JSON — what ``[s]how`` reveals."""
        calls = [
            {"capability": action.call.name, "arguments": action.call.arguments}
            for action in self.actions
        ]
        return json.dumps(calls, indent=2, ensure_ascii=False, default=str)


@runtime_checkable
class ApprovalHandler(Protocol):
    """The human gate: return ``True`` to execute the gated actions, ``False`` to deny."""

    def approve(self, request: ApprovalRequest) -> bool: ...


ApprovalLike = ApprovalHandler | Callable[[ApprovalRequest], bool]
"""What ``approve=`` accepts: a handler object or a plain ``request -> bool`` callable."""


@dataclass(slots=True)
class TerminalApprovalHandler:
    """The default gate: print the preview, ask ``[y]es / [n]o / [s]how``.

    ``[s]how`` prints the exact calls and arguments as JSON, then re-asks. It
    denies fail-closed when there is no interactive human (EOF, closed stdin,
    interrupt) and when the re-prompt bound is exhausted — so headless runs and
    CI can never auto-approve a write. ``input_fn`` / ``print_fn`` are injectable
    for deterministic tests.
    """

    input_fn: Callable[[str], str] = input
    print_fn: Callable[[str], None] = print

    def approve(self, request: ApprovalRequest) -> bool:
        self.print_fn(request.preview())
        for _ in range(_MAX_PROMPTS):
            try:
                answer = self.input_fn("Approve? [y]es / [n]o / [s]how: ").strip().lower()
            except (EOFError, KeyboardInterrupt, OSError):
                # No interactive human (CI, piped stdin, ^C) ⇒ deny, never approve.
                self.print_fn(
                    "No interactive terminal — denying."
                    "\n  → run interactively, or pass approve= to decide programmatically."
                )
                return False
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            if answer in ("s", "show"):
                self.print_fn(request.detail())
                continue
            self.print_fn("Unrecognized — answer y, n, or s.")
        self.print_fn("Too many unrecognized answers — denying.\n  → answer y, n, or s.")
        return False  # re-prompt bound exhausted ⇒ deny (fail closed)


@dataclass(frozen=True, slots=True)
class _CallableHandler:
    """Adapter so a plain ``request -> bool`` callable can be the gate."""

    func: Callable[[ApprovalRequest], bool]

    def approve(self, request: ApprovalRequest) -> bool:
        return bool(self.func(request))


def as_approval_handler(approve: ApprovalLike) -> ApprovalHandler:
    """Coerce ``approve=`` to a handler: objects with ``.approve`` pass through,
    plain callables are wrapped. Anything else is a configuration error, raised
    here (at construction) so it never masquerades as a human denial."""
    if isinstance(approve, ApprovalHandler):
        return approve
    if callable(approve):
        return _CallableHandler(approve)
    raise ApprovalError(
        f"approve= must be an ApprovalHandler or a callable, got {type(approve).__name__}",
        hint="pass approve=lambda request: True to auto-approve, or an object with .approve()",
    )


def bulk_count(actions: Sequence[ProposedAction]) -> int | None:
    """How many items the largest bulk write argument touches, or ``None``.

    The pre-ORM row-limit default: a write/destructive call with a top-level
    argument that is a list of more than one item is treated as touching that
    many rows, so it needs an explicit confirm. Reads never count (they must
    never prompt), nested shapes are out of scope for the heuristic, and real
    row counts arrive with the ORM adapters (M4.1).
    """
    counts = [
        len(value)
        for action in actions
        if action.capability.access is not Access.READ
        for value in action.call.arguments.values()
        if isinstance(value, list) and len(value) > 1
    ]
    return max(counts) if counts else None


def _access_word(action: ProposedAction) -> str:
    # A custom policy may gate a read-bearing batch; label each action honestly.
    if action.capability.access is Access.DESTRUCTIVE:
        return "DESTRUCTIVE WRITE"
    if action.capability.access is Access.READ:
        return "READ"
    return "WRITE"


def _render_call(action: ProposedAction) -> str:
    args = ", ".join(f"{key}={_bounded(value)}" for key, value in action.call.arguments.items())
    text = f"{action.call.name}({args})"
    if len(text) > _LINE_WIDTH:  # argument keys are model-controlled; bound the line too
        return text[:_LINE_WIDTH] + "…)"
    return text


def _bounded(value: object) -> str:
    text = repr(value)
    if len(text) > _VALUE_WIDTH:
        return text[:_VALUE_WIDTH] + "…"
    return text
