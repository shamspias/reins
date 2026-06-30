"""Unit tests for the conformance runner's loader (M0.2).

These check the loader itself; the cross-language cases run via `make conformance`
(see conformance_runner.py). Covers the happy path and the unhappy paths that
matter: malformed cases must fail loudly with a `→`-hinted message (invariant §2.19).
"""

import pytest

from conformance_runner import (
    Case,
    HarnessNotImplemented,
    drive_case,
    infer_kind,
    load_cases,
    validate_case,
)

# The three canonical examples from CLAUDE.md §3 must always be present.
REQUIRED_CASES = {
    "ask_cannot_write",
    "destructive_requires_approval",
    "linter_rejects_raw_capability",
}


def test_load_cases_loads_a_valid_suite() -> None:
    cases = load_cases()
    assert len(cases) >= 5
    assert all(c.kind in ("golden", "linter") for c in cases)
    names = {c.name for c in cases}
    assert len(names) == len(cases), "case names must be unique"
    assert names >= REQUIRED_CASES


def test_approval_golden_cases_raise_until_m2() -> None:
    cases = [c for c in load_cases() if c.kind == "golden" and "approvals" in c.data]
    assert cases
    for case in cases:
        with pytest.raises(HarnessNotImplemented):
            drive_case(case)


def test_non_approval_golden_cases_drive_to_a_result() -> None:
    # As of M1.4 the ask/read/budget golden cases run for real.
    cases = [c for c in load_cases() if c.kind == "golden" and "approvals" not in c.data]
    assert cases
    for case in cases:
        result = drive_case(case)
        assert result["outcome"] in {"refused", "completed", "executed", "not_executed", "stopped"}


def test_linter_cases_drive_to_a_result() -> None:
    # As of M1.3 the linter cases run for real (no longer xfail).
    linter = [c for c in load_cases() if c.kind == "linter"]
    assert linter
    for case in linter:
        result = drive_case(case)
        assert result["lint"] in ("pass", "fail")


def test_infer_kind_golden() -> None:
    assert infer_kind({"goal": "x", "verb": "ask"}) == "golden"


def test_infer_kind_linter() -> None:
    assert infer_kind({"capability": {"name": "x"}}) == "linter"


def test_infer_kind_unknown_is_hinted_error() -> None:
    with pytest.raises(ValueError, match="→") as exc:
        infer_kind({"name": "mystery"})
    assert "neither" in str(exc.value)


def test_validate_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="→"):
        validate_case(["not", "a", "mapping"], source="bad.yaml")


def test_validate_rejects_missing_name() -> None:
    with pytest.raises(ValueError, match="name"):
        validate_case({"goal": "x", "verb": "ask"}, source="noname.yaml")


def test_validate_rejects_bad_golden_outcome() -> None:
    bad = {
        "name": "bad",
        "goal": "x",
        "verb": "ask",
        "capabilities": ["find"],
        "model_script": [],
        "expect": {"outcome": "exploded"},
    }
    with pytest.raises(ValueError, match="outcome"):
        validate_case(bad, source="bad.yaml")


def test_validate_rejects_missing_verb() -> None:
    bad = {
        "name": "bad",
        "goal": "x",
        "capabilities": ["find"],
        "model_script": [],
        "expect": {"outcome": "completed"},
    }
    with pytest.raises(ValueError, match="verb"):
        validate_case(bad, source="bad.yaml")


def test_validate_rejects_unknown_reason() -> None:
    # A typo'd reason must fail loudly at load time, not silently after the
    # feature lands (invariant §2.21: the suite is the cross-language backbone).
    bad = {
        "name": "bad",
        "goal": "x",
        "verb": "ask",
        "capabilities": ["find"],
        "model_script": [],
        "expect": {"outcome": "refused", "reason": "budget_exhuasted"},
    }
    with pytest.raises(ValueError, match="reason"):
        validate_case(bad, source="bad.yaml")


def test_validate_rejects_unknown_violation_code() -> None:
    bad = {
        "name": "bad",
        "capability": {"name": "x", "description": "d", "params": 1, "returns": "trimmed"},
        "expect": {"lint": "fail", "violations": ["opaqu_name"]},
    }
    with pytest.raises(ValueError, match="violation"):
        validate_case(bad, source="bad.yaml")


def test_validate_rejects_bad_lint_value() -> None:
    bad = {
        "name": "bad",
        "capability": {"name": "x", "description": "d", "params": 1, "returns": "trimmed"},
        "expect": {"lint": "maybe"},
    }
    with pytest.raises(ValueError, match="lint"):
        validate_case(bad, source="bad.yaml")


def test_validate_accepts_a_clean_golden_case() -> None:
    good = {
        "name": "ok",
        "goal": "count things",
        "verb": "ask",
        "capabilities": ["find"],
        "model_script": [{"call": "find"}],
        "expect": {"outcome": "completed", "executed": ["find"]},
    }
    case = validate_case(good, source="ok.yaml")
    assert isinstance(case, Case)
    assert case.kind == "golden"


def test_unknown_extra_keys_are_tolerated() -> None:
    # Forward-compatibility: extra keys must not break loading.
    good = {
        "name": "ok",
        "capability": {"name": "x", "description": "d", "params": 1, "returns": "trimmed"},
        "expect": {"lint": "pass"},
        "future_field": {"anything": True},
    }
    case = validate_case(good, source="ok.yaml")
    assert case.kind == "linter"
