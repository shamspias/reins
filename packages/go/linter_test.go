package reins

import (
	"reflect"
	"sort"
	"testing"
)

func TestLinterRejectsARawCapability(t *testing.T) {
	// The canonical §3 case: opaque name, empty description, too many params,
	// raw response — all four violations at once.
	report := Lint("tbl_ord_upd", "", 14, true)
	want := []string{
		ViolationMissingDesc, ViolationOpaqueName, ViolationRawResponse, ViolationTooManyParams,
	}
	sort.Strings(want)
	if !reflect.DeepEqual(report.Violations, want) {
		t.Errorf("violations = %v, want %v", report.Violations, want)
	}
	if report.OK() {
		t.Error("a raw capability must not pass")
	}
}

func TestLinterAcceptsACleanCapability(t *testing.T) {
	report := Lint("refund_order", "Refund a customer's order by its number.", 2, false)
	if !report.OK() {
		t.Errorf("clean capability should pass, got %v", report.Violations)
	}
}

func TestOpaqueNames(t *testing.T) {
	opaque := []string{"tbl_ord_upd", "X", "get user", "Get_User", "a_b_c", "123"}
	for _, name := range opaque {
		if !isOpaque(name) {
			t.Errorf("%q should be opaque", name)
		}
	}
	fine := []string{"refund_order", "get_user", "find_orders", "notify_customer", "list"}
	for _, name := range fine {
		if isOpaque(name) {
			t.Errorf("%q should be fine", name)
		}
	}
}

func TestLintCapabilityCountsSchemaProperties(t *testing.T) {
	type Wide struct {
		A, B, C, D, E, F, G, H int // 8 fields > maxParams (7)
	}
	c := Define[Wide]("update_widget", "Update a widget.", noopHandler)
	if LintCapability(c.Spec, false).OK() {
		t.Error("8 params should trip too_many_params")
	}
	type Lean struct {
		ID int `json:"id"`
	}
	d := Define[Lean]("get_widget", "Get a widget by id.", noopHandler, Reads())
	if !LintCapability(d.Spec, false).OK() {
		t.Errorf("lean capability should pass: %v", LintCapability(d.Spec, false).Violations)
	}
}
