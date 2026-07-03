"""Sandboxes for code-mode (M3.2): where agent-written programs run.

``SubprocessSandbox`` is the development default (whitelisted imports, reduced
builtins — an isolation aid, not a hard boundary); ``DockerSandbox`` is the
production tier (no network, read-only, resource-limited container). Both
speak the same bridged protocol, and every capability call a program makes is
gated by the harness exactly like a direct call (§2.2, §2.9).
"""

from reins.sandbox.docker import DockerSandbox
from reins.sandbox.runtime import (
    DEFAULT_TIMEOUT,
    BridgeCall,
    Sandbox,
    SandboxOutcome,
)
from reins.sandbox.subproc import SubprocessSandbox

__all__ = [
    "DEFAULT_TIMEOUT",
    "BridgeCall",
    "DockerSandbox",
    "Sandbox",
    "SandboxOutcome",
    "SubprocessSandbox",
]
