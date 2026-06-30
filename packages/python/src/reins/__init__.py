"""Reins — a lightweight agent harness you bolt onto an existing app.

Point it at functions you already have (or your ORM models), hand it a goal in
plain language, and it operates your app through your own code — safely and
cheaply. See ``CLAUDE.md`` for the build plan and the non-negotiable invariants.

This is the M0.1 scaffold: the package is intentionally empty of features. The
public surface (``Agent``, ``capability``, ``ask`` / ``run``, ...) is added from
M1.1 onward, one milestone per release.
"""

from reins.capability import capability
from reins.loop import Agent

__all__ = ["Agent", "__version__", "capability"]

__version__ = "0.0.0"
