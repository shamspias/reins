"""The subprocess sandbox (M3.2) — the development-tier backend.

Runs the child runtime in ``python -I`` (isolated mode: no env vars, no user
site) with a whitelisted-import, reduced-builtins namespace and no stdin
beyond the RPC channel. Network and filesystem access are blocked by removing
the imports and builtins that reach them.

**Honestly labeled (§0, ENGINEERING.md §5):** this is an isolation aid for
development, NOT a hard security boundary — a determined program can escape a
same-kernel Python namespace. Production deployments should pass
``sandbox=DockerSandbox()`` (or something stricter).
"""

from __future__ import annotations

import subprocess
import sys

from reins.sandbox.runtime import (
    CHILD_RUNNER,
    DEFAULT_TIMEOUT,
    BridgeCall,
    SandboxOutcome,
    drive,
)


class SubprocessSandbox:
    """A :class:`~reins.sandbox.Sandbox` running programs in ``python -I``."""

    def run(
        self,
        program: str,
        capabilities: list[str],
        call: BridgeCall,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> SandboxOutcome:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", CHILD_RUNNER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return drive(proc, program, capabilities, call, timeout)
