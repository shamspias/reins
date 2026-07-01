"""The Capability Linter (M1.3): a build-time check that a capability is one a model
can actually use (invariant §2.15).

It encodes the agent-experience principles — intent-level name, a real description,
lean parameters, trimmed responses — as the violation codes the cross-language
conformance suite asserts (spec/contract.md §3). Auto-generated capabilities (M4) must
pass it before they are exposed to the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from reins.types import Capability

OPAQUE_NAME = "opaque_name"
MISSING_DESCRIPTION = "missing_description"
TOO_MANY_PARAMS = "too_many_params"
RAW_RESPONSE = "raw_response"
AMBIGUOUS_OVERLAP = "ambiguous_overlap"  # emitted at registry scope; see contract.md §3

MAX_PARAMS = 7
MIN_DESCRIPTION_CHARS = 10
_SNAKE_NAME = re.compile(r"[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class LintReport:
    """The result of linting one capability: the violation codes it triggered."""

    violations: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


class CapabilityLinter:
    """Lints a capability's agent-facing surface against the §2.15 quality bar."""

    def lint(
        self,
        *,
        name: str,
        description: str,
        param_count: int,
        returns_raw: bool = False,
    ) -> LintReport:
        found: list[str] = []
        if _is_opaque(name):
            found.append(OPAQUE_NAME)
        if len(description.strip()) < MIN_DESCRIPTION_CHARS:
            found.append(MISSING_DESCRIPTION)
        if param_count > MAX_PARAMS:
            found.append(TOO_MANY_PARAMS)
        if returns_raw:
            found.append(RAW_RESPONSE)
        return LintReport(violations=tuple(sorted(found)))

    def lint_capability(self, cap: Capability, *, returns_raw: bool = False) -> LintReport:
        properties = cap.input_schema.get("properties", {})
        return self.lint(
            name=cap.name,
            description=cap.description,
            param_count=len(properties),
            returns_raw=returns_raw,
        )


def _is_opaque(name: str) -> bool:
    # Opaque = not a clean lowercase snake_case name, or built only from short
    # abbreviation-like tokens with no real word: `tbl_ord_upd` is opaque, while
    # `refund_order` / `notify_customer` are fine. (Verb-first is encouraged by the
    # classifier, but not required here — that over-flagged legitimate names.)
    if not _SNAKE_NAME.match(name):
        return True
    return max(len(token) for token in name.split("_")) < 4
