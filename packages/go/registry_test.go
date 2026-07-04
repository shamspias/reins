package reins

import (
	"strings"
	"testing"
)

func TestCallSuccessReturnsData(t *testing.T) {
	c := Define[struct {
		Status string `json:"status"`
	}]("find_orders", "Find orders by status.", func(_ Context, args map[string]any) (any, error) {
		return "orders:" + args["status"].(string), nil
	}, Reads())
	r := reg(c)
	res := r.Call("find_orders", map[string]any{"status": "open"}, Context{})
	if !res.OK || res.Value != "orders:open" {
		t.Errorf("call result = %+v", res)
	}
}

func TestCallUnknownCapabilitySuggestsClosest(t *testing.T) {
	r := reg(findOrders())
	res := r.Call("find_ordes", nil, Context{}) // typo
	if res.OK {
		t.Fatal("unknown capability should not succeed")
	}
	if !strings.Contains(res.Error, "→") || !strings.Contains(res.Error, "find_orders") {
		t.Errorf("error should suggest the closest match: %q", res.Error)
	}
}

func TestCallMissingRequiredArgIsData(t *testing.T) {
	c := Define[struct {
		OrderID int `json:"order_id"`
	}]("get_order", "Get an order by id.", func(_ Context, _ map[string]any) (any, error) {
		return nil, nil
	}, Reads())
	res := reg(c).Call("get_order", map[string]any{}, Context{})
	if res.OK || !strings.Contains(res.Error, "order_id") {
		t.Errorf("missing required arg should be hinted data: %+v", res)
	}
}

func TestCallWrongTypeIsData(t *testing.T) {
	c := Define[struct {
		OrderID int `json:"order_id"`
	}]("get_order", "Get an order by id.", func(_ Context, _ map[string]any) (any, error) {
		return nil, nil
	}, Reads())
	res := reg(c).Call("get_order", map[string]any{"order_id": "not-an-int"}, Context{})
	if res.OK || !strings.Contains(res.Error, "integer") {
		t.Errorf("wrong type should be rejected as data: %+v", res)
	}
}

func TestCallRejectsUnexpectedArg(t *testing.T) {
	// Fail-closed: additionalProperties is false, so extras are rejected (§2.6).
	res := reg(findOrders()).Call("find_orders", map[string]any{"bogus": 1}, Context{})
	if res.OK || !strings.Contains(res.Error, "unexpected") {
		t.Errorf("unexpected arg should be rejected: %+v", res)
	}
}

func TestCallHandlerErrorIsData(t *testing.T) {
	c := Define[struct{}]("do_thing", "Do a thing.", func(_ Context, _ map[string]any) (any, error) {
		return nil, NewError("boom", "try again")
	})
	res := reg(c).Call("do_thing", nil, Context{})
	if res.OK || !strings.Contains(res.Error, "boom") {
		t.Errorf("handler error should surface as data: %+v", res)
	}
}

func TestCallInjectsContext(t *testing.T) {
	var seen Context
	c := Define[struct{}]("whoami", "Report the caller.", func(ctx Context, _ map[string]any) (any, error) {
		seen = ctx
		return ctx.Principal, nil
	}, Reads())
	res := reg(c).Call("whoami", nil, Context{Principal: "user:7"})
	if !res.OK || res.Value != "user:7" || seen.Principal != "user:7" {
		t.Errorf("context not injected: res=%+v seen=%+v", res, seen)
	}
}

func TestListAndToolSpecs(t *testing.T) {
	r := reg(findOrders(), deleteOrder())
	if len(r.List()) != 2 {
		t.Errorf("expected 2 specs, got %d", len(r.List()))
	}
	specs := r.ToolSpecs()
	if len(specs) != 2 || specs[0].Name != "find_orders" {
		t.Errorf("tool specs wrong: %+v", specs)
	}
}
