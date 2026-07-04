package reins

import (
	"strings"
	"testing"
)

// --- test fixtures ---------------------------------------------------------------

func findOrders() BoundCapability {
	return Define[struct {
		Status string `json:"status,omitempty"`
	}]("find_orders", "Find orders by status.", func(_ Context, _ map[string]any) (any, error) {
		return []string{"o1", "o2"}, nil
	}, Reads())
}

func deleteOrder() BoundCapability {
	return Define[struct {
		OrderID int `json:"order_id"`
	}]("delete_order", "Delete an order.", func(_ Context, _ map[string]any) (any, error) {
		return nil, nil
	})
}

func reg(caps ...BoundCapability) *CapabilityRegistry {
	r := NewRegistry()
	for _, c := range caps {
		if err := r.Register(c); err != nil {
			panic(err)
		}
	}
	return r
}

// --- the loop --------------------------------------------------------------------

func TestReadGoalCompletes(t *testing.T) {
	model := NewFakeModel(
		CallResponse("c0", "find_orders", map[string]any{"status": "open"}),
		FinalResponse("found 2"),
	)
	rep, err := Run(model, reg(findOrders()), "list open orders", false, DefaultBudget())
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if rep.Outcome != "completed" {
		t.Errorf("outcome = %q, want completed", rep.Outcome)
	}
	if len(rep.Executed) != 1 || rep.Executed[0] != "find_orders" {
		t.Errorf("executed = %v", rep.Executed)
	}
}

func TestAskRefusesAWrite(t *testing.T) {
	model := NewFakeModel(CallResponse("c0", "delete_order", map[string]any{"order_id": 1}))
	rep, _ := Run(model, reg(findOrders(), deleteOrder()), "delete order 1", false, DefaultBudget())
	if rep.Outcome != "refused" || rep.Reason != "write_in_read_only" {
		t.Errorf("got outcome=%q reason=%q, want refused/write_in_read_only", rep.Outcome, rep.Reason)
	}
	if len(rep.Executed) != 0 {
		t.Errorf("nothing should have executed, got %v", rep.Executed)
	}
}

func TestAmbiguousVerbTreatedAsWriteUnderAsk(t *testing.T) {
	// process_order classifies write (ambiguous), so Ask refuses it (§2.4 + §2.1).
	processOrder := Define[struct {
		OrderID int `json:"order_id"`
	}]("process_order", "Process an order.", func(_ Context, _ map[string]any) (any, error) {
		return nil, nil
	})
	model := NewFakeModel(CallResponse("c0", "process_order", map[string]any{"order_id": 8842}))
	rep, _ := Run(model, reg(processOrder), "process order 8842", false, DefaultBudget())
	if rep.Outcome != "refused" || rep.Reason != "write_in_read_only" {
		t.Errorf("ambiguous verb under Ask should be refused, got %q/%q", rep.Outcome, rep.Reason)
	}
}

func TestRunWriteNotExecutedWithoutGate(t *testing.T) {
	// Write gating (policy/approval) is M6.4; until then Run never auto-executes.
	model := NewFakeModel(CallResponse("c0", "delete_order", map[string]any{"order_id": 1}))
	rep, _ := Run(model, reg(deleteOrder()), "delete order 1", true, DefaultBudget())
	if rep.Outcome != "not_executed" {
		t.Errorf("outcome = %q, want not_executed", rep.Outcome)
	}
}

func TestBudgetStopsALoopingModel(t *testing.T) {
	model := NewFakeModel(
		CallResponse("c0", "find_orders", nil),
		CallResponse("c1", "find_orders", nil),
		CallResponse("c2", "find_orders", nil),
	)
	rep, _ := Run(model, reg(findOrders()), "loop forever", false, Budget{MaxTurns: 2})
	if rep.Outcome != "stopped" || rep.Reason != "budget_exhausted" {
		t.Errorf("got %q/%q, want stopped/budget_exhausted", rep.Outcome, rep.Reason)
	}
}

func TestAgentAskReturnsRunResult(t *testing.T) {
	model := NewFakeModel(FinalResponse("hello"))
	agent, err := NewAgent(model, findOrders())
	if err != nil {
		t.Fatalf("new agent: %v", err)
	}
	result, err := agent.Ask("say hi")
	if err != nil {
		t.Fatalf("ask: %v", err)
	}
	if result.Output.Text != "hello" {
		t.Errorf("output = %q, want hello", result.Output.Text)
	}
}

func TestNewAgentRejectsDuplicateCapability(t *testing.T) {
	_, err := NewAgent(NewFakeModel(), findOrders(), findOrders())
	if err == nil || !strings.Contains(err.Error(), "→") {
		t.Errorf("duplicate registration should be a hinted error, got %v", err)
	}
}

func TestModelErrorPropagates(t *testing.T) {
	// An exhausted FakeModel (control-plane failure) surfaces as an error, not data.
	model := NewFakeModel() // no responses
	_, err := Run(model, reg(findOrders()), "x", false, DefaultBudget())
	if err == nil {
		t.Error("expected a control-plane error from an exhausted model")
	}
}

func TestStopConditions(t *testing.T) {
	resp := FinalResponse("done")
	if _, stop := (GoalReached{}).Evaluate(RunState{LastResponse: &resp, Turn: 1}); !stop {
		t.Error("GoalReached should stop on a final answer")
	}
	if _, stop := (MaxTurns{Limit: 3}).Evaluate(RunState{Turn: 3}); !stop {
		t.Error("MaxTurns should stop at the limit")
	}
	if _, stop := (MaxTurns{Limit: 4}).Evaluate(RunState{Turn: 3}); stop {
		t.Error("MaxTurns should not stop below the limit")
	}
}
