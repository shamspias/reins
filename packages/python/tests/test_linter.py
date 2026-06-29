"""Tests for the CapabilityLinter (M1.3)."""

from reins.capability import capability
from reins.linter import (
    MISSING_DESCRIPTION,
    OPAQUE_NAME,
    RAW_RESPONSE,
    TOO_MANY_PARAMS,
    CapabilityLinter,
)


def test_rejects_raw_auto_generated_capability() -> None:
    # Mirrors spec/conformance/cases/linter_rejects_raw_capability.yaml.
    report = CapabilityLinter().lint(
        name="tbl_ord_upd", description="", param_count=14, returns_raw=True
    )
    assert not report.ok
    assert set(report.violations) == {
        OPAQUE_NAME,
        MISSING_DESCRIPTION,
        TOO_MANY_PARAMS,
        RAW_RESPONSE,
    }


def test_accepts_clean_capability() -> None:
    # Mirrors spec/conformance/cases/linter_accepts_clean_capability.yaml.
    report = CapabilityLinter().lint(
        name="refund_order",
        description="Refund a customer's order by its public order number.",
        param_count=2,
        returns_raw=False,
    )
    assert report.ok
    assert report.violations == ()


def test_opaque_name_is_flagged_on_its_own() -> None:
    report = CapabilityLinter().lint(
        name="xqz", description="A perfectly adequate description.", param_count=1
    )
    assert report.violations == (OPAQUE_NAME,)


def test_too_many_params_only() -> None:
    report = CapabilityLinter().lint(
        name="update_order", description="Update an order with many fields.", param_count=12
    )
    assert report.violations == (TOO_MANY_PARAMS,)


def test_clean_multiword_names_are_not_flagged_opaque() -> None:
    # Names with real words must pass even when the first token isn't a known verb.
    lint = CapabilityLinter().lint
    for name in ("notify_customer", "quote_for_order", "export_report", "estimate_shipping"):
        report = lint(name=name, description="A clear capability description.", param_count=2)
        assert OPAQUE_NAME not in report.violations, name


def test_lint_capability_from_a_real_spec() -> None:
    @capability
    def find_orders(status: str) -> list[str]:
        "Find orders by their status value."
        return []

    assert CapabilityLinter().lint_capability(find_orders.spec).ok
