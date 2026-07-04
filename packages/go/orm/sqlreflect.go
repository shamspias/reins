package orm

import (
	"database/sql"
	"fmt"
	"regexp"
	"sort"
	"strings"

	reins "github.com/shamspias/reins/packages/go"
)

// This file gives the CLI a genuine zero-code path: reflect a live SQL database
// through its own schema (no hand-written models) and hand the resulting
// ModelInfo set to BuildCapabilities. Go has no SQLAlchemy-style automap, so we
// read the schema ourselves. SQLite is the built-in target (PRAGMA-based); the
// SQLBackend is driver-agnostic ANSI SQL, so other databases need only their own
// reflection function.
//
// database/sql is stdlib, so this keeps the orm package driver-free: the CLI
// blank-imports the concrete driver (e.g. modernc.org/sqlite) and opens the *sql.DB.

// identRe guards every identifier we splice into SQL. Names come from the schema
// (trusted), but we validate + quote regardless — belt and suspenders against
// injection, per §2.6 (treat everything as untrusted).
var identRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

func quoteIdent(name string) (string, bool) {
	if !identRe.MatchString(name) {
		return "", false
	}
	return `"` + name + `"`, true
}

// ReflectSQLite introspects a live SQLite database into ORM-neutral ModelInfo —
// one per user table — via PRAGMA. It never touches sqlite_* internal tables.
func ReflectSQLite(db *sql.DB) ([]ModelInfo, error) {
	tables, err := listTables(db)
	if err != nil {
		return nil, err
	}
	if len(tables) == 0 {
		return nil, reins.NewError("the database has no tables", "point reins at a database with tables, or create one first")
	}
	infos := make([]ModelInfo, 0, len(tables))
	for _, t := range tables {
		info, err := reflectTable(db, t)
		if err != nil {
			return nil, err
		}
		if len(info.Columns) > 0 {
			infos = append(infos, info)
		}
	}
	if len(infos) == 0 {
		return nil, reins.NewError("no usable tables found", "reins needs tables with readable columns")
	}
	return infos, nil
}

func listTables(db *sql.DB) ([]string, error) {
	rows, err := db.Query(`SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name`)
	if err != nil {
		return nil, reins.NewError("cannot read the schema: "+err.Error(), "check the database URL and that the file is a valid SQLite database")
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		out = append(out, name)
	}
	return out, rows.Err()
}

func reflectTable(db *sql.DB, table string) (ModelInfo, error) {
	qt, ok := quoteIdent(table)
	if !ok {
		return ModelInfo{}, nil // unusual name — skip rather than risk unsafe SQL
	}
	rows, err := db.Query("PRAGMA table_info(" + qt + ")")
	if err != nil {
		return ModelInfo{}, reins.NewError(fmt.Sprintf("cannot read columns of %q: %v", table, err), "check the table exists and is readable")
	}
	defer rows.Close()

	var columns []ColumnInfo
	var primaryKey *ColumnInfo
	for rows.Next() {
		var (
			cid         int
			name, ctype string
			notnull, pk int
			dflt        sql.NullString
		)
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			return ModelInfo{}, err
		}
		if _, ok := quoteIdent(name); !ok {
			continue // skip a column we couldn't safely reference
		}
		col := ColumnInfo{
			Name:          name,
			JSONType:      sqliteJSONType(ctype),
			Nullable:      notnull == 0 && pk == 0,
			Autoincrement: pk == 1 && strings.Contains(strings.ToUpper(ctype), "INT"),
			HasDefault:    dflt.Valid,
		}
		columns = append(columns, col)
		if pk >= 1 && primaryKey == nil {
			c := col
			primaryKey = &c
		}
	}
	if err := rows.Err(); err != nil {
		return ModelInfo{}, err
	}

	unique := singleColumnUniques(db, qt)
	for i := range columns {
		if unique[columns[i].Name] {
			columns[i].Unique = true
		}
	}
	return ModelInfo{
		Singular:   singularize(table),
		Plural:     table,
		Columns:    columns,
		PrimaryKey: primaryKey,
	}, nil
}

// singleColumnUniques returns the columns backed by a single-column UNIQUE index,
// which is what makes a natural-key lookup safe (exactly one row). Partial indexes
// are ignored — they don't guarantee uniqueness across the table.
func singleColumnUniques(db *sql.DB, quotedTable string) map[string]bool {
	out := map[string]bool{}
	rows, err := db.Query("PRAGMA index_list(" + quotedTable + ")")
	if err != nil {
		return out
	}
	type idx struct {
		name    string
		partial bool
	}
	var uniques []idx
	for rows.Next() {
		var (
			seq             int
			name, origin    string
			unique, partial int
		)
		// Older SQLite omits origin/partial; scan defensively.
		if err := rows.Scan(&seq, &name, &unique, &origin, &partial); err != nil {
			rows.Close()
			return out
		}
		if unique == 1 && partial == 0 {
			uniques = append(uniques, idx{name: name})
		}
	}
	rows.Close()

	for _, u := range uniques {
		qn, ok := quoteIdent(u.name)
		if !ok {
			continue
		}
		cols := indexColumns(db, qn)
		if len(cols) == 1 {
			out[cols[0]] = true
		}
	}
	return out
}

func indexColumns(db *sql.DB, quotedIndex string) []string {
	rows, err := db.Query("PRAGMA index_info(" + quotedIndex + ")")
	if err != nil {
		return nil
	}
	defer rows.Close()
	var cols []string
	for rows.Next() {
		var seqno, cid int
		var name sql.NullString
		if err := rows.Scan(&seqno, &cid, &name); err != nil {
			return nil
		}
		if name.Valid {
			cols = append(cols, name.String)
		}
	}
	return cols
}

// sqliteJSONType maps a declared SQLite type to a JSON Schema type by affinity.
func sqliteJSONType(declared string) string {
	t := strings.ToUpper(declared)
	switch {
	case strings.Contains(t, "INT"):
		return "integer"
	case strings.Contains(t, "BOOL"):
		return "boolean"
	case strings.Contains(t, "REAL"), strings.Contains(t, "FLOA"), strings.Contains(t, "DOUB"),
		strings.Contains(t, "NUMERIC"), strings.Contains(t, "DEC"):
		return "number"
	default: // TEXT, CHAR, CLOB, BLOB, DATE, and anything unknown → string
		return "string"
	}
}

// singularize is a small, deliberately naive English de-pluralizer for capability
// names (get_customer_by_id from a customers table). If it guesses wrong the name
// is still valid — just less pretty.
func singularize(word string) string {
	lower := strings.ToLower(word)
	switch {
	case strings.HasSuffix(lower, "ies") && len(word) > 3:
		return word[:len(word)-3] + "y"
	case strings.HasSuffix(lower, "ses"), strings.HasSuffix(lower, "xes"),
		strings.HasSuffix(lower, "zes"), strings.HasSuffix(lower, "ches"),
		strings.HasSuffix(lower, "shes"):
		return word[:len(word)-2]
	case strings.HasSuffix(lower, "s") && !strings.HasSuffix(lower, "ss") && len(word) > 1:
		return word[:len(word)-1]
	default:
		return word
	}
}

// FromSQL reflects a live SQL database and returns intent-level capabilities plus
// a schema index — the zero-code path behind `reins inspect/ask/chat <db-url>`.
// Reads are always generated; writes only when canWrite is true (and are then
// gated by the autonomy ladder like any capability).
func FromSQL(db *sql.DB, canWrite bool, readLimit int) ([]reins.BoundCapability, reins.SchemaIndex, error) {
	infos, err := ReflectSQLite(db)
	if err != nil {
		return nil, reins.SchemaIndex{}, err
	}
	caps, index := BuildCapabilities(SQLBackend{DB: db}, infos, canWrite, readLimit)
	return caps, index, nil
}

// SQLBackend runs the six data operations against a database/sql handle using
// portable, parameterized SQL. Identifiers are validated + quoted; every value is
// bound through a placeholder.
type SQLBackend struct {
	DB *sql.DB
}

func (b SQLBackend) table(info ModelInfo) (string, error) {
	qt, ok := quoteIdent(info.Plural)
	if !ok {
		return "", reins.NewError("unsafe table name: "+info.Plural, "reins only operates on tables with plain identifier names")
	}
	return qt, nil
}

// whereClause builds "WHERE a=? AND b=?" from filters, sorted for determinism.
func whereClause(filters map[string]any) (string, []any, error) {
	if len(filters) == 0 {
		return "", nil, nil
	}
	keys := make([]string, 0, len(filters))
	for k := range filters {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	var args []any
	for _, k := range keys {
		qc, ok := quoteIdent(k)
		if !ok {
			return "", nil, reins.NewError("unsafe column name: "+k, "filter only on real column names")
		}
		parts = append(parts, qc+" = ?")
		args = append(args, filters[k])
	}
	return " WHERE " + strings.Join(parts, " AND "), args, nil
}

func (b SQLBackend) Find(info ModelInfo, filters map[string]any, limit int) ([]map[string]any, error) {
	qt, err := b.table(info)
	if err != nil {
		return nil, err
	}
	where, args, err := whereClause(filters)
	if err != nil {
		return nil, err
	}
	rows, err := b.DB.Query(fmt.Sprintf("SELECT * FROM %s%s LIMIT %d", qt, where, limit), args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	scanned, err := scanRows(rows)
	if err != nil {
		return nil, err
	}
	return trimRows(info, scanned), nil
}

func (b SQLBackend) Count(info ModelInfo, filters map[string]any) (int, error) {
	qt, err := b.table(info)
	if err != nil {
		return 0, err
	}
	where, args, err := whereClause(filters)
	if err != nil {
		return 0, err
	}
	var n int
	if err := b.DB.QueryRow("SELECT COUNT(*) FROM "+qt+where, args...).Scan(&n); err != nil {
		return 0, err
	}
	return n, nil
}

func (b SQLBackend) Get(info ModelInfo, key string, value any) (map[string]any, error) {
	rows, err := b.Find(info, map[string]any{key: value}, 1)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return rows[0], nil
}

func (b SQLBackend) Create(info ModelInfo, values map[string]any) (map[string]any, error) {
	qt, err := b.table(info)
	if err != nil {
		return nil, err
	}
	if len(values) == 0 {
		return nil, reins.NewError("nothing to create", "provide at least one column value")
	}
	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	cols := make([]string, len(keys))
	placeholders := make([]string, len(keys))
	args := make([]any, len(keys))
	for i, k := range keys {
		qc, ok := quoteIdent(k)
		if !ok {
			return nil, reins.NewError("unsafe column name: "+k, "create only with real column names")
		}
		cols[i] = qc
		placeholders[i] = "?"
		args[i] = values[k]
	}
	q := fmt.Sprintf("INSERT INTO %s (%s) VALUES (%s)", qt, strings.Join(cols, ", "), strings.Join(placeholders, ", "))
	if _, err := b.DB.Exec(q, args...); err != nil {
		return nil, err
	}
	return trimRow(info, values), nil
}

func (b SQLBackend) Update(info ModelInfo, key string, value any, changes map[string]any) (map[string]any, error) {
	existing, err := b.Get(info, key, value)
	if err != nil {
		return nil, err
	}
	if existing == nil {
		return nil, missing(info, key, value)
	}
	if len(changes) == 0 {
		return existing, nil
	}
	qt, err := b.table(info)
	if err != nil {
		return nil, err
	}
	qk, ok := quoteIdent(key)
	if !ok {
		return nil, reins.NewError("unsafe key column: "+key, "update by a real column name")
	}
	keys := make([]string, 0, len(changes))
	for k := range changes {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	sets := make([]string, len(keys))
	args := make([]any, 0, len(keys)+1)
	for i, k := range keys {
		qc, ok := quoteIdent(k)
		if !ok {
			return nil, reins.NewError("unsafe column name: "+k, "update only real column names")
		}
		sets[i] = qc + " = ?"
		args = append(args, changes[k])
	}
	args = append(args, value)
	q := fmt.Sprintf("UPDATE %s SET %s WHERE %s = ?", qt, strings.Join(sets, ", "), qk)
	if _, err := b.DB.Exec(q, args...); err != nil {
		return nil, err
	}
	return b.Get(info, key, value)
}

func (b SQLBackend) Delete(info ModelInfo, key string, value any) error {
	existing, err := b.Get(info, key, value)
	if err != nil {
		return err
	}
	if existing == nil {
		return missing(info, key, value)
	}
	qt, err := b.table(info)
	if err != nil {
		return err
	}
	qk, ok := quoteIdent(key)
	if !ok {
		return reins.NewError("unsafe key column: "+key, "delete by a real column name")
	}
	_, err = b.DB.Exec(fmt.Sprintf("DELETE FROM %s WHERE %s = ?", qt, qk), value)
	return err
}

// scanRows reads all rows of a SELECT * into column-keyed maps, normalizing values
// (bytes → string) so results are JSON-clean before trimming.
func scanRows(rows *sql.Rows) ([]map[string]any, error) {
	cols, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	var out []map[string]any
	for rows.Next() {
		cells := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range cells {
			ptrs[i] = &cells[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		row := make(map[string]any, len(cols))
		for i, name := range cols {
			row[name] = jsonValue(cells[i])
		}
		out = append(out, row)
	}
	return out, rows.Err()
}
