package orm

import (
	"database/sql"
	"errors"
	"testing"

	_ "github.com/glebarez/go-sqlite" // registers the pure-Go "sqlite" database/sql driver
	reins "github.com/shamspias/reins/packages/go"
)

// openMem returns a fresh in-memory SQLite database pinned to one connection so the
// schema persists across queries (each new connection would get its own empty db).
func openMem(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { db.Close() })
	return db
}

func seedCustomers(t *testing.T, db *sql.DB) {
	t.Helper()
	_, err := db.Exec(`CREATE TABLE customers (
		id     INTEGER PRIMARY KEY,
		email  TEXT UNIQUE NOT NULL,
		name   TEXT,
		active INTEGER DEFAULT 1
	)`)
	if err != nil {
		t.Fatalf("create table: %v", err)
	}
}

func TestReflectSQLiteShapesTheTable(t *testing.T) {
	db := openMem(t)
	seedCustomers(t, db)

	infos, err := ReflectSQLite(db)
	if err != nil {
		t.Fatalf("reflect: %v", err)
	}
	if len(infos) != 1 {
		t.Fatalf("expected 1 table, got %d", len(infos))
	}
	info := infos[0]
	if info.Plural != "customers" || info.Singular != "customer" {
		t.Errorf("names = %q/%q", info.Singular, info.Plural)
	}
	if len(info.Columns) != 4 {
		t.Fatalf("columns = %d", len(info.Columns))
	}
	byName := map[string]ColumnInfo{}
	for _, c := range info.Columns {
		byName[c.Name] = c
	}
	if !byName["id"].Autoincrement || byName["id"].JSONType != "integer" {
		t.Errorf("id column = %+v", byName["id"])
	}
	if !byName["email"].Unique || byName["email"].JSONType != "string" || byName["email"].Nullable {
		t.Errorf("email column = %+v", byName["email"])
	}
	if !byName["name"].Nullable {
		t.Errorf("name should be nullable: %+v", byName["name"])
	}
	if !byName["active"].HasDefault {
		t.Errorf("active should carry a default: %+v", byName["active"])
	}
	if info.PrimaryKey == nil || info.PrimaryKey.Name != "id" {
		t.Errorf("primary key = %+v", info.PrimaryKey)
	}
	// email is a unique non-pk string → the natural key for lookups.
	if nk := info.naturalKey(); nk == nil || nk.Name != "email" {
		t.Errorf("natural key = %+v", nk)
	}
}

func TestSQLBackendRoundTrip(t *testing.T) {
	db := openMem(t)
	seedCustomers(t, db)
	infos, _ := ReflectSQLite(db)
	info := infos[0]
	b := SQLBackend{DB: db}

	created, err := b.Create(info, map[string]any{"email": "a@b.com", "name": "Ada", "active": 1})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if created["email"] != "a@b.com" {
		t.Errorf("created = %+v", created)
	}

	got, err := b.Get(info, "email", "a@b.com")
	if err != nil || got == nil {
		t.Fatalf("get: %v / %v", got, err)
	}
	if got["name"] != "Ada" {
		t.Errorf("got = %+v", got)
	}

	n, err := b.Count(info, nil)
	if err != nil || n != 1 {
		t.Errorf("count = %d (%v)", n, err)
	}

	updated, err := b.Update(info, "email", "a@b.com", map[string]any{"name": "Ada L."})
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	if updated["name"] != "Ada L." {
		t.Errorf("updated = %+v", updated)
	}

	rows, err := b.Find(info, map[string]any{"active": 1}, 10)
	if err != nil || len(rows) != 1 {
		t.Errorf("find = %d rows (%v)", len(rows), err)
	}

	if err := b.Delete(info, "email", "a@b.com"); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if after, _ := b.Get(info, "email", "a@b.com"); after != nil {
		t.Errorf("row should be gone, got %+v", after)
	}
}

func TestUpdateAndDeleteMissingRowAreHintedErrors(t *testing.T) {
	db := openMem(t)
	seedCustomers(t, db)
	info, _ := ReflectSQLite(db)
	b := SQLBackend{DB: db}

	if _, err := b.Update(info[0], "email", "nope@x.com", map[string]any{"name": "X"}); err == nil {
		t.Error("update of a missing row should error")
	}
	if err := b.Delete(info[0], "email", "nope@x.com"); err == nil {
		t.Error("delete of a missing row should error")
	}
}

func TestFromSQLGeneratesLintedCapabilities(t *testing.T) {
	db := openMem(t)
	seedCustomers(t, db)

	caps, schema, err := FromSQL(db, true, 0)
	if err != nil {
		t.Fatalf("FromSQL: %v", err)
	}
	names := map[string]reins.Access{}
	for _, c := range caps {
		names[c.Spec.Name] = c.Spec.Access
		if !reins.LintCapability(c.Spec, false).OK() {
			t.Errorf("generated capability %q does not pass the linter", c.Spec.Name)
		}
	}
	for _, want := range []string{"find_customers", "count_customers", "get_customer_by_email"} {
		if names[want] != reins.AccessRead {
			t.Errorf("missing read capability %q (access=%v)", want, names[want])
		}
	}
	if names["create_customer"] != reins.AccessWrite {
		t.Errorf("create_customer access = %v", names["create_customer"])
	}
	if names["delete_customer"] != reins.AccessDestructive {
		t.Errorf("delete_customer access = %v", names["delete_customer"])
	}
	if len(schema.Tables) != 1 || schema.Tables[0].Name != "customers" {
		t.Errorf("schema = %+v", schema.Tables)
	}
}

func TestFromSQLReadOnlyOmitsWrites(t *testing.T) {
	db := openMem(t)
	seedCustomers(t, db)
	caps, _, _ := FromSQL(db, false, 0)
	for _, c := range caps {
		if c.Spec.Access != reins.AccessRead {
			t.Errorf("read-only build produced a %v capability %q", c.Spec.Access, c.Spec.Name)
		}
	}
}

func TestReflectEmptyDatabaseIsHinted(t *testing.T) {
	db := openMem(t)
	_, err := ReflectSQLite(db)
	if err == nil {
		t.Fatal("empty database should error")
	}
	var rerr *reins.Error
	if !errors.As(err, &rerr) || rerr.Hint == "" {
		t.Errorf("error should carry a → hint, got %v", err)
	}
}

func TestQuoteIdentRejectsInjection(t *testing.T) {
	for _, bad := range []string{"a; DROP TABLE users", "col name", `"quoted"`, "", "1col"} {
		if _, ok := quoteIdent(bad); ok {
			t.Errorf("quoteIdent should reject %q", bad)
		}
	}
	if q, ok := quoteIdent("customer_email"); !ok || q != `"customer_email"` {
		t.Errorf("quoteIdent(customer_email) = %q, %v", q, ok)
	}
}

func TestSingularize(t *testing.T) {
	cases := map[string]string{
		"customers": "customer", "users": "user", "categories": "category",
		"boxes": "box", "addresses": "address", "data": "data",
	}
	for in, want := range cases {
		if got := singularize(in); got != want {
			t.Errorf("singularize(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSQLiteTypeAffinity(t *testing.T) {
	cases := map[string]string{
		"INTEGER": "integer", "VARCHAR(20)": "string", "TEXT": "string",
		"REAL": "number", "NUMERIC": "number", "BOOLEAN": "boolean", "": "string", "BLOB": "string",
	}
	for in, want := range cases {
		if got := sqliteJSONType(in); got != want {
			t.Errorf("sqliteJSONType(%q) = %q, want %q", in, got, want)
		}
	}
}
