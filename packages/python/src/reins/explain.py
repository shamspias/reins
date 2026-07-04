"""Transparency (M5.1): render a run's trace as a readable account.

``agent.explain()`` (and ``--trace``) answer "what did the agent just do?" —
which operations it discovered, the code it wrote in code-mode, each capability
call and whether it succeeded, every approval decision, and how the run ended.
It reads the structured :class:`~reins.types.TraceEvent` list the loop records;
no re-execution, no guessing. Capability arguments were PII-redacted when the
trace was built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reins.loop import RunReport

# A glyph per event kind, so a trace scans at a glance. Unknown kinds get "•".
_GLYPH = {
    "search": "🔍",
    "program": "⌨",
    "output": "→",
    "call": "•",
    "approval": "⚠",
    "refused": "✗",
    "denied": "✗",
    "stopped": "■",
}


def render(report: RunReport) -> str:
    """A multi-line, human-readable account of ``report``'s run."""
    lines = [
        "── reins trace ──",
        f"outcome: {report.outcome}"
        + (f" ({report.reason})" if report.reason else "")
        + f"  ·  turns: {report.turn}  ·  tokens: "
        f"{report.usage.input_tokens + report.usage.output_tokens}"
        + (f"  ·  ${report.usage.cost:.4f}" if report.usage.cost else "")
        + (f"  ·  compactions: {report.compactions}" if report.compactions else ""),
    ]
    if report.executed:
        lines.append(f"executed: {', '.join(report.executed)}")
    lines.append("")

    if not report.trace:
        lines.append("(no capability calls, programs, or discovery this run)")
    for event in report.trace:
        glyph = _GLYPH.get(event.kind, "•")
        mark = "" if event.ok else "  [failed]"
        if event.kind == "program":
            lines.append(f"{glyph} wrote a program:")
            lines.extend(f"    {line}" for line in event.detail.splitlines())
        else:
            lines.append(f"{glyph} {event.kind}: {event.detail}{mark}")

    answer = report.output.text
    if answer:
        lines.append("")
        lines.append(f"answer: {answer}")
    return "\n".join(lines)
