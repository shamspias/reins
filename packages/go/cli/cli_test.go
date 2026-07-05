package cli

import (
	"bytes"
	"database/sql"
	"errors"
	"path/filepath"
	"strings"
	"testing"

	reins "github.com/shamspias/reins/packages/go"
)

// tempDB creates a real on-disk SQLite database (the CLI opens by URL, so an
// in-memory handle wouldn't be shared) seeded with a customers table + two rows.
func tempDB(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "app.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	if _, err := db.Exec(`CREATE TABLE customers (
		id     INTEGER PRIMARY KEY,
		email  TEXT UNIQUE NOT NULL,
		name   TEXT,
		active INTEGER DEFAULT 1
	)`); err != nil {
		t.Fatalf("create: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO customers (email, name, active) VALUES ('ada@x.com','Ada',1),('bob@x.com','Bob',0)`); err != nil {
		t.Fatalf("seed: %v", err)
	}
	return path
}

func TestInspectReadOnlyThenWrite(t *testing.T) {
	path := tempDB(t)

	var buf bytes.Buffer
	if err := Inspect(path, false, &buf); err != nil {
		t.Fatalf("inspect: %v", err)
	}
	s := buf.String()
	for _, want := range []string{"Reflected 1 table", "find_customers", "count_customers", "get_customer_by_email", "read-only"} {
		if !strings.Contains(s, want) {
			t.Errorf("read-only inspect missing %q in:\n%s", want, s)
		}
	}
	if strings.Contains(s, "create_customer") {
		t.Errorf("read-only inspect must not list a write capability:\n%s", s)
	}

	var wbuf bytes.Buffer
	if err := Inspect(path, true, &wbuf); err != nil {
		t.Fatalf("inspect --write: %v", err)
	}
	w := wbuf.String()
	for _, want := range []string{"create_customer", "update_customer", "delete_customer"} {
		if !strings.Contains(w, want) {
			t.Errorf("--write inspect missing %q in:\n%s", want, w)
		}
	}
}

func TestAskCallsCapabilityAndAnswers(t *testing.T) {
	path := tempDB(t)
	// The scripted model looks up a customer, then answers — exercising the full
	// reflect → agent → capability → answer path with no network.
	model := reins.NewFakeModel(
		reins.CallResponse("c1", "get_customer_by_email", map[string]any{"email": "ada@x.com"}),
		reins.FinalResponse("Ada is active."),
	)
	var buf bytes.Buffer
	if err := Ask(path, "is ada active?", model, &buf); err != nil {
		t.Fatalf("ask: %v", err)
	}
	if got := strings.TrimSpace(buf.String()); got != "Ada is active." {
		t.Errorf("ask output = %q", got)
	}
}

func TestChatAnswersThenExits(t *testing.T) {
	path := tempDB(t)
	model := reins.NewFakeModel(reins.FinalResponse("There are 2 customers."))
	in := strings.NewReader("how many customers?\nexit\n")
	var buf bytes.Buffer
	if err := Chat(path, model, in, &buf); err != nil {
		t.Fatalf("chat: %v", err)
	}
	s := buf.String()
	if !strings.Contains(s, "There are 2 customers.") {
		t.Errorf("chat missing the answer:\n%s", s)
	}
	if !strings.Contains(s, "read-only over") {
		t.Errorf("chat missing its banner:\n%s", s)
	}
}

func TestSQLitePathParsing(t *testing.T) {
	cases := map[string]string{
		"./app.db":                 "./app.db",
		":memory:":                 ":memory:",
		"sqlite:///abs/app.db":     "/abs/app.db",
		"sqlite://rel.db":          "rel.db",
		"sqlite:plain.db":          "plain.db",
		"sqlite3:///x.db":          "/x.db",
		"file:app.db?cache=shared": "file:app.db?cache=shared",
	}
	for in, want := range cases {
		if got := sqlitePath(in); got != want {
			t.Errorf("sqlitePath(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestOpenDBBadPathIsHinted(t *testing.T) {
	_, err := OpenDB(t.TempDir()) // a directory is not a valid SQLite file
	if err == nil {
		t.Fatal("opening a directory should error")
	}
	var rerr *reins.Error
	if !errors.As(err, &rerr) || rerr.Hint == "" {
		t.Errorf("OpenDB error should carry a → hint, got %v", err)
	}
}
