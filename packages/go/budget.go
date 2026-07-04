package reins

// Budget caps a run so it stops deterministically (invariant §2.13) — a runaway
// loop is a bug, not an edge case. M6.3 mirrors the Python M1.4 caps (turns +
// tokens); cost and time join with the rest of the token engine.
type Budget struct {
	// MaxTurns bounds the number of model calls, INCLUDING the final-answer turn:
	// a goal needing k rounds of tool calls plus an answer needs MaxTurns >= k+1.
	MaxTurns int
	// MaxTokens bounds total token spend (0 = uncapped).
	MaxTokens int
}

// DefaultBudget is the out-of-the-box cap (8 turns, tokens uncapped).
func DefaultBudget() Budget {
	return Budget{MaxTurns: 8}
}

// OverTokens reports whether cumulative usage has exceeded the token cap.
func (b Budget) OverTokens(u Usage) bool {
	if b.MaxTokens == 0 {
		return false
	}
	return u.InputTokens+u.OutputTokens > b.MaxTokens
}
