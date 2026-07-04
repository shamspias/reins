package reins

import "testing"

func TestClassifyByVerb(t *testing.T) {
	cases := map[string]Access{
		"find_orders":       AccessRead,
		"get_user":          AccessRead,
		"count_orders":      AccessRead,
		"create_user":       AccessWrite,
		"update_order":      AccessWrite,
		"delete_order":      AccessDestructive,
		"refund_payment":    AccessDestructive,
		"cancel_order":      AccessDestructive,
		"process_order":     AccessWrite, // ambiguous verb ⇒ write (§2.4)
		"frobnicate_widget": AccessWrite,
	}
	for name, want := range cases {
		if got := Classify(name); got != want {
			t.Errorf("Classify(%q) = %q, want %q", name, got, want)
		}
	}
}

func TestClassifyOverridesBeatTheHeuristic(t *testing.T) {
	// export_report would be write (ambiguous), but Reads() forces read.
	c := Define[struct{}]("export_report", "Export the monthly report.", noopHandler, Reads())
	if c.Spec.Access != AccessRead {
		t.Errorf("Reads() override failed: %q", c.Spec.Access)
	}
	// A named write forced destructive.
	d := Define[struct{}]("update_thing", "Update a thing.", noopHandler, Destructive())
	if d.Spec.Access != AccessDestructive {
		t.Errorf("Destructive() override failed: %q", d.Spec.Access)
	}
}

func TestContradictoryOverridesPanic(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Error("expected a panic for Reads() + Destructive()")
		}
	}()
	Define[struct{}]("x_thing", "A thing.", noopHandler, Reads(), Destructive())
}

func TestSchemaFromStruct(t *testing.T) {
	type Params struct {
		OrderID  int      `json:"order_id"`
		Status   string   `json:"status,omitempty"` // optional
		Tags     []string `json:"tags"`
		Note     *string  `json:"note"` // pointer ⇒ optional
		Internal string   `json:"-"`    // excluded
		private  string   //nolint  // unexported ⇒ excluded
	}
	c := Define[Params]("update_order", "Update an order.", noopHandler)
	props, _ := c.Spec.InputSchema["properties"].(map[string]any)
	if _, ok := props["order_id"]; !ok {
		t.Errorf("order_id missing from schema: %+v", props)
	}
	if _, ok := props["internal"]; ok {
		t.Error("json:\"-\" field should be excluded")
	}
	if _, ok := props["private"]; ok {
		t.Error("unexported field should be excluded")
	}
	if got := props["order_id"].(map[string]any)["type"]; got != "integer" {
		t.Errorf("order_id type = %v, want integer", got)
	}
	if got := props["tags"].(map[string]any)["type"]; got != "array" {
		t.Errorf("tags type = %v, want array", got)
	}
	required := toStringSet(c.Spec.InputSchema["required"])
	if !required["order_id"] || !required["tags"] {
		t.Errorf("order_id and tags should be required: %v", c.Spec.InputSchema["required"])
	}
	if required["status"] {
		t.Error("status has omitempty ⇒ should be optional")
	}
	if required["note"] {
		t.Error("note is a pointer ⇒ should be optional")
	}
	if c.Spec.InputSchema["additionalProperties"] != false {
		t.Error("schema should reject additional properties (fail-closed, §2.6)")
	}
}

func TestScopeAndConfirmOptions(t *testing.T) {
	c := Define[struct{}]("delete_note", "Delete a note.", noopHandler, Confirm(), Scope("user"))
	if !c.Spec.Confirm || c.Spec.Scope != "user" {
		t.Errorf("options not applied: %+v", c.Spec)
	}
	if c.Spec.Access != AccessDestructive {
		t.Errorf("delete_ should classify destructive: %q", c.Spec.Access)
	}
}

func noopHandler(_ Context, _ map[string]any) (any, error) { return nil, nil }

func toStringSet(v any) map[string]bool {
	out := map[string]bool{}
	if items, ok := v.([]string); ok {
		for _, s := range items {
			out[s] = true
		}
	}
	return out
}
