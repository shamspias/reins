package reins

import "fmt"

// Row-level security (M6.4): capabilities are scoped to the caller's identity. The
// principal set at the entry point reaches every call as Context.Principal (§2.7),
// and a Scope-annotated capability refuses to run without one (fail closed, §2.8).
// The capability's own code (or the ORM adapters) then filters rows — Reins never
// guesses which rows belong to whom.

// scopeViolation returns why a call must not run, or ("", false) if scoping is
// satisfied. A Scope-annotated capability requires an authenticated principal.
func scopeViolation(cap Capability, ctx Context) (string, bool) {
	if cap.Scope == "" {
		return "", false
	}
	if ctx.Principal == "" {
		return fmt.Sprintf(
			"%q is scoped to %q but the run has no principal"+
				"\n  → set a Principal on the run (Agent option) or the request identity",
			cap.Name, cap.Scope,
		), true
	}
	return "", false
}
