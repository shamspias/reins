package reins

import (
	"fmt"
	"sort"
)

// CapabilityRegistry is the only path by which the agent reaches a capability. It
// validates arguments against the schema BEFORE executing (model output is
// untrusted — the schema is the prepared statement, invariant §2.6), then invokes
// the handler and wraps the outcome as errors-as-data (CapabilityResult). Only
// control-plane misuse (duplicate registration) returns an error.
type CapabilityRegistry struct {
	caps  map[string]BoundCapability
	order []string
}

// NewRegistry builds an empty registry.
func NewRegistry() *CapabilityRegistry {
	return &CapabilityRegistry{caps: map[string]BoundCapability{}}
}

// Register adds a capability; a duplicate name is a control-plane error.
func (r *CapabilityRegistry) Register(cap BoundCapability) error {
	if _, exists := r.caps[cap.Spec.Name]; exists {
		return NewError(
			fmt.Sprintf("capability %q is already registered", cap.Spec.Name),
			"give each capability a unique name",
		)
	}
	r.caps[cap.Spec.Name] = cap
	r.order = append(r.order, cap.Spec.Name)
	return nil
}

// List returns the capability specs in registration order.
func (r *CapabilityRegistry) List() []Capability {
	out := make([]Capability, 0, len(r.order))
	for _, name := range r.order {
		out = append(out, r.caps[name].Spec)
	}
	return out
}

// ToolSpecs returns the intent surfaces shown to the model (never policy metadata).
func (r *CapabilityRegistry) ToolSpecs() []ToolSpec {
	out := make([]ToolSpec, 0, len(r.order))
	for _, name := range r.order {
		out = append(out, r.caps[name].Spec.ToolSpec())
	}
	return out
}

// Get returns the bound capability and whether it exists.
func (r *CapabilityRegistry) Get(name string) (BoundCapability, bool) {
	cap, ok := r.caps[name]
	return cap, ok
}

// Validate checks args against the capability's schema; returns a hinted error if invalid.
func (r *CapabilityRegistry) Validate(name string, args map[string]any) error {
	cap, ok := r.caps[name]
	if !ok {
		return NewError(fmt.Sprintf("no capability named %q", name), "available: "+sortedNames(r.caps))
	}
	return validateArgs(cap.Spec.InputSchema, args, name)
}

// Call validates then invokes, returning the outcome as data (never an error over
// the loop). The run's Context is passed to the handler (identity, §2.7).
func (r *CapabilityRegistry) Call(name string, args map[string]any, ctx Context) CapabilityResult {
	cap, ok := r.caps[name]
	if !ok {
		fix := "register it with a capability"
		if matches := closestMatches(name, r.order, 3); len(matches) > 0 {
			fix = fmt.Sprintf("closest matches: %v", matches)
		}
		return CapabilityResult{OK: false, Error: fmt.Sprintf("no capability named %q\n  → %s", name, fix)}
	}
	if err := validateArgs(cap.Spec.InputSchema, args, name); err != nil {
		return CapabilityResult{OK: false, Error: err.Error()}
	}
	value, err := cap.Handler(ctx, args)
	if err != nil {
		return CapabilityResult{OK: false, Error: err.Error()}
	}
	return CapabilityResult{OK: true, Value: value}
}

// --- schema validation (top-level keys + JSON types; fail-closed) ----------------

func validateArgs(schema, args map[string]any, capName string) error {
	properties, _ := schema["properties"].(map[string]any)
	allowExtra := schema["additionalProperties"] == true
	for _, key := range requiredKeys(schema) {
		if _, ok := args[key]; !ok {
			return NewError(fmt.Sprintf("%s: missing required argument %q", capName, key), "include "+key)
		}
	}
	for key, value := range args {
		spec, known := properties[key]
		if !known {
			if allowExtra {
				continue
			}
			return NewError(
				fmt.Sprintf("%s: unexpected argument %q", capName, key),
				"allowed: "+sortedMapKeys(properties),
			)
		}
		expected := ""
		if m, ok := spec.(map[string]any); ok {
			if t, ok := m["type"].(string); ok {
				expected = t
			}
		}
		if expected != "" && !typeMatches(expected, value) {
			return NewError(
				fmt.Sprintf("%s: argument %q must be of type %s", capName, key, expected),
				fmt.Sprintf("got %T", value),
			)
		}
	}
	return nil
}

func requiredKeys(schema map[string]any) []string {
	switch req := schema["required"].(type) {
	case []string:
		return req
	case []any:
		out := make([]string, 0, len(req))
		for _, v := range req {
			if s, ok := v.(string); ok {
				out = append(out, s)
			}
		}
		return out
	default:
		return nil
	}
}

func typeMatches(expected string, value any) bool {
	switch expected {
	case "integer":
		switch n := value.(type) {
		case int, int8, int16, int32, int64:
			return true
		case float64:
			return n == float64(int64(n)) // JSON/YAML numbers arrive as float64
		default:
			return false
		}
	case "number":
		switch value.(type) {
		case int, int8, int16, int32, int64, float32, float64:
			return true
		default:
			return false
		}
	case "boolean":
		_, ok := value.(bool)
		return ok
	case "string":
		_, ok := value.(string)
		return ok
	case "array":
		_, ok := value.([]any)
		return ok
	case "object":
		_, ok := value.(map[string]any)
		return ok
	default:
		return true // unknown type — accept
	}
}

// --- helpers ---------------------------------------------------------------------

func sortedNames(caps map[string]BoundCapability) string {
	names := make([]string, 0, len(caps))
	for name := range caps {
		names = append(names, name)
	}
	sort.Strings(names)
	return fmt.Sprintf("%v", names)
}

func sortedMapKeys(m map[string]any) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return fmt.Sprintf("%v", keys)
}

// closestMatches returns up to n candidates within a small edit distance of name,
// nearest first — a lightweight stand-in for Python's difflib suggestions.
func closestMatches(name string, candidates []string, n int) []string {
	type scored struct {
		name string
		dist int
	}
	var ranked []scored
	for _, c := range candidates {
		d := levenshtein(name, c)
		if d <= len(name)/2+1 { // "close enough" cutoff
			ranked = append(ranked, scored{c, d})
		}
	}
	sort.SliceStable(ranked, func(i, j int) bool { return ranked[i].dist < ranked[j].dist })
	out := []string{}
	for i := 0; i < len(ranked) && i < n; i++ {
		out = append(out, ranked[i].name)
	}
	return out
}

func levenshtein(a, b string) int {
	prev := make([]int, len(b)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(a); i++ {
		cur := make([]int, len(b)+1)
		cur[0] = i
		for j := 1; j <= len(b); j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			cur[j] = min3(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
		}
		prev = cur
	}
	return prev[len(b)]
}

func min3(a, b, c int) int {
	m := a
	if b < m {
		m = b
	}
	if c < m {
		m = c
	}
	return m
}
