"""The Docker sandbox (M3.2) — the production-tier backend (§0 default for prod).

Runs the same child runtime inside a locked-down container: no network,
read-only root filesystem, memory and pid limits. The container is the hard
boundary the subprocess tier honestly is not.
"""

from __future__ import annotations

import shutil
import subprocess

from reins.errors import SandboxError
from reins.sandbox.runtime import (
    CHILD_RUNNER,
    DEFAULT_TIMEOUT,
    BridgeCall,
    SandboxOutcome,
    drive,
)

DEFAULT_IMAGE = "python:3.12-alpine"


class DockerSandbox:
    """A :class:`~reins.sandbox.Sandbox` running programs in a locked-down container."""

    def __init__(self, image: str = DEFAULT_IMAGE) -> None:
        if shutil.which("docker") is None:
            raise SandboxError(
                "the 'docker' CLI was not found on PATH",
                hint="install Docker, or use the default SubprocessSandbox for development",
            )
        self._image = image

    def run(
        self,
        program: str,
        capabilities: list[str],
        call: BridgeCall,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> SandboxOutcome:
        proc = subprocess.Popen(
            [
                "docker",
                "run",
                "--rm",
                "--interactive",
                "--network",
                "none",
                "--read-only",
                "--memory",
                "256m",
                "--pids-limit",
                "64",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                self._image,
                "python",
                "-I",
                "-c",
                CHILD_RUNNER,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Container start-up is slower than a local process; give it headroom
        # beyond the program's own budget.
        return drive(proc, program, capabilities, call, timeout=timeout + 15.0)
