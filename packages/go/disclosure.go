package reins

import (
	"fmt"
	"sort"
	"strings"
)

// Dual progressive disclosure (M6.5, invariant §2.12): never dump every capability
// schema or the whole data model into context. Above a small threshold the model
// sees one constant-size search_capabilities tool instead of the full registry;
// matches are exposed and become directly callable. A SchemaIndex, when attached,
// is likewise reachable only through search_schema. Mirrors the Python port so the
// base-context behavior is language-neutral (asserted by the conformance suite).

const (
	deferAfter         = 12 // above this many capabilities, defer them behind search
	maxCapabilityMatch = 8
	maxTableMatch      = 5
	maxQueryChars      = 200
	searchCapabilities = "search_capabilities"
	searchSchema       = "search_schema"
)

var searchCapabilitiesSpec = ToolSpec{
	Name: searchCapabilities,
	Description: "Search the available operations by keyword and load the matches. " +
		"Call this first to find the operations you need; matches become directly callable.",
	InputSchema: map[string]any{
		"type":                 "object",
		"properties":           map[string]any{"query": map[string]any{"type": "string"}},
		"required":             []string{"query"},
		"additionalProperties": false,
	},
}

var searchSchemaSpec = ToolSpec{
	Name:        searchSchema,
	Description: "Search the application's data model by keyword. Returns only the relevant tables.",
	InputSchema: map[string]any{
		"type":                 "object",
		"properties":           map[string]any{"query": map[string]any{"type": "string"}},
		"required":             []string{"query"},
		"additionalProperties": false,
	},
}

// Disclosure is per-run state: what the model can currently see. Defer nil (auto)
// defers capabilities behind search when more than deferAfter are registered;
// non-nil forces it.
type Disclosure struct {
	reg     *CapabilityRegistry
	schema  *SchemaIndex
	defer_  *bool
	exposed map[string]bool
}

func newDisclosure(reg *CapabilityRegistry, schema *SchemaIndex, defer_ *bool) (*Disclosure, error) {
	d := &Disclosure{reg: reg, schema: schema, defer_: defer_, exposed: map[string]bool{}}
	if d.deferred() {
		if _, ok := reg.Get(searchCapabilities); ok {
			return nil, NewError(
				fmt.Sprintf("capability %q collides with the built-in discovery tool", searchCapabilities),
				"rename your capability — that name is reserved while disclosure is active",
			)
		}
	}
	if schema != nil {
		if _, ok := reg.Get(searchSchema); ok {
			return nil, NewError(
				fmt.Sprintf("capability %q collides with the built-in discovery tool", searchSchema),
				"rename your capability — that name is reserved while a schema is attached",
			)
		}
	}
	return d, nil
}

func (d *Disclosure) deferred() bool {
	if d.defer_ != nil {
		return *d.defer_
	}
	return len(d.reg.List()) > deferAfter
}

// tools returns the tool surface for the next model turn — tiny and constant when deferred.
func (d *Disclosure) tools() []ToolSpec {
	var specs []ToolSpec
	if d.deferred() {
		specs = append(specs, searchCapabilitiesSpec)
		names := make([]string, 0, len(d.exposed))
		for name := range d.exposed {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			if cap, ok := d.reg.Get(name); ok {
				specs = append(specs, cap.Spec.ToolSpec())
			}
		}
	} else {
		specs = d.reg.ToolSpecs()
	}
	if d.schema != nil {
		specs = append(specs, searchSchemaSpec)
	}
	return specs
}

// answer handles a discovery call, or returns (_, false) if call is not one.
// Discovery is a harness-internal read: deterministic, never gated, allowed under Ask.
func (d *Disclosure) answer(call ToolCall) (CapabilityResult, bool) {
	if call.Name == searchCapabilities && d.deferred() {
		return d.query(call, d.searchCaps), true
	}
	if call.Name == searchSchema && d.schema != nil {
		return d.query(call, d.searchTables), true
	}
	return CapabilityResult{}, false
}

func (d *Disclosure) query(call ToolCall, handler func(string) CapabilityResult) CapabilityResult {
	q, _ := call.Arguments["query"].(string)
	q = strings.TrimSpace(q)
	if q == "" {
		return CapabilityResult{OK: false, Error: call.Name + ": 'query' must be a non-empty string\n  → pass keywords, e.g. query=\"refund order\""}
	}
	if len(q) > maxQueryChars {
		q = q[:maxQueryChars]
	}
	return handler(q)
}

func (d *Disclosure) searchCaps(query string) CapabilityResult {
	tokens := tokenize(query)
	type scored struct {
		spec  Capability
		score int
	}
	var ranked []scored
	for _, spec := range d.reg.List() {
		if s := score(tokens, spec.Name, spec.Description); s > 0 {
			ranked = append(ranked, scored{spec, s})
		}
	}
	sort.SliceStable(ranked, func(i, j int) bool {
		if ranked[i].score != ranked[j].score {
			return ranked[i].score > ranked[j].score
		}
		return ranked[i].spec.Name < ranked[j].spec.Name
	})
	if len(ranked) == 0 {
		return CapabilityResult{OK: false, Error: fmt.Sprintf("no operations matched %q\n  → try different keywords", query)}
	}
	lines := []string{"Found operation(s); they are now directly callable:"}
	for i, r := range ranked {
		if i >= maxCapabilityMatch {
			break
		}
		d.exposed[r.spec.Name] = true
		params := strings.Join(schemaParamNames(r.spec.InputSchema), ", ")
		lines = append(lines, fmt.Sprintf("  %s(%s)", r.spec.Name, params))
	}
	return CapabilityResult{OK: true, Value: strings.Join(lines, "\n")}
}

func (d *Disclosure) searchTables(query string) CapabilityResult {
	tokens := tokenize(query)
	type scored struct {
		table TableInfo
		score int
	}
	var ranked []scored
	for _, t := range d.schema.Tables {
		if s := score(tokens, t.Name, t.Description); s > 0 {
			ranked = append(ranked, scored{t, s})
		}
	}
	sort.SliceStable(ranked, func(i, j int) bool {
		if ranked[i].score != ranked[j].score {
			return ranked[i].score > ranked[j].score
		}
		return ranked[i].table.Name < ranked[j].table.Name
	})
	if len(ranked) == 0 {
		return CapabilityResult{OK: false, Error: fmt.Sprintf("no tables matched %q\n  → try different keywords", query)}
	}
	lines := []string{"Found table(s):"}
	for i, r := range ranked {
		if i >= maxTableMatch {
			break
		}
		lines = append(lines, fmt.Sprintf("  %s (fields: %s)", r.table.Name, strings.Join(r.table.Columns, ", ")))
	}
	return CapabilityResult{OK: true, Value: strings.Join(lines, "\n")}
}

func schemaParamNames(schema map[string]any) []string {
	props, _ := schema["properties"].(map[string]any)
	names := make([]string, 0, len(props))
	for k := range props {
		names = append(names, k)
	}
	sort.Strings(names)
	return names
}

func tokenize(text string) map[string]bool {
	out := map[string]bool{}
	var word []rune
	flush := func() {
		if len(word) > 0 {
			out[string(word)] = true
			word = word[:0]
		}
	}
	for _, ch := range strings.ToLower(text) {
		if (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') {
			word = append(word, ch)
		} else {
			flush()
		}
	}
	flush()
	return out
}

func score(query map[string]bool, name, description string) int {
	nameTokens := tokenize(name)
	descTokens := tokenize(description)
	total := 0
	for token := range query {
		if nameTokens[token] {
			total += 3
		} else {
			for nt := range nameTokens {
				if strings.Contains(nt, token) {
					total += 2
					break
				}
			}
		}
		if descTokens[token] {
			total++
		}
	}
	return total
}
