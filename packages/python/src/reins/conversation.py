"""Context discipline (M3.3): keep the conversation inside the token window.

A long run's history grows without bound — mostly capability results and
programs' output. When the estimated size crosses a threshold, :func:`compact`
drops the oldest middle of the conversation while **always preserving the goal**
(the system prompt and the first user message) and the most recent turns, so the
model never loses what it is trying to do.

Compaction is deterministic — no model call, no summarizer — and safe for the
provider wire format: it never leaves a tool result whose originating tool call
was dropped (that would break the Anthropic/OpenAI adapters). Token counts are a
cheap, documented heuristic (~4 chars/token), not exact.
"""

from __future__ import annotations

import json

from reins.types import Message, Role

DEFAULT_COMPACT_AT = 24_000  # token estimate at which history compaction kicks in
_KEEP_RECENT = 6  # most-recent messages always preserved intact
_CHARS_PER_TOKEN = 4  # rough heuristic; deltas are what matter, not absolute counts


def estimate_tokens(messages: list[Message]) -> int:
    """A cheap upper-ish estimate of the conversation's token size."""
    chars = 0
    for message in messages:
        if message.text:
            chars += len(message.text)
        for call in message.tool_calls:
            chars += len(call.name) + len(json.dumps(call.arguments, default=str))
    return chars // _CHARS_PER_TOKEN + len(messages)  # +1/message for role overhead


def compact(
    messages: list[Message],
    *,
    compact_at: int | None,
    keep_recent: int = _KEEP_RECENT,
) -> tuple[list[Message], bool]:
    """Return ``(messages, compacted)``, trimming the middle only if oversized.

    Always kept: the leading system prompt(s), the first user message (the goal),
    and the last ``keep_recent`` messages. The elided middle is replaced by one
    marker note. The recent window is advanced past any leading tool-result
    message so its originating tool call is never orphaned.
    """
    if compact_at is None or estimate_tokens(messages) <= compact_at:
        return messages, False

    n = len(messages)
    head_end = 0
    while head_end < n and messages[head_end].role is Role.SYSTEM:
        head_end += 1
    goal_idx = next((i for i, m in enumerate(messages) if m.role is Role.USER), None)
    if goal_idx is not None:
        head_end = max(head_end, goal_idx + 1)

    start = max(head_end, n - keep_recent)
    # Never begin the recent window on a tool result — its tool call would be in
    # the elided region, which breaks the provider wire format.
    while start < n and messages[start].role is Role.TOOL:
        start += 1
    if start >= n or start <= head_end:
        return messages, False  # nothing safely elidable

    elided = start - head_end
    marker = Message(
        role=Role.SYSTEM,
        text=f"[{elided} earlier step(s) elided to stay within the context window; "
        "the goal above and the recent steps below are preserved.]",
    )
    return [*messages[:head_end], marker, *messages[start:]], True
