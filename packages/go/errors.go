package reins

// Error is a control-plane failure carrying a human Hint — the "→ next step"
// invariant §2.19 requires. Capability-level failures are data (CapabilityResult);
// only control-plane failures return an *Error across the loop boundary.
type Error struct {
	Message string
	Hint    string
}

func (e *Error) Error() string {
	return e.Message + "\n  → " + e.Hint
}

// NewError builds a hinted control-plane error.
func NewError(message, hint string) *Error {
	return &Error{Message: message, Hint: hint}
}
