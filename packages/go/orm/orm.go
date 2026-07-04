// Package orm auto-exposes ORM models as intent-level Reins capabilities — the Go
// mirror of the Python reins.orm package. GORM ships here (M6.5); the capability
// shaping (names, linting, trimming, schema) is ORM-neutral and lives in this file
// behind a Backend, so another ORM means implementing only introspection + the six
// data operations.
//
// It lives in a subpackage so the core reins package never depends on an ORM.
package orm

import (
	"fmt"
	"sort"
	"strings"

	reins "github.com/shamspias/reins/packages/go"
)

const (
	maxParams          = 7  // keep generated capabilities lean (linter §2.15)
	maxColumnsReturned = 12 // trim wide rows so raw tables never flood context
	defaultReadLimit   = 50 // bounded result sets (§2.13)
)

// naturalKeyHints are column names commonly used as human-facing keys, best first.
var naturalKeyHints = []string{"slug", "email", "username", "code", "key", "name", "title", "uuid"}

// ColumnInfo describes one column in ORM-neutral terms.
type ColumnInfo struct {
	Name          string
	JSONType      string
	Nullable      bool
	Autoincrement bool
	HasDefault    bool
	Unique        bool
}

func (c ColumnInfo) optionalOnCreate() bool {
	return c.Nullable || c.Autoincrement || c.HasDefault
}

// ModelInfo describes one mapped model in ORM-neutral terms.
type ModelInfo struct {
	Model       any
	Singular    string
	Plural      string
	Columns     []ColumnInfo
	PrimaryKey  *ColumnInfo
	Description string
}

func (m ModelInfo) naturalKey() *ColumnInfo {
	pkName := ""
	if m.PrimaryKey != nil {
		pkName = m.PrimaryKey.Name
	}
	uniqueStrings := map[string]*ColumnInfo{}
	var order []string
	for i := range m.Columns {
		c := &m.Columns[i]
		if c.Unique && c.JSONType == "string" && c.Name != pkName {
			uniqueStrings[c.Name] = c
			order = append(order, c.Name)
		}
	}
	if len(order) == 0 {
		return nil
	}
	for _, hint := range naturalKeyHints {
		if c, ok := uniqueStrings[hint]; ok {
			return c
		}
	}
	return uniqueStrings[order[0]]
}

func (m ModelInfo) lookupKey() *ColumnInfo {
	if nk := m.naturalKey(); nk != nil {
		return nk
	}
	return m.PrimaryKey
}

// Backend runs the six data operations for one ORM, returning trimmed rows. Update
// and Delete return a "not found" error when the row is missing — surfaced to the
// model as errors-as-data.
type Backend interface {
	Find(info ModelInfo, filters map[string]any, limit int) ([]map[string]any, error)
	Count(info ModelInfo, filters map[string]any) (int, error)
	Get(info ModelInfo, key string, value any) (map[string]any, error) // nil map ⇒ not found
	Create(info ModelInfo, values map[string]any) (map[string]any, error)
	Update(info ModelInfo, key string, value any, changes map[string]any) (map[string]any, error)
	Delete(info ModelInfo, key string, value any) error
}

// BuildCapabilities turns introspected models into linted capabilities plus a
// schema index. Reads are always generated; writes only when canWrite is true. A
// generated capability that fails the linter is dropped, never shipped (§2.15).
func BuildCapabilities(b Backend, infos []ModelInfo, canWrite bool, readLimit int) ([]reins.BoundCapability, reins.SchemaIndex) {
	if readLimit <= 0 {
		readLimit = defaultReadLimit
	}
	var caps []reins.BoundCapability
	var tables []reins.TableInfo
	for _, info := range infos {
		candidates := readCaps(b, info, readLimit)
		if canWrite {
			candidates = append(candidates, writeCaps(b, info)...)
		}
		for _, c := range candidates {
			if reins.LintCapability(c.Spec, false).OK() {
				caps = append(caps, c)
			}
		}
		tables = append(tables, tableInfo(info))
	}
	return caps, reins.SchemaIndex{Tables: tables}
}

// --- read capabilities -----------------------------------------------------------

func readCaps(b Backend, info ModelInfo, readLimit int) []reins.BoundCapability {
	caps := []reins.BoundCapability{findCap(b, info, readLimit), countCap(b, info)}
	if info.lookupKey() != nil {
		caps = append(caps, getCap(b, info))
	}
	return caps
}

func findCap(b Backend, info ModelInfo, readLimit int) reins.BoundCapability {
	filters := filterColumns(info)
	spec := reins.Capability{
		Name:        "find_" + info.Plural,
		Description: fmt.Sprintf("Find %s matching optional filters (returns up to %d rows, trimmed).", info.Plural, readLimit),
		InputSchema: objectSchema(columnTypes(filters), nil),
		Access:      reins.AccessRead,
	}
	handler := func(_ reins.Context, args map[string]any) (any, error) {
		return b.Find(info, known(args, filters), readLimit)
	}
	return reins.BoundCapability{Spec: spec, Handler: handler}
}

func countCap(b Backend, info ModelInfo) reins.BoundCapability {
	filters := filterColumns(info)
	spec := reins.Capability{
		Name:        "count_" + info.Plural,
		Description: fmt.Sprintf("Count %s matching optional filters.", info.Plural),
		InputSchema: objectSchema(columnTypes(filters), nil),
		Access:      reins.AccessRead,
	}
	handler := func(_ reins.Context, args map[string]any) (any, error) {
		return b.Count(info, known(args, filters))
	}
	return reins.BoundCapability{Spec: spec, Handler: handler}
}

func getCap(b Backend, info ModelInfo) reins.BoundCapability {
	key := info.lookupKey()
	spec := reins.Capability{
		Name:        fmt.Sprintf("get_%s_by_%s", info.Singular, key.Name),
		Description: fmt.Sprintf("Get one %s by its %s (returns the row, trimmed, or nothing if not found).", info.Singular, key.Name),
		InputSchema: objectSchema(map[string]string{key.Name: key.JSONType}, []string{key.Name}),
		Access:      reins.AccessRead,
	}
	handler := func(_ reins.Context, args map[string]any) (any, error) {
		row, err := b.Get(info, key.Name, args[key.Name])
		if err != nil {
			return nil, err
		}
		if row == nil {
			return nil, nil
		}
		return row, nil
	}
	return reins.BoundCapability{Spec: spec, Handler: handler}
}

// --- write capabilities (gated) --------------------------------------------------

func writeCaps(b Backend, info ModelInfo) []reins.BoundCapability {
	caps := []reins.BoundCapability{createCap(b, info)}
	if info.lookupKey() != nil {
		caps = append(caps, updateCap(b, info), deleteCap(b, info))
	}
	return caps
}

func createCap(b Backend, info ModelInfo) reins.BoundCapability {
	settable := settableColumns(info)
	var required []string
	for _, c := range settable {
		if !c.optionalOnCreate() {
			required = append(required, c.Name)
		}
	}
	spec := reins.Capability{
		Name:        "create_" + info.Singular,
		Description: fmt.Sprintf("Create a new %s (returns the created row, trimmed).", info.Singular),
		InputSchema: objectSchema(columnTypes(settable), required),
		Access:      reins.AccessWrite,
	}
	handler := func(_ reins.Context, args map[string]any) (any, error) {
		return b.Create(info, known(args, settable))
	}
	return reins.BoundCapability{Spec: spec, Handler: handler}
}

func updateCap(b Backend, info ModelInfo) reins.BoundCapability {
	key := info.lookupKey()
	// Leave room for the required key under the MAX_PARAMS cap.
	var nonKey []ColumnInfo
	for _, c := range info.Columns {
		if !c.Autoincrement && c.Name != key.Name {
			nonKey = append(nonKey, c)
		}
	}
	settable := prioritize(info, nonKey)
	if len(settable) > maxParams-1 {
		settable = settable[:maxParams-1]
	}
	props := map[string]string{key.Name: key.JSONType}
	for _, c := range settable {
		props[c.Name] = c.JSONType
	}
	spec := reins.Capability{
		Name:        "update_" + info.Singular,
		Description: fmt.Sprintf("Update an existing %s, found by its %s (returns the updated row, trimmed).", info.Singular, key.Name),
		InputSchema: objectSchema(props, []string{key.Name}),
		Access:      reins.AccessWrite,
	}
	handler := func(_ reins.Context, args map[string]any) (any, error) {
		return b.Update(info, key.Name, args[key.Name], known(args, settable))
	}
	return reins.BoundCapability{Spec: spec, Handler: handler}
}

func deleteCap(b Backend, info ModelInfo) reins.BoundCapability {
	key := info.lookupKey()
	spec := reins.Capability{
		Name:        "delete_" + info.Singular,
		Description: fmt.Sprintf("Delete a %s, found by its %s.", info.Singular, key.Name),
		InputSchema: objectSchema(map[string]string{key.Name: key.JSONType}, []string{key.Name}),
		Access:      reins.AccessDestructive,
	}
	handler := func(_ reins.Context, args map[string]any) (any, error) {
		if err := b.Delete(info, key.Name, args[key.Name]); err != nil {
			return nil, err
		}
		return map[string]any{"deleted": args[key.Name]}, nil
	}
	return reins.BoundCapability{Spec: spec, Handler: handler}
}

// --- column selection + helpers --------------------------------------------------

func filterColumns(info ModelInfo) []ColumnInfo {
	return cap7(prioritize(info, info.Columns))
}

func settableColumns(info ModelInfo) []ColumnInfo {
	var s []ColumnInfo
	for _, c := range info.Columns {
		if !c.Autoincrement {
			s = append(s, c)
		}
	}
	return cap7(prioritize(info, s))
}

func cap7(cols []ColumnInfo) []ColumnInfo {
	if len(cols) > maxParams {
		return cols[:maxParams]
	}
	return cols
}

func prioritize(info ModelInfo, columns []ColumnInfo) []ColumnInfo {
	keyNames := map[string]bool{}
	if nk := info.naturalKey(); nk != nil {
		keyNames[nk.Name] = true
	}
	if info.PrimaryKey != nil {
		keyNames[info.PrimaryKey.Name] = true
	}
	rank := func(c ColumnInfo) int {
		switch {
		case keyNames[c.Name]:
			return 0
		case !c.Nullable:
			return 1
		default:
			return 2
		}
	}
	out := append([]ColumnInfo(nil), columns...)
	sort.SliceStable(out, func(i, j int) bool {
		if rank(out[i]) != rank(out[j]) {
			return rank(out[i]) < rank(out[j])
		}
		return out[i].Name < out[j].Name
	})
	return out
}

func known(args map[string]any, columns []ColumnInfo) map[string]any {
	allowed := map[string]bool{}
	for _, c := range columns {
		allowed[c.Name] = true
	}
	out := map[string]any{}
	for k, v := range args {
		if allowed[k] && v != nil {
			out[k] = v
		}
	}
	return out
}

func columnTypes(columns []ColumnInfo) map[string]string {
	out := map[string]string{}
	for _, c := range columns {
		out[c.Name] = c.JSONType
	}
	return out
}

func objectSchema(props map[string]string, required []string) map[string]any {
	properties := map[string]any{}
	for name, jt := range props {
		properties[name] = map[string]any{"type": jt}
	}
	if required == nil {
		required = []string{}
	}
	return map[string]any{
		"type":                 "object",
		"properties":           properties,
		"required":             required,
		"additionalProperties": false,
	}
}

func tableInfo(info ModelInfo) reins.TableInfo {
	desc := strings.TrimSpace(info.Description)
	if desc == "" {
		desc = "The " + info.Plural + " table."
	}
	cols := make([]string, len(info.Columns))
	for i, c := range info.Columns {
		cols[i] = c.Name
	}
	return reins.TableInfo{Name: info.Plural, Description: desc, Columns: cols}
}

func missing(info ModelInfo, key string, value any) error {
	return reins.NewError(
		fmt.Sprintf("no %s with %s=%v", info.Singular, key, value),
		"check the key, or find_ the row first",
	)
}
