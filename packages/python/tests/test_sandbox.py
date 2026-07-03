"""Tests for the sandbox backends (M3.2): program execution, the capability
bridge, and — most importantly — the escape attempts that must be blocked."""

import os
import shutil
from typing import Any

import pytest

from reins.sandbox import SandboxOutcome, SubprocessSandbox
from reins.types import CapabilityResult


def _run(
    program: str,
    *,
    capabilities: list[str] | None = None,
    results: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> SandboxOutcome:
    """Run a program with a bridge that serves canned per-capability results."""
    table = results or {}

    def call(name: str, arguments: dict[str, Any]) -> CapabilityResult:
        if name in table:
            return CapabilityResult(ok=True, value=table[name])
        return CapabilityResult(ok=False, error=f"no capability named {name!r}")

    return SubprocessSandbox().run(program, capabilities or list(table), call, timeout=timeout)


# --- basic execution ------------------------------------------------------------


def test_runs_a_program_and_captures_print_output() -> None:
    outcome = _run("print('hello from the sandbox')")
    assert outcome.ok
    assert outcome.output.strip() == "hello from the sandbox"


def test_capability_proxy_round_trips_values() -> None:
    outcome = _run(
        "rows = find_orders(status='open')\nprint(len(rows), rows[0]['id'])",
        results={"find_orders": [{"id": 7}, {"id": 8}]},
    )
    assert outcome.ok
    assert outcome.output.strip() == "2 7"


def test_capabilities_are_importable_as_a_module() -> None:
    outcome = _run(
        "from capabilities import find_orders\nprint(len(find_orders()))",
        results={"find_orders": [1, 2, 3]},
    )
    assert outcome.ok
    assert outcome.output.strip() == "3"


def test_whitelisted_stdlib_imports_work() -> None:
    outcome = _run("import json, math\nprint(json.dumps({'pi': round(math.pi, 2)}))")
    assert outcome.ok
    assert '"pi": 3.14' in outcome.output


def test_failed_capability_call_is_catchable_inside_the_program() -> None:
    outcome = _run(
        "try:\n    nope()\nexcept CapabilityError as exc:\n    print('caught:', exc)\n",
        capabilities=["nope"],
        results={},
    )
    assert outcome.ok
    assert "caught:" in outcome.output


def test_program_exception_is_reported_with_partial_output() -> None:
    outcome = _run("print('before')\nraise ValueError('boom')")
    assert outcome.ok is False
    assert "ValueError: boom" in (outcome.error or "")
    assert "before" in outcome.output


def test_syntax_error_is_reported_as_data() -> None:
    outcome = _run("def broken(:\n")
    assert outcome.ok is False
    assert "SyntaxError" in (outcome.error or "")


def test_output_is_bounded() -> None:
    outcome = _run("print('x' * 100_000)")
    assert outcome.ok
    assert len(outcome.output) < 11_000
    assert "trimmed" in outcome.output


def test_timeout_kills_a_runaway_program() -> None:
    outcome = _run("while True:\n    pass\n", timeout=1.0)
    assert outcome.ok is False
    assert "timed out" in (outcome.error or "")
    assert "→" in (outcome.error or "")


# --- escapes that MUST be blocked (invariant §2.9) --------------------------------


@pytest.mark.parametrize(
    "program",
    [
        "import socket",  # network
        "import urllib.request",  # network
        "import os",  # filesystem / process
        "import subprocess",  # process spawning
        "import sys",  # interpreter internals
        "import pathlib",  # filesystem
        "__import__('os')",  # dynamic import of a blocked module
    ],
)
def test_blocked_imports(program: str) -> None:
    outcome = _run(program)
    assert outcome.ok is False
    assert "blocked" in (outcome.error or "")


def test_open_is_unavailable() -> None:
    outcome = _run("open('/etc/passwd')")
    assert outcome.ok is False
    assert "NameError" in (outcome.error or "")


def test_exec_eval_compile_are_unavailable() -> None:
    for snippet in ("exec('1')", "eval('1')", "compile('1', 'x', 'eval')"):
        outcome = _run(snippet)
        assert outcome.ok is False, snippet
        assert "NameError" in (outcome.error or ""), snippet


def test_input_is_unavailable() -> None:
    # The child's real stdin is the RPC channel; a program must not reach it.
    outcome = _run("input()")
    assert outcome.ok is False
    assert "NameError" in (outcome.error or "")


def test_no_ambient_environment_reaches_the_program() -> None:
    # -I isolated mode + blocked os: there is no path to env secrets.
    outcome = _run("import os\nprint(os.environ)")
    assert outcome.ok is False
    assert "blocked" in (outcome.error or "")


def test_call_budget_is_bounded() -> None:
    program = (
        "for i in range(400):\n"
        "    try:\n"
        "        f()\n"
        "    except CapabilityError:\n"
        "        pass\n"
        "print('done')"
    )
    outcome = _run(program, results={"f": 1})
    assert outcome.ok
    assert "done" in outcome.output  # bounded, not crashed: excess calls error as data


# --- docker backend (live-optional) ------------------------------------------------


@pytest.mark.skipif(
    not (shutil.which("docker") and os.environ.get("REINS_DOCKER_TESTS")),
    reason="needs docker and REINS_DOCKER_TESTS=1",
)
def test_docker_sandbox_runs_a_program() -> None:
    from reins.sandbox import DockerSandbox

    outcome = DockerSandbox().run(
        "print(sum(range(10)))",
        [],
        lambda name, arguments: CapabilityResult(ok=False, error="none"),
        timeout=30.0,
    )
    assert outcome.ok
    assert outcome.output.strip() == "45"
