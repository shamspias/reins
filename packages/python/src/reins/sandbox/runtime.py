"""The sandbox contract and the shared child runtime (M3.2).

A sandbox runs one agent-written program in an isolated child process and
bridges its capability calls back to the harness over a line-delimited JSON
protocol on stdin/stdout:

    parent → child   {"type": "run", "program": ..., "capabilities": [...]}
    child  → parent  {"type": "call", "id": n, "name": ..., "arguments": {...}}
    parent → child   {"ok": true, "value": ...} | {"ok": false, "error": ...}
    child  → parent  {"type": "done", "output": ...} | {"type": "error", ...}

The child exposes each capability as a plain function (and as the importable
``capabilities`` module), restricts imports to a whitelist, and removes
file/exec/eval builtins — so intermediate data lives in the child and only the
program's ``print()`` output returns to the model's context (invariant §2.11).
Every capability call still crosses back into the harness, where the full gate
chain applies (§2.2) — the sandbox isolates *computation*, never *authority*.

Backends share :func:`drive`; they differ only in how the child is spawned
(local subprocess vs Docker). The subprocess tier is honestly labeled: it is
an isolation aid for development, **not** a hard security boundary — use
:class:`reins.sandbox.DockerSandbox` (or stricter) in production (§0).
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from reins.types import CapabilityResult

DEFAULT_TIMEOUT = 10.0  # seconds; a runaway program is a bug, not an edge case (§2.13)
_MAX_OUTPUT_CHARS = 10_000  # bound what re-enters the model's context
_MAX_PROGRAM_CALLS = 256  # bound how often one program may call capabilities

BridgeCall = Callable[[str, dict[str, Any]], CapabilityResult]
"""The harness-side handler for one in-program capability call (fully gated)."""


@dataclass(frozen=True, slots=True)
class SandboxOutcome:
    """What one program run produced: its printed output, or why it failed."""

    ok: bool
    output: str = ""
    error: str | None = None


@runtime_checkable
class Sandbox(Protocol):
    """Runs one agent-written program with bridged, gated capability calls."""

    def run(
        self,
        program: str,
        capabilities: list[str],
        call: BridgeCall,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> SandboxOutcome: ...


# The child runtime, passed verbatim to `python -I -c`. Self-contained on
# purpose: it must run in a bare container with nothing but the stdlib.
CHILD_RUNNER = r"""
import io, json, sys, types

_rpc_out = sys.stdout            # the RPC channel to the parent
sys.stdout = io.StringIO()       # the program's print() output


def _send(msg):
    _rpc_out.write(json.dumps(msg) + "\n")
    _rpc_out.flush()


def _recv():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)      # parent went away; nothing sane left to do
    return json.loads(line)


_ALLOWED_IMPORTS = {
    "bisect", "collections", "dataclasses", "datetime", "decimal", "fractions",
    "functools", "heapq", "itertools", "json", "math", "random", "re",
    "statistics", "string", "textwrap", "typing", "unicodedata",
}
_real_import = __import__


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root == "capabilities":
        return _caps_module
    if root not in _ALLOWED_IMPORTS:
        raise ImportError(
            "import of " + repr(name) + " is blocked in the sandbox; allowed: "
            + ", ".join(sorted(_ALLOWED_IMPORTS)) + ", capabilities"
        )
    return _real_import(name, globals, locals, fromlist, level)


class CapabilityError(RuntimeError):
    pass


_call_id = [0]


def _proxy(name):
    def call(**kwargs):
        _call_id[0] += 1
        _send({"type": "call", "id": _call_id[0], "name": name, "arguments": kwargs})
        reply = _recv()
        if reply.get("ok"):
            return reply.get("value")
        raise CapabilityError(reply.get("error") or "capability call failed")

    call.__name__ = name
    return call


_setup = _recv()
_caps_module = types.ModuleType("capabilities")

import builtins as _builtins

_SAFE_BUILTINS = {}
for _n in (
    "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "getattr", "hasattr",
    "hash", "hex", "int", "isinstance", "issubclass", "iter", "len", "list",
    "map", "max", "min", "next", "object", "oct", "ord", "pow", "print",
    "range", "repr", "reversed", "round", "set", "slice", "sorted", "str",
    "sum", "tuple", "type", "zip",
    "ArithmeticError", "AttributeError", "BaseException", "Exception",
    "IndexError", "KeyError", "LookupError", "NameError", "OverflowError",
    "RuntimeError", "StopIteration", "TypeError", "ValueError",
    "ZeroDivisionError",
    "True", "False", "None",
):
    if hasattr(_builtins, _n):
        _SAFE_BUILTINS[_n] = getattr(_builtins, _n)
_SAFE_BUILTINS["__import__"] = _guarded_import
_SAFE_BUILTINS["CapabilityError"] = CapabilityError

_globals = {"__builtins__": _SAFE_BUILTINS, "__name__": "__main__"}
for _name in _setup["capabilities"]:
    _fn = _proxy(_name)
    setattr(_caps_module, _name, _fn)
    if _name.isidentifier():
        _globals[_name] = _fn

try:
    exec(compile(_setup["program"], "<program>", "exec"), _globals)
    _send({"type": "done", "output": sys.stdout.getvalue()})
except BaseException as exc:
    _send({
        "type": "error",
        "error": type(exc).__name__ + ": " + str(exc),
        "output": sys.stdout.getvalue(),
    })
"""


def drive(
    proc: subprocess.Popen[str],
    program: str,
    capabilities: list[str],
    call: BridgeCall,
    timeout: float,
) -> SandboxOutcome:
    """Feed ``program`` to a spawned child and serve its capability calls.

    Bounded on every axis: a wall-clock timeout kills the child, the number of
    in-program capability calls is capped, and the returned output is trimmed.
    The child is always killed and reaped on exit — no orphans.
    """
    expired = threading.Event()

    def _kill() -> None:
        expired.set()
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    calls_served = 0
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(
            json.dumps({"type": "run", "program": program, "capabilities": capabilities}) + "\n"
        )
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                if expired.is_set():
                    return SandboxOutcome(
                        ok=False,
                        error=f"program timed out after {timeout:g}s"
                        "\n  → simplify the program, or raise the sandbox timeout",
                    )
                return SandboxOutcome(
                    ok=False,
                    error="the sandbox exited unexpectedly"
                    "\n  → check the program for a crash (e.g. out-of-memory) and retry",
                )
            try:
                msg = json.loads(line)
            except ValueError:
                return SandboxOutcome(
                    ok=False,
                    error="the sandbox spoke garbage (protocol violation)"
                    "\n  → this is a reins bug or a corrupted runtime; please report it",
                )
            if msg.get("type") == "call":
                calls_served += 1
                if calls_served > _MAX_PROGRAM_CALLS:
                    reply = {
                        "ok": False,
                        "error": f"call budget exhausted ({_MAX_PROGRAM_CALLS} per program)"
                        "\n  → batch the work, or split it across programs",
                    }
                else:
                    reply = _serve(call, msg)
                proc.stdin.write(json.dumps(reply, default=str) + "\n")
                proc.stdin.flush()
                continue
            output = _bounded_output(str(msg.get("output", "")))
            if msg.get("type") == "done":
                return SandboxOutcome(ok=True, output=output)
            return SandboxOutcome(
                ok=False,
                error=str(msg.get("error", "program failed")),
                output=output,
            )
    except BrokenPipeError:
        return SandboxOutcome(
            ok=False,
            error="the sandbox went away mid-run"
            "\n  → retry; if it persists, check the sandbox backend",
        )
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()


def _serve(call: BridgeCall, msg: dict[str, Any]) -> dict[str, Any]:
    name = msg.get("name")
    arguments = msg.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return {"ok": False, "error": "malformed capability call from the program"}
    result = call(name, arguments)
    if result.ok:
        return {"ok": True, "value": result.value}
    return {"ok": False, "error": result.error or "capability failed"}


def _bounded_output(output: str) -> str:
    if len(output) > _MAX_OUTPUT_CHARS:
        return output[:_MAX_OUTPUT_CHARS] + f"\n… (output trimmed at {_MAX_OUTPUT_CHARS} chars)"
    return output
