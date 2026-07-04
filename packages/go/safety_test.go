package reins

import (
	"strings"
	"testing"
)

// --- policy ----------------------------------------------------------------------

func policyAction(name string, access Access, confirm bool) ProposedAction {
	return ProposedAction{
		Call:       ToolCall{ID: "c", Name: name},
		Capability: Capability{Name: name, Access: access, Confirm: confirm},
	}
}

func decide(a ProposedAction, autonomy AutonomyLevel) PolicyDecision {
	return DefaultPolicy{}.CheckActions([]ProposedAction{a}, autonomy)
}

func TestPolicyLadder(t *testing.T) {
	read := policyAction("find_x", AccessRead, false)
	write := policyAction("update_x", AccessWrite, false)
	destr := policyAction("delete_x", AccessDestructive, false)

	for _, level := range []AutonomyLevel{AutonomyReadOnly, AutonomyDraftWrites, AutonomyApprovedWrites, AutonomyTrusted} {
		if decide(read, level).Decision != DecisionAllow {
			t.Errorf("reads must be allowed at %s", level)
		}
	}
	if d := decide(write, AutonomyReadOnly); d.Decision != DecisionDeny || d.Reason != "write_in_read_only" {
		t.Errorf("read_only write = %+v", d)
	}
	if d := decide(write, AutonomyDraftWrites); d.Decision != DecisionDeny || d.Reason != "draft_only" {
		t.Errorf("draft write = %+v", d)
	}
	if decide(write, AutonomyApprovedWrites).Decision != DecisionRequireApproval {
		t.Error("approved_writes should require approval")
	}
	if decide(write, AutonomyTrusted).Decision != DecisionAllow {
		t.Error("trusted plain write should be allowed")
	}
	if decide(destr, AutonomyTrusted).Decision != DecisionRequireApproval {
		t.Error("trusted destructive should still require approval")
	}
	confirmed := policyAction("update_y", AccessWrite, true)
	if decide(confirmed, AutonomyTrusted).Decision != DecisionRequireApproval {
		t.Error("trusted confirm-marked write should require approval")
	}
}

func TestPolicyBatchTakesMostRestrictive(t *testing.T) {
	read := policyAction("find_x", AccessRead, false)
	write := policyAction("update_x", AccessWrite, false)
	d := DefaultPolicy{}.CheckActions([]ProposedAction{read, write}, AutonomyApprovedWrites)
	if d.Decision != DecisionRequireApproval {
		t.Errorf("batch verdict = %+v, want require_approval", d)
	}
}

// --- redaction -------------------------------------------------------------------

func TestRedactionMasksSensitiveKeys(t *testing.T) {
	out := RedactArguments(map[string]any{
		"password":      "hunter2",
		"API_KEY":       "sk-1",
		"order_id":      7,
		"nested":        map[string]any{"token": "abc", "name": "amy"},
		"authorization": "Bearer x",
	})
	if out["password"] != Redacted || out["API_KEY"] != Redacted || out["authorization"] != Redacted {
		t.Errorf("sensitive keys not masked: %+v", out)
	}
	if out["order_id"] != 7 {
		t.Error("non-sensitive key should pass through")
	}
	nested := out["nested"].(map[string]any)
	if nested["token"] != Redacted || nested["name"] != "amy" {
		t.Errorf("nested redaction wrong: %+v", nested)
	}
}

func TestRedactionDoesNotMutateOriginal(t *testing.T) {
	original := map[string]any{"password": "hunter2"}
	RedactArguments(original)
	if original["password"] != "hunter2" {
		t.Error("the original args must not be mutated — the capability needs the real value")
	}
}

// --- the gate through the loop ---------------------------------------------------

func updatePassword() BoundCapability {
	return Define[struct {
		UserID   int    `json:"user_id"`
		Password string `json:"password"`
	}]("update_password", "Update a user's password.", func(_ Context, _ map[string]any) (any, error) {
		return "updated", nil
	})
}

func TestApprovedWriteExecutesAndIsAudited(t *testing.T) {
	model := NewFakeModel(
		CallResponse("c0", "delete_order", map[string]any{"order_id": 1}),
		FinalResponse("deleted"),
	)
	rep, _ := RunWith(model, reg(deleteOrder()), "delete order 1", true, RunOptions{
		Approve: ApprovalFunc(func(ApprovalRequest) bool { return true }),
	})
	if rep.Outcome != "executed" {
		t.Errorf("outcome = %q, want executed", rep.Outcome)
	}
	decisions := []string{}
	for _, r := range rep.Audit {
		decisions = append(decisions, r.Decision)
	}
	if len(decisions) != 2 || decisions[0] != "approved" || decisions[1] != "executed" {
		t.Errorf("audit decisions = %v, want [approved executed]", decisions)
	}
}

func TestDeniedWriteIsNotExecutedButAudited(t *testing.T) {
	model := NewFakeModel(CallResponse("c0", "delete_order", map[string]any{"order_id": 1}))
	rep, _ := RunWith(model, reg(deleteOrder()), "delete order 1", true, RunOptions{
		Approve: ApprovalFunc(func(ApprovalRequest) bool { return false }),
	})
	if rep.Outcome != "not_executed" || rep.Reason != "approval_denied" {
		t.Errorf("got %q/%q, want not_executed/approval_denied", rep.Outcome, rep.Reason)
	}
	if !rep.Audited || len(rep.Audit) != 1 || rep.Audit[0].Decision != "approval_denied" {
		t.Errorf("denied write should be audited: %+v", rep.Audit)
	}
	if len(rep.Executed) != 0 {
		t.Errorf("nothing should have executed: %v", rep.Executed)
	}
}

func TestAuditRedactsArguments(t *testing.T) {
	model := NewFakeModel(
		CallResponse("c0", "update_password", map[string]any{"user_id": 7, "password": "hunter2"}),
		FinalResponse("done"),
	)
	rep, _ := RunWith(model, reg(updatePassword()), "reset pw", true, RunOptions{
		Approve: ApprovalFunc(func(ApprovalRequest) bool { return true }),
	})
	for _, r := range rep.Audit {
		if r.Arguments["password"] != Redacted {
			t.Errorf("password not redacted in audit: %+v", r.Arguments)
		}
	}
}

func TestTrustedPlainWriteExecutesWithoutPrompting(t *testing.T) {
	updateStatus := Define[struct {
		OrderID int `json:"order_id"`
	}]("update_status", "Update an order's status.", func(_ Context, _ map[string]any) (any, error) {
		return "ok", nil
	})
	prompted := false
	model := NewFakeModel(
		CallResponse("c0", "update_status", map[string]any{"order_id": 1}),
		FinalResponse("done"),
	)
	rep, _ := RunWith(model, reg(updateStatus), "update", true, RunOptions{
		Autonomy: AutonomyTrusted,
		Approve:  ApprovalFunc(func(ApprovalRequest) bool { prompted = true; return false }),
	})
	if rep.Outcome != "executed" || prompted {
		t.Errorf("trusted plain write should execute unprompted: outcome=%q prompted=%v", rep.Outcome, prompted)
	}
}

func TestBulkWritePromptsEvenWhenTrusted(t *testing.T) {
	updateOrders := Define[struct {
		OrderIDs []int  `json:"order_ids"`
		Status   string `json:"status"`
	}]("update_orders", "Update a batch of orders.", func(_ Context, _ map[string]any) (any, error) {
		return "ok", nil
	})
	var gotBulk *int
	model := NewFakeModel(CallResponse("c0", "update_orders", map[string]any{
		"order_ids": []any{1, 2, 3}, "status": "shipped",
	}))
	rep, _ := RunWith(model, reg(updateOrders), "ship all", true, RunOptions{
		Autonomy: AutonomyTrusted,
		Approve: ApprovalFunc(func(r ApprovalRequest) bool {
			gotBulk = r.BulkCount
			return false
		}),
	})
	if rep.Outcome != "not_executed" {
		t.Errorf("bulk write should be gated: %q", rep.Outcome)
	}
	if gotBulk == nil || *gotBulk != 3 {
		t.Errorf("bulk count = %v, want 3", gotBulk)
	}
}

func TestDefaultGateFailsClosed(t *testing.T) {
	// No approve handler ⇒ deny-all ⇒ a write is never executed.
	model := NewFakeModel(CallResponse("c0", "delete_order", map[string]any{"order_id": 1}))
	rep, _ := RunWith(model, reg(deleteOrder()), "delete", true, RunOptions{})
	if rep.Outcome != "not_executed" {
		t.Errorf("default gate should fail closed: %q", rep.Outcome)
	}
}

func TestBrokenAuditSinkBlocksTheWrite(t *testing.T) {
	model := NewFakeModel(
		CallResponse("c0", "update_status", map[string]any{"order_id": 1}),
		FinalResponse("gave up"),
	)
	updateStatus := Define[struct {
		OrderID int `json:"order_id"`
	}]("update_status", "Update status.", func(_ Context, _ map[string]any) (any, error) {
		t.Fatal("handler must not run when audit fails")
		return nil, nil
	})
	rep, _ := RunWith(model, reg(updateStatus), "update", true, RunOptions{
		Autonomy: AutonomyTrusted,
		Approve:  ApprovalFunc(func(ApprovalRequest) bool { return true }),
		Audit:    brokenSink{},
	})
	if len(rep.Executed) != 0 {
		t.Errorf("no audit, no action: nothing should execute, got %v", rep.Executed)
	}
}

type brokenSink struct{}

func (brokenSink) Record(AuditRecord) error { return NewError("disk full", "fix the sink") }

// --- identity + RLS --------------------------------------------------------------

func TestPrincipalReachesTheCapability(t *testing.T) {
	var seen Context
	whoami := Define[struct{}]("list_my_things", "List the caller's things.", func(ctx Context, _ map[string]any) (any, error) {
		seen = ctx
		return []string{}, nil
	}, Reads())
	model := NewFakeModel(CallResponse("c0", "list_my_things", nil), FinalResponse("none"))
	RunWith(model, reg(whoami), "mine", false, RunOptions{Principal: "user:alice"})
	if seen.Principal != "user:alice" {
		t.Errorf("principal not propagated: %+v", seen)
	}
}

func TestScopedCapabilityNeedsAPrincipal(t *testing.T) {
	ran := false
	notes := Define[struct{}]("list_my_notes", "List the caller's notes.", func(_ Context, _ map[string]any) (any, error) {
		ran = true
		return nil, nil
	}, Reads(), Scope("user"))
	model := NewFakeModel(CallResponse("c0", "list_my_notes", nil), FinalResponse("no access"))
	rep, _ := RunWith(model, reg(notes), "my notes", false, RunOptions{}) // no principal
	if ran {
		t.Error("a scoped capability must not run without a principal (fail closed)")
	}
	if len(rep.Executed) != 0 {
		t.Errorf("scope-refused call should not count as executed: %v", rep.Executed)
	}
}

func TestRLSCrossUserDataUnreachable(t *testing.T) {
	notes := map[string][]string{"user:alice": {"alice-plan"}, "user:bob": {"bob-secret"}}
	listNotes := Define[struct{}]("list_my_notes", "List the caller's notes.", func(ctx Context, _ map[string]any) (any, error) {
		return notes[ctx.Principal], nil
	}, Reads(), Scope("user"))
	model := NewFakeModel(CallResponse("c0", "list_my_notes", nil), FinalResponse("1 note"))
	RunWith(model, reg(listNotes), "my notes", false, RunOptions{Principal: "user:alice"})
	shown := ""
	for _, call := range model.Calls {
		for _, m := range call.Messages {
			shown += m.Text
		}
	}
	if !strings.Contains(shown, "alice-plan") || strings.Contains(shown, "bob-secret") {
		t.Errorf("RLS leaked cross-user data: %q", shown)
	}
}
