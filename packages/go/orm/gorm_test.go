package orm

import (
	"testing"

	"github.com/glebarez/sqlite"
	reins "github.com/shamspias/reins/packages/go"
	"gorm.io/gorm"
)

type Customer struct {
	ID    uint   `gorm:"primaryKey"`
	Email string `gorm:"unique"`
	Name  string
	Tier  string `gorm:"default:free"`
}

type Order struct {
	ID     uint   `gorm:"primaryKey"`
	Ref    string `gorm:"unique"`
	Status string
	Total  int
}

func newDB(t *testing.T) *gorm.DB {
	db, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	if err := db.AutoMigrate(&Customer{}, &Order{}); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	db.Create(&Customer{Email: "a@x.com", Name: "Ada", Tier: "pro"})
	db.Create(&Customer{Email: "b@x.com", Name: "Ben", Tier: "free"})
	db.Create(&Order{Ref: "ORD-1", Status: "open", Total: 120})
	db.Create(&Order{Ref: "ORD-2", Status: "open", Total: 40})
	db.Create(&Order{Ref: "ORD-3", Status: "closed", Total: 75})
	return db
}

func names(caps []reins.BoundCapability) map[string]reins.Capability {
	out := map[string]reins.Capability{}
	for _, c := range caps {
		out[c.Spec.Name] = c.Spec
	}
	return out
}

func registry(t *testing.T, caps []reins.BoundCapability) *reins.CapabilityRegistry {
	reg := reins.NewRegistry()
	for _, c := range caps {
		if err := reg.Register(c); err != nil {
			t.Fatalf("register %s: %v", c.Spec.Name, err)
		}
	}
	return reg
}

func TestGeneratesIntentLevelReads(t *testing.T) {
	db := newDB(t)
	caps, _, err := FromORM(db, []any{&Customer{}, &Order{}}, false, 0)
	if err != nil {
		t.Fatalf("from orm: %v", err)
	}
	got := names(caps)
	for _, want := range []string{"find_customers", "count_customers", "get_customer_by_email", "find_orders", "get_order_by_ref"} {
		if _, ok := got[want]; !ok {
			t.Errorf("missing generated capability %q (have %v)", want, keys(got))
		}
	}
}

func TestNoWritesWithoutCanWrite(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}}, false, 0)
	for name, spec := range names(caps) {
		if spec.Access != reins.AccessRead {
			t.Errorf("%q should be read-only without canWrite, got %q", name, spec.Access)
		}
	}
}

func TestWritesAppearWithCanWrite(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}}, true, 0)
	got := names(caps)
	if got["create_customer"].Access != reins.AccessWrite {
		t.Errorf("create_customer should be a write")
	}
	if got["delete_customer"].Access != reins.AccessDestructive {
		t.Errorf("delete_customer should be destructive")
	}
}

func TestEveryGeneratedCapabilityPassesLinter(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}, &Order{}}, true, 0)
	if len(caps) == 0 {
		t.Fatal("no capabilities generated")
	}
	for _, c := range caps {
		if !reins.LintCapability(c.Spec, false).OK() {
			t.Errorf("%q failed the linter: %v", c.Spec.Name, reins.LintCapability(c.Spec, false).Violations)
		}
	}
}

func TestNaturalKeyPreferredOverID(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}}, false, 0)
	got := names(caps)
	if _, ok := got["get_customer_by_email"]; !ok {
		t.Error("email (unique) should be the lookup key")
	}
	if _, ok := got["get_customer_by_id"]; ok {
		t.Error("id should not be the lookup key when a natural key exists")
	}
}

func TestCreateOmitsAutoincrementPK(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}}, true, 0)
	props := names(caps)["create_customer"].InputSchema["properties"].(map[string]any)
	if _, ok := props["id"]; ok {
		t.Error("autoincrement id must not be a create parameter")
	}
	if _, ok := props["email"]; !ok {
		t.Error("email should be a create parameter")
	}
}

func TestSchemaIndexDescribesTables(t *testing.T) {
	_, index, _ := FromORM(newDB(t), []any{&Customer{}, &Order{}}, false, 0)
	tables := map[string]reins.TableInfo{}
	for _, tbl := range index.Tables {
		tables[tbl.Name] = tbl
	}
	if _, ok := tables["customers"]; !ok {
		t.Errorf("expected a customers table, got %v", index.Tables)
	}
	if _, ok := tables["orders"]; !ok {
		t.Errorf("expected an orders table")
	}
}

// --- functional round-trips through the registry --------------------------------

func TestFindAndCount(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Order{}}, false, 0)
	reg := registry(t, caps)
	open := reg.Call("count_orders", map[string]any{"status": "open"}, reins.Context{})
	if !open.OK || toInt(open.Value) != 2 {
		t.Errorf("count open = %+v, want 2", open)
	}
	all := reg.Call("count_orders", map[string]any{}, reins.Context{})
	if !all.OK || toInt(all.Value) != 3 {
		t.Errorf("count all = %+v, want 3", all)
	}
	rows := reg.Call("find_orders", map[string]any{"status": "open"}, reins.Context{})
	list, _ := rows.Value.([]map[string]any)
	if !rows.OK || len(list) != 2 {
		t.Errorf("find open = %+v", rows)
	}
}

func TestGetByNaturalKey(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}}, false, 0)
	reg := registry(t, caps)
	hit := reg.Call("get_customer_by_email", map[string]any{"email": "b@x.com"}, reins.Context{})
	row, _ := hit.Value.(map[string]any)
	if !hit.OK || row["name"] != "Ben" {
		t.Errorf("get b@x.com = %+v", hit)
	}
	miss := reg.Call("get_customer_by_email", map[string]any{"email": "nobody@x.com"}, reins.Context{})
	if !miss.OK || miss.Value != nil {
		t.Errorf("miss should be ok with nil value, got %+v", miss)
	}
}

func TestCreateUpdateDeleteRoundTrip(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}}, true, 0)
	reg := registry(t, caps)

	created := reg.Call("create_customer", map[string]any{"email": "c@x.com", "name": "Cid"}, reins.Context{})
	if !created.OK {
		t.Fatalf("create failed: %+v", created)
	}
	updated := reg.Call("update_customer", map[string]any{"email": "c@x.com", "name": "Caroline"}, reins.Context{})
	row, _ := updated.Value.(map[string]any)
	if !updated.OK || row["name"] != "Caroline" {
		t.Errorf("update = %+v", updated)
	}
	deleted := reg.Call("delete_customer", map[string]any{"email": "c@x.com"}, reins.Context{})
	if !deleted.OK {
		t.Errorf("delete failed: %+v", deleted)
	}
	after := reg.Call("count_customers", map[string]any{"email": "c@x.com"}, reins.Context{})
	if toInt(after.Value) != 0 {
		t.Errorf("row should be gone, count = %+v", after)
	}
}

func TestUpdateMissingRowIsErrorAsData(t *testing.T) {
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}}, true, 0)
	reg := registry(t, caps)
	res := reg.Call("update_customer", map[string]any{"email": "ghost@x.com", "name": "X"}, reins.Context{})
	if res.OK || res.Error == "" {
		t.Errorf("updating a missing row should be a hinted error, got %+v", res)
	}
}

// --- end-to-end through the Agent ------------------------------------------------

func TestFromORMEndToEndRead(t *testing.T) {
	caps, index, _ := FromORM(newDB(t), []any{&Order{}}, false, 0)
	model := reins.NewFakeModel(
		reins.CallResponse("c0", "count_orders", map[string]any{"status": "open"}),
		reins.FinalResponse("2 open orders."),
	)
	opts := append([]reins.AgentOption{reins.WithCapabilities(caps...)}, reins.WithSchema(index))
	agent, err := reins.NewAgent(model, opts...)
	if err != nil {
		t.Fatalf("agent: %v", err)
	}
	result, err := agent.Ask("how many open orders?")
	if err != nil {
		t.Fatalf("ask: %v", err)
	}
	if result.Output.Text != "2 open orders." {
		t.Errorf("answer = %q", result.Output.Text)
	}
}

func TestFromORMWriteIsGated(t *testing.T) {
	prompted := false
	caps, _, _ := FromORM(newDB(t), []any{&Customer{}}, true, 0)
	model := reins.NewFakeModel(
		reins.CallResponse("c0", "create_customer", map[string]any{"email": "dave@x.com", "name": "Dave"}),
		reins.FinalResponse("created Dave"),
	)
	agent, _ := reins.NewAgent(model,
		reins.WithCapabilities(caps...),
		reins.WithApproveFunc(func(reins.ApprovalRequest) bool { prompted = true; return true }),
	)
	result, err := agent.Run("add customer Dave")
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !prompted {
		t.Error("the write should have paused for approval")
	}
	if result.Output.Text != "created Dave" {
		t.Errorf("answer = %q", result.Output.Text)
	}
}

func keys(m map[string]reins.Capability) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func toInt(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	default:
		return -1
	}
}
