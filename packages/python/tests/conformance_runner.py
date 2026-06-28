"""Conformance runner — the Python adapter for the cross-language conformance suite.

It loads the language-neutral cases in ``spec/conformance/cases/*.yaml`` and drives
each through the Reins harness with a scripted FakeModel, asserting the case's
``expect`` block. The cases are the source of truth; the Go and TS packages ship
their own thin runners over the *same* cases (CLAUDE.md §3, spec/contract.md §7).

Status (M0.2): the behavior under test is delivered from M1.x onward. Until a
case's feature exists, :func:`drive_case` raises :class:`HarnessNotImplemented`
and the case is reported **xfail** — so ``make conformance`` runs and reports
per-case status today, and each case flips to a real pass as its feature lands.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

# packages/python/tests/conformance_runner.py -> repo root is parents[3].
CASES_DIR = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"

GOLDEN_OUTCOMES = frozenset({"refused", "not_executed", "completed", "executed", "stopped"})
GOLDEN_REASONS = frozenset({"write_in_read_only", "approval_denied", "budget_exhausted"})
LINT_RESULTS = frozenset({"pass", "fail"})
VIOLATION_CODES = frozenset(
    {"opaque_name", "missing_description", "too_many_params", "raw_response", "ambiguous_overlap"}
)
CASE_KINDS = frozenset({"golden", "linter"})


class HarnessNotImplemented(NotImplementedError):
    """The feature a case exercises is not built yet.

    The runner converts this into an xfail so the suite reports progress as the
    milestones in CLAUDE.md §10 land, with no per-case marks to maintain.
    """


@dataclass(frozen=True, slots=True)
class Case:
    """One loaded, validated conformance case."""

    name: str
    kind: str
    source: str
    data: dict[str, Any]


def _hint(problem: str, fix: str) -> str:
    """Build an error message that ends with a ``→ next step`` (invariant §2.19)."""
    return f"{problem}\n  → {fix}"


def _require_mapping(value: Any, source: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(
            _hint(f"{source}: {what} is not a mapping", f"write {what} as a YAML mapping")
        )
    return value


def infer_kind(data: dict[str, Any]) -> str:
    """Infer the kind from shape: a ``capability`` block => linter, a ``goal`` => golden."""
    if "capability" in data:
        return "linter"
    if "goal" in data:
        return "golden"
    raise ValueError(
        _hint(
            "case has neither a `capability` block (linter) nor a `goal` (golden)",
            "add `goal` + `verb` for a golden case, or a `capability` block for a linter case",
        )
    )


def _validate_golden(data: dict[str, Any], expect: dict[str, Any], source: str) -> None:
    if data.get("verb") not in ("ask", "run"):
        raise ValueError(
            _hint(
                f"{source}: golden case needs `verb`",
                "set verb: ask (read-only) or run (may write)",
            )
        )
    if not isinstance(data.get("capabilities"), list):
        raise ValueError(
            _hint(
                f"{source}: golden case needs a `capabilities` list",
                "list the capability names available to the agent",
            )
        )
    if not isinstance(data.get("model_script"), list):
        raise ValueError(
            _hint(
                f"{source}: golden case needs a `model_script` list",
                "script the FakeModel's decisions as a list of steps",
            )
        )
    outcome = expect.get("outcome")
    if outcome not in GOLDEN_OUTCOMES:
        raise ValueError(
            _hint(
                f"{source}: expect.outcome {outcome!r} is invalid",
                f"use one of {sorted(GOLDEN_OUTCOMES)}",
            )
        )
    reason = expect.get("reason")
    if reason is not None and reason not in GOLDEN_REASONS:
        raise ValueError(
            _hint(
                f"{source}: expect.reason {reason!r} is not a known reason code",
                f"use one of {sorted(GOLDEN_REASONS)} (or omit it)",
            )
        )


def _validate_linter(data: dict[str, Any], expect: dict[str, Any], source: str) -> None:
    _require_mapping(data.get("capability"), source, "`capability`")
    if expect.get("lint") not in LINT_RESULTS:
        raise ValueError(
            _hint(f"{source}: expect.lint must be pass|fail", "set expect.lint: pass or fail")
        )
    violations = expect.get("violations", [])
    if not isinstance(violations, list):
        raise ValueError(
            _hint(
                f"{source}: expect.violations must be a list",
                "list violation codes, or omit for none",
            )
        )
    unknown = sorted(v for v in violations if v not in VIOLATION_CODES)
    if unknown:
        raise ValueError(
            _hint(
                f"{source}: unknown violation code(s) {unknown}",
                f"use codes from {sorted(VIOLATION_CODES)}",
            )
        )


def validate_case(data: Any, source: str) -> Case:
    """Validate one raw case mapping and return a :class:`Case`, or raise ``ValueError``."""
    mapping = _require_mapping(data, source, "case")
    name = mapping.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(
            _hint(f"{source}: missing or empty `name`", "give the case a unique string `name`")
        )
    kind = mapping.get("kind") or infer_kind(mapping)
    if kind not in CASE_KINDS:
        raise ValueError(
            _hint(
                f"{source}: unknown kind {kind!r}",
                "use kind: golden | linter (or omit to infer)",
            )
        )
    expect = _require_mapping(mapping.get("expect"), source, "`expect`")
    if kind == "golden":
        _validate_golden(mapping, expect, source)
    else:
        _validate_linter(mapping, expect, source)
    return Case(name=name, kind=kind, source=source, data=mapping)


def load_cases(cases_dir: Path = CASES_DIR) -> list[Case]:
    """Load and validate every ``*.yaml`` case, sorted by filename. Names must be unique."""
    if not cases_dir.is_dir():
        raise FileNotFoundError(
            _hint(
                f"conformance cases dir not found: {cases_dir}",
                "expected spec/conformance/cases/*.yaml",
            )
        )
    cases: list[Case] = []
    seen: set[str] = set()
    for path in sorted(cases_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(
                _hint(f"{path.name}: invalid YAML ({exc})", "fix the YAML syntax")
            ) from exc
        case = validate_case(raw, source=path.name)
        if case.name in seen:
            raise ValueError(
                _hint(
                    f"duplicate case name {case.name!r} in {path.name}",
                    "case names must be unique across the suite",
                )
            )
        seen.add(case.name)
        cases.append(case)
    if not cases:
        raise FileNotFoundError(
            _hint(f"no .yaml cases found in {cases_dir}", "add cases under spec/conformance/cases/")
        )
    return cases


def drive_case(case: Case) -> dict[str, Any]:
    """Run one case through the Reins harness and return the observed result.

    Placeholder until the behavior under test exists:
      * golden cases need the loop + verbs + policy/approval (M1.4, M2.x);
      * linter cases need the CapabilityLinter (M1.3).
    Raises :class:`HarnessNotImplemented` until then; the runner reports xfail.
    """
    raise HarnessNotImplemented(
        f"{case.kind} case {case.name!r} needs features from a later milestone "
        "(golden: M1.4/M2.x, linter: M1.3)"
    )


def assert_outcome(case: Case, result: dict[str, Any]) -> None:
    """Assert an observed ``result`` matches the case's ``expect`` block."""
    expect = case.data["expect"]
    if case.kind == "golden":
        assert result.get("outcome") == expect.get("outcome"), f"{case.name}: outcome"
        if "reason" in expect:
            assert result.get("reason") == expect["reason"], f"{case.name}: reason"
        if "executed" in expect:
            assert result.get("executed") == expect["executed"], f"{case.name}: executed"
        if "audited" in expect:
            assert result.get("audited") == expect["audited"], f"{case.name}: audited"
    else:
        assert result.get("lint") == expect.get("lint"), f"{case.name}: lint"
        if "violations" in expect:
            got = sorted(result.get("violations", []))
            assert got == sorted(expect["violations"]), f"{case.name}: violations"


def _case_id(case: Case) -> str:
    return f"{case.kind}:{case.name}"


@pytest.mark.parametrize("case", load_cases(), ids=_case_id)
def test_conformance_case(case: Case) -> None:
    """Drive one conformance case; xfail until its feature is implemented (§7.6)."""
    try:
        result = drive_case(case)
    except HarnessNotImplemented as exc:
        # pytest.xfail is NoReturn, so assert_outcome below is unreachable until
        # drive_case is implemented in a later milestone.
        pytest.xfail(str(exc))
    assert_outcome(case, result)
