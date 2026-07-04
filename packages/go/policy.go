package reins

// The policy engine (M6.4): deterministic, code-owned governance for writes.
// Reads run freely; writes are gated by the autonomy ladder
// (ReadOnly → DraftWrites → ApprovedWrites → Trusted, invariant §2.10). Its
// constraints are enforced here, never described to the model (§2.5). The default
// never auto-executes a write, and destructive ops are never auto-executed.

type AutonomyLevel string

const (
	AutonomyReadOnly       AutonomyLevel = "read_only"
	AutonomyDraftWrites    AutonomyLevel = "draft_writes"
	AutonomyApprovedWrites AutonomyLevel = "approved_writes"
	AutonomyTrusted        AutonomyLevel = "trusted"
)

type Decision string

const (
	DecisionAllow           Decision = "allow"
	DecisionDeny            Decision = "deny"
	DecisionRequireApproval Decision = "require_approval"
)

// PolicyDecision is a verdict plus an optional machine-readable reason.
type PolicyDecision struct {
	Decision Decision
	Reason   string
}

// ProposedAction is one capability call the model wants to make, with its descriptor.
type ProposedAction struct {
	Call       ToolCall
	Capability Capability
}

// Policy governs a run's actions. CheckActions runs before execution; CheckOutput
// after (e.g. PII redaction, a later milestone).
type Policy interface {
	CheckActions(actions []ProposedAction, autonomy AutonomyLevel) PolicyDecision
	CheckOutput(output Message) PolicyDecision
}

// DefaultPolicy implements the autonomy ladder: reads free, writes gated.
type DefaultPolicy struct{}

var decisionRank = map[Decision]int{DecisionAllow: 0, DecisionRequireApproval: 1, DecisionDeny: 2}

func (DefaultPolicy) CheckActions(actions []ProposedAction, autonomy AutonomyLevel) PolicyDecision {
	verdict := PolicyDecision{Decision: DecisionAllow}
	for _, a := range actions { // the batch takes the most restrictive decision
		candidate := decideOne(a.Capability, autonomy)
		if decisionRank[candidate.Decision] > decisionRank[verdict.Decision] {
			verdict = candidate
		}
	}
	return verdict
}

func (DefaultPolicy) CheckOutput(Message) PolicyDecision {
	return PolicyDecision{Decision: DecisionAllow}
}

func decideOne(cap Capability, autonomy AutonomyLevel) PolicyDecision {
	if cap.Access == AccessRead {
		return PolicyDecision{Decision: DecisionAllow} // reads run freely (§2.2)
	}
	switch autonomy {
	case AutonomyReadOnly:
		return PolicyDecision{DecisionDeny, "write_in_read_only"}
	case AutonomyDraftWrites:
		return PolicyDecision{DecisionDeny, "draft_only"}
	case AutonomyApprovedWrites:
		return PolicyDecision{DecisionRequireApproval, "approval_required"}
	default: // Trusted: plain writes auto-allow; destructive or confirm-marked stay gated
		if cap.Access == AccessDestructive || cap.Confirm {
			return PolicyDecision{DecisionRequireApproval, "approval_required"}
		}
		return PolicyDecision{Decision: DecisionAllow}
	}
}
