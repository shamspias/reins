package reins

// StopCondition decides whether a run should stop, given its evolving state. The
// loop owns stopping deterministically; conditions are pure and testable.
type StopCondition interface {
	// Evaluate returns a stop reason and true if the run should stop now.
	Evaluate(state RunState) (reason string, stop bool)
}

// GoalReached stops when the model produced a final answer (finish reason Stop).
type GoalReached struct{}

func (GoalReached) Evaluate(state RunState) (string, bool) {
	if state.LastResponse != nil && state.LastResponse.FinishReason == FinishStop {
		return "goal_reached", true
	}
	return "", false
}

// MaxTurns stops once the turn count reaches the cap.
type MaxTurns struct{ Limit int }

func (m MaxTurns) Evaluate(state RunState) (string, bool) {
	if state.Turn >= m.Limit {
		return "max_turns", true
	}
	return "", false
}

// firstStop returns the reason of the first condition that fires, or "" and false.
func firstStop(conditions []StopCondition, state RunState) (string, bool) {
	for _, c := range conditions {
		if reason, stop := c.Evaluate(state); stop {
			return reason, true
		}
	}
	return "", false
}
