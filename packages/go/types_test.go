package reins

import (
	"encoding/json"
	"testing"
)

func TestEnumValuesMatchTheContract(t *testing.T) {
	// The wire values are language-neutral (spec/contract.md) — they must match
	// the Python port and the conformance cases exactly.
	cases := map[string]string{
		string(RoleSystem):        "system",
		string(RoleTool):          "tool",
		string(FinishStop):        "stop",
		string(FinishToolCalls):   "tool_calls",
		string(AccessRead):        "read",
		string(AccessWrite):       "write",
		string(AccessDestructive): "destructive",
	}
	for got, want := range cases {
		if got != want {
			t.Errorf("enum value = %q, want %q", got, want)
		}
	}
}

func TestMessageJSONRoundTrips(t *testing.T) {
	msg := Message{
		Role:      RoleAssistant,
		Text:      "here you go",
		ToolCalls: []ToolCall{{ID: "c1", Name: "find_orders", Arguments: map[string]any{"status": "open"}}},
	}
	data, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var back Message
	if err := json.Unmarshal(data, &back); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if back.Role != RoleAssistant || back.Text != "here you go" {
		t.Errorf("round-trip lost fields: %+v", back)
	}
	if len(back.ToolCalls) != 1 || back.ToolCalls[0].Name != "find_orders" {
		t.Errorf("round-trip lost tool call: %+v", back.ToolCalls)
	}
	if back.ToolCalls[0].Arguments["status"] != "open" {
		t.Errorf("round-trip lost args: %+v", back.ToolCalls[0].Arguments)
	}
}

func TestCapabilityToolSpecDropsPolicyMetadata(t *testing.T) {
	// Invariant §2.5: only name / description / input schema cross to the model.
	cap := Capability{
		Name:        "delete_order",
		Description: "Delete an order.",
		InputSchema: map[string]any{"type": "object"},
		Access:      AccessDestructive,
		Confirm:     true,
		Scope:       "user",
	}
	spec := cap.ToolSpec()
	if spec.Name != "delete_order" || spec.Description != "Delete an order." {
		t.Errorf("tool spec lost name/description: %+v", spec)
	}
	// A ToolSpec has no Access/Confirm/Scope fields at all — enforced by the type.
	data, err := json.Marshal(spec)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	for _, leaked := range []string{"access", "confirm", "scope", "destructive"} {
		if containsField(data, leaked) {
			t.Errorf("tool spec leaked policy field %q: %s", leaked, data)
		}
	}
}

func TestContextJSONRoundTrips(t *testing.T) {
	ctx := Context{Principal: "user:42", TraceID: "abc"}
	data, _ := json.Marshal(ctx)
	var back Context
	if err := json.Unmarshal(data, &back); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if back != ctx {
		t.Errorf("round-trip changed context: %+v", back)
	}
}

func containsField(jsonData []byte, field string) bool {
	var m map[string]any
	if err := json.Unmarshal(jsonData, &m); err != nil {
		return false
	}
	_, ok := m[field]
	return ok
}
