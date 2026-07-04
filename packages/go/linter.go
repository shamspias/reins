package reins

import (
	"regexp"
	"sort"
	"strings"
)

// The Capability Linter (invariant §2.15): a build-time check that a capability is
// one a model can actually use — intent-level name, a real description, lean
// parameters, trimmed responses. Auto-generated capabilities (the ORM adapters)
// must pass it before exposure. The violation codes are language-neutral
// (spec/contract.md §3) and asserted by the shared conformance suite.

const (
	ViolationOpaqueName       = "opaque_name"
	ViolationMissingDesc      = "missing_description"
	ViolationTooManyParams    = "too_many_params"
	ViolationRawResponse      = "raw_response"
	ViolationAmbiguousOverlap = "ambiguous_overlap"
	maxParams                 = 7
	minDescriptionChars       = 10
)

var snakeName = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

// LintReport is the result of linting one capability: the violation codes it
// triggered (sorted, so equality is order-independent).
type LintReport struct {
	Violations []string
}

// OK reports whether the capability passed (no violations).
func (r LintReport) OK() bool { return len(r.Violations) == 0 }

// Lint checks a capability's agent-facing surface against the §2.15 quality bar.
func Lint(name, description string, paramCount int, returnsRaw bool) LintReport {
	var found []string
	if isOpaque(name) {
		found = append(found, ViolationOpaqueName)
	}
	if len(strings.TrimSpace(description)) < minDescriptionChars {
		found = append(found, ViolationMissingDesc)
	}
	if paramCount > maxParams {
		found = append(found, ViolationTooManyParams)
	}
	if returnsRaw {
		found = append(found, ViolationRawResponse)
	}
	sort.Strings(found)
	return LintReport{Violations: found}
}

// LintCapability lints a Capability's spec (its schema's property count).
func LintCapability(c Capability, returnsRaw bool) LintReport {
	count := 0
	if props, ok := c.InputSchema["properties"].(map[string]any); ok {
		count = len(props)
	}
	return Lint(c.Name, c.Description, count, returnsRaw)
}

func isOpaque(name string) bool {
	// Opaque = not clean lowercase snake_case, or built only from short
	// abbreviation-like tokens with no real word (tbl_ord_upd is opaque;
	// refund_order / get_user are fine).
	if !snakeName.MatchString(name) {
		return true
	}
	longest := 0
	for _, token := range strings.Split(name, "_") {
		if len(token) > longest {
			longest = len(token)
		}
	}
	return longest < 4
}
