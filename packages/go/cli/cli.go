// Package cli implements the `reins` command-line tool: point it at a database
// URL and inspect / ask / chat over it with zero hand-written code. The commands
// are ordinary functions taking an io.Writer (and, for ask/chat, a reins.Model),
// so they test deterministically with a FakeModel and no real database file.
//
// SQLite is the built-in target; the driver is blank-imported here so callers get
// it for free. Other databases need only their own reflection (see orm.FromSQL).
package cli

import (
	"bufio"
	"database/sql"
	"fmt"
	"io"
	"sort"
	"strings"

	reins "github.com/shamspias/reins/packages/go"
	"github.com/shamspias/reins/packages/go/orm"

	_ "github.com/glebarez/go-sqlite" // registers the pure-Go "sqlite" driver
)

// OpenDB opens a SQLite database from a URL. Accepted forms:
//
//	./app.db                      a bare file path
//	:memory:                      an ephemeral in-memory database
//	sqlite:///abs/path/app.db     a sqlite:// URL (scheme stripped)
//	file:app.db?cache=shared      a SQLite file: URI (passed through)
//
// The pool is pinned to one connection so an in-memory database keeps its schema.
func OpenDB(dbURL string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", sqlitePath(dbURL))
	if err != nil {
		return nil, reins.NewError("cannot open database: "+err.Error(), "pass a SQLite file path, :memory:, or a sqlite:/// URL")
	}
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, reins.NewError("cannot open database: "+err.Error(), "check the path points to a readable SQLite database")
	}
	return db, nil
}

// sqlitePath turns a database URL into a path the driver understands. A file: URI
// is passed through untouched (it is meaningful to SQLite); a sqlite scheme is
// stripped; anything else is treated as a bare path.
func sqlitePath(dbURL string) string {
	if strings.HasPrefix(dbURL, "file:") {
		return dbURL
	}
	for _, prefix := range []string{"sqlite3://", "sqlite://", "sqlite3:", "sqlite:"} {
		if strings.HasPrefix(dbURL, prefix) {
			return dbURL[len(prefix):]
		}
	}
	return dbURL
}

// Inspect reflects the database and lists the capabilities reins would expose —
// the transparency surface, and a fast way to see what a model would be given. No
// model is needed. With canWrite, gated create/update/delete also appear.
func Inspect(dbURL string, canWrite bool, out io.Writer) error {
	db, err := OpenDB(dbURL)
	if err != nil {
		return err
	}
	defer db.Close()

	caps, schema, err := orm.FromSQL(db, canWrite, 0)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "Reflected %d table(s) from %s\n", len(schema.Tables), dbURL)
	for _, t := range schema.Tables {
		fmt.Fprintf(out, "  • %s (%d columns)\n", t.Name, len(t.Columns))
	}
	fmt.Fprintf(out, "\n%d capabilities:\n", len(caps))
	for _, line := range describe(caps) {
		fmt.Fprintln(out, line)
	}
	if !canWrite {
		fmt.Fprintln(out, "\n(read-only — pass --write to also generate gated create/update/delete)")
	}
	return nil
}

// Ask answers one read-only question and writes the reply to out. The build is
// read-only (no write capabilities are even generated) and runs through Ask, which
// is structurally incapable of writing (invariant §2.1) — belt and suspenders.
func Ask(dbURL, question string, model reins.Model, out io.Writer) error {
	agent, db, err := readOnlyAgent(dbURL, model)
	if err != nil {
		return err
	}
	defer db.Close()

	res, err := agent.Ask(question)
	if err != nil {
		return err
	}
	fmt.Fprintln(out, strings.TrimSpace(res.Output.Text))
	return nil
}

// Chat runs an interactive read-only question loop until EOF or "exit"/"quit".
func Chat(dbURL string, model reins.Model, in io.Reader, out io.Writer) error {
	agent, db, err := readOnlyAgent(dbURL, model)
	if err != nil {
		return err
	}
	defer db.Close()

	fmt.Fprintf(out, "reins chat — read-only over %s. Ask a question, or type 'exit'.\n", dbURL)
	scanner := bufio.NewScanner(in)
	for {
		fmt.Fprint(out, "\n> ")
		if !scanner.Scan() {
			break
		}
		q := strings.TrimSpace(scanner.Text())
		if q == "" {
			continue
		}
		if q == "exit" || q == "quit" {
			break
		}
		res, err := agent.Ask(q)
		if err != nil {
			fmt.Fprintf(out, "%v\n", err)
			continue
		}
		fmt.Fprintln(out, strings.TrimSpace(res.Output.Text))
	}
	return nil
}

// readOnlyAgent reflects the database (reads only) and builds a READ_ONLY agent.
// The caller owns closing the returned *sql.DB.
func readOnlyAgent(dbURL string, model reins.Model) (*reins.Agent, *sql.DB, error) {
	db, err := OpenDB(dbURL)
	if err != nil {
		return nil, nil, err
	}
	caps, schema, err := orm.FromSQL(db, false, 0)
	if err != nil {
		db.Close()
		return nil, nil, err
	}
	agent, err := reins.NewAgent(model,
		reins.WithCapabilities(caps...),
		reins.WithSchema(schema),
		reins.WithAutonomy(reins.AutonomyReadOnly),
	)
	if err != nil {
		db.Close()
		return nil, nil, err
	}
	return agent, db, nil
}

// describe renders capabilities grouped read → write → destructive, then by name,
// so the riskier operations are easy to spot at the bottom.
func describe(caps []reins.BoundCapability) []string {
	sorted := append([]reins.BoundCapability(nil), caps...)
	sort.SliceStable(sorted, func(i, j int) bool {
		ri, rj := accessRank(sorted[i].Spec.Access), accessRank(sorted[j].Spec.Access)
		if ri != rj {
			return ri < rj
		}
		return sorted[i].Spec.Name < sorted[j].Spec.Name
	})
	lines := make([]string, 0, len(sorted))
	for _, c := range sorted {
		lines = append(lines, fmt.Sprintf("  [%-11s] %s — %s", c.Spec.Access, c.Spec.Name, c.Spec.Description))
	}
	return lines
}

func accessRank(a reins.Access) int {
	switch a {
	case reins.AccessRead:
		return 0
	case reins.AccessWrite:
		return 1
	default:
		return 2
	}
}
