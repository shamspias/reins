package reins

import (
	"os"
	"path/filepath"
	"sort"
	"testing"

	"gopkg.in/yaml.v3"
)

// The Go adapter for the cross-language conformance suite (M6.5): it loads the same
// language-neutral cases in spec/conformance/cases/*.yaml and drives the Go harness
// with a scripted FakeModel, asserting each case's expect block. This is how parity
// (invariant §2.21) and the safety invariants are locked across Python and Go.
//
// Code-mode programs are a language-specific dialect (a {program} step carries
// Python source), so those golden cases are skipped here with a recorded reason —
// Go code-mode is validated by native Go tests instead. Every other case (call /
// final / autonomy / approvals / linter) is genuinely neutral and must pass.

type conformanceCase struct {
	Name         string           `yaml:"name"`
	Verb         string           `yaml:"verb"`
	Goal         string           `yaml:"goal"`
	Capabilities []string         `yaml:"capabilities"`
	ModelScript  []map[string]any `yaml:"model_script"`
	Autonomy     string           `yaml:"autonomy"`
	Approvals    string           `yaml:"approvals"`
	Budget       map[string]any   `yaml:"budget"`
	Capability   map[string]any   `yaml:"capability"`
	Expect       map[string]any   `yaml:"expect"`
}

func casesDir(t *testing.T) string {
	// packages/go/ -> repo root is ../../
	dir, err := filepath.Abs(filepath.Join("..", "..", "spec", "conformance", "cases"))
	if err != nil {
		t.Fatalf("resolve cases dir: %v", err)
	}
	return dir
}

func loadCases(t *testing.T) []conformanceCase {
	dir := casesDir(t)
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read cases dir %s: %v", dir, err)
	}
	var cases []conformanceCase
	for _, e := range entries {
		if filepath.Ext(e.Name()) != ".yaml" {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			t.Fatalf("read %s: %v", e.Name(), err)
		}
		var c conformanceCase
		if err := yaml.Unmarshal(data, &c); err != nil {
			t.Fatalf("parse %s: %v", e.Name(), err)
		}
		cases = append(cases, c)
	}
	sort.Slice(cases, func(i, j int) bool { return cases[i].Name < cases[j].Name })
	if len(cases) == 0 {
		t.Fatalf("no cases found in %s", dir)
	}
	return cases
}

func TestConformanceSuite(t *testing.T) {
	for _, c := range loadCases(t) {
		c := c
		t.Run(c.Name, func(t *testing.T) {
			if c.Capability != nil {
				driveLinterCase(t, c)
				return
			}
			driveGoldenCase(t, c)
		})
	}
}

func driveLinterCase(t *testing.T, c conformanceCase) {
	cap := c.Capability
	name, _ := cap["name"].(string)
	description, _ := cap["description"].(string)
	params := asInt(cap["params"])
	returnsRaw := cap["returns"] == "raw_row"
	report := Lint(name, description, params, returnsRaw)

	wantLint, _ := c.Expect["lint"].(string)
	if (wantLint == "pass") != report.OK() {
		t.Fatalf("lint = %v (violations %v), want %s", report.OK(), report.Violations, wantLint)
	}
	if raw, ok := c.Expect["violations"]; ok {
		want := asStrings(raw)
		sort.Strings(want)
		got := append([]string(nil), report.Violations...)
		sort.Strings(got)
		if !equalStrings(got, want) {
			t.Fatalf("violations = %v, want %v", got, want)
		}
	}
}

func driveGoldenCase(t *testing.T, c conformanceCase) {
	if scriptHasProgram(c.ModelScript) {
		t.Skipf("code-mode program is a language-specific dialect (Python source); "+
			"Go code-mode is validated by native tests — case %q", c.Name)
	}

	reg := NewRegistry()
	for _, capName := range c.Capabilities {
		// A permissive stub (additionalProperties:true, access classified from the
		// name) — identical to the Python runner's stub. Returns an empty list so
		// code over it (e.g. len()) works.
		name := capName
		stub := BoundCapability{
			Spec: Capability{
				Name:        name,
				Description: "Stub capability " + name + ".",
				InputSchema: map[string]any{"type": "object", "additionalProperties": true},
				Access:      Classify(name),
			},
			Handler: func(_ Context, _ map[string]any) (any, error) { return []any{}, nil },
		}
		if err := reg.Register(stub); err != nil {
			t.Fatalf("register %s: %v", name, err)
		}
	}

	model := scriptModel(c.ModelScript)
	autonomy := AutonomyLevel(c.Autonomy)
	approvals := c.Approvals // "allow" | "deny" | "" (deny-all default)

	budget := DefaultBudget()
	if mt, ok := c.Budget["max_turns"]; ok {
		budget.MaxTurns = asInt(mt)
	}

	gate := ApprovalFunc(func(ApprovalRequest) bool { return approvals == "allow" })
	rep, err := RunWith(model, reg, c.Goal, c.Verb == "run", RunOptions{
		Autonomy: autonomy,
		Approve:  gate,
		Budget:   budget,
	})
	if err != nil {
		t.Fatalf("run: %v", err)
	}

	if want, ok := c.Expect["outcome"].(string); ok && rep.Outcome != want {
		t.Errorf("outcome = %q, want %q", rep.Outcome, want)
	}
	if want, ok := c.Expect["reason"].(string); ok && rep.Reason != want {
		t.Errorf("reason = %q, want %q", rep.Reason, want)
	}
	if raw, ok := c.Expect["executed"]; ok {
		want := asStrings(raw)
		if !equalStrings(rep.Executed, want) {
			t.Errorf("executed = %v, want %v", rep.Executed, want)
		}
	}
	if want, ok := c.Expect["audited"].(bool); ok && rep.Audited != want {
		t.Errorf("audited = %v, want %v", rep.Audited, want)
	}
}

func scriptHasProgram(script []map[string]any) bool {
	for _, step := range script {
		if _, ok := step["program"]; ok {
			return true
		}
	}
	return false
}

func scriptModel(script []map[string]any) *FakeModel {
	var responses []ModelResponse
	for i, step := range script {
		id := "call_" + itoa(i)
		switch {
		case has(step, "final"):
			responses = append(responses, FinalResponse(asString(step["final"])))
		case has(step, "call"):
			responses = append(responses, CallResponse(id, asString(step["call"]), asArgs(step["args"])))
		default:
			// program steps are filtered out earlier; anything else is a bad case.
		}
	}
	return NewFakeModel(responses...)
}

// --- small YAML-value helpers ----------------------------------------------------

func has(m map[string]any, key string) bool { _, ok := m[key]; return ok }

func asString(v any) string {
	s, _ := v.(string)
	return s
}

func asArgs(v any) map[string]any {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return map[string]any{}
}

func asInt(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	default:
		return 0
	}
}

func asStrings(v any) []string {
	items, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(items))
	for _, it := range items {
		out = append(out, asString(it))
	}
	return out
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var buf []byte
	neg := i < 0
	if neg {
		i = -i
	}
	for i > 0 {
		buf = append([]byte{byte('0' + i%10)}, buf...)
		i /= 10
	}
	if neg {
		buf = append([]byte{'-'}, buf...)
	}
	return string(buf)
}
