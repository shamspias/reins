"""The clock (M3.3): the harness's only source of wall-clock time.

Injected like every other dependency (ENGINEERING.md §3) so time budgets are
deterministic under test — a run never depends on real elapsed time unless it
is handed the real clock. Uses a monotonic source: immune to system clock
adjustments, and only differences are ever meaningful.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic time source. ``now()`` returns seconds; only deltas matter."""

    def now(self) -> float: ...


class MonotonicClock:
    """The default clock: :func:`time.monotonic`."""

    def now(self) -> float:
        return time.monotonic()
