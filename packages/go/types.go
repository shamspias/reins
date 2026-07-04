// Package reins is the Go port of the Reins agent harness: you point it at
// functions (or ORM models) you already have and hand it a goal in plain
// language, and it operates your app through your own code — safely and cheaply.
//
// This file holds the core value types (M6.1), mirroring the Python package and
// the language-neutral contract in spec/contract.md. The same names exist in the
// Python and TypeScript ports (invariant §2.21), and the shared conformance suite
// (spec/conformance) locks their behavior across all three.
package reins

// Role identifies who produced a Message.
type Role string

const (
	RoleSystem    Role = "system"
	RoleUser      Role = "user"
	RoleAssistant Role = "assistant"
	RoleTool      Role = "tool"
)

// FinishReason says why a model turn (or a whole run) ended.
type FinishReason string

const (
	FinishStop        FinishReason = "stop"
	FinishToolCalls   FinishReason = "tool_calls"
	FinishLength      FinishReason = "length"
	FinishInterrupted FinishReason = "interrupted"
	FinishError       FinishReason = "error"
)

// Access is a capability's danger classification, ordered by danger. The harness
// defaults to Write when classification is ambiguous (invariant §2.4) — never Read.
type Access string

const (
	AccessRead        Access = "read"
	AccessWrite       Access = "write"
	AccessDestructive Access = "destructive"
)

// ToolCall is a model's request to call one capability, with its JSON arguments.
type ToolCall struct {
	ID        string         `json:"id"`
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments,omitempty"`
}

// Message is one turn in the conversation (system / user / assistant / tool).
type Message struct {
	Role       Role           `json:"role"`
	Text       string         `json:"text,omitempty"`
	ToolCalls  []ToolCall     `json:"tool_calls,omitempty"`
	ToolCallID string         `json:"tool_call_id,omitempty"`
	Meta       map[string]any `json:"meta,omitempty"`
}

// Usage is token + cost accounting for a model call (or a cumulative total).
type Usage struct {
	InputTokens  int     `json:"input_tokens"`
	OutputTokens int     `json:"output_tokens"`
	Cost         float64 `json:"cost"`
}

// ModelResponse is one model turn: the assistant message, why it stopped, its usage.
type ModelResponse struct {
	Message      Message      `json:"message"`
	FinishReason FinishReason `json:"finish_reason"`
	Usage        Usage        `json:"usage"`
}

// ToolSpec is a capability's intent surface — the ONLY shape that crosses to the
// model (invariant §2.5): name, description, input schema; never policy metadata.
type ToolSpec struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"input_schema,omitempty"`
}

// Capability describes one operation the agent may perform: a typed, intent-named
// function plus safety metadata. InputSchema is a JSON Schema object.
type Capability struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"input_schema,omitempty"`
	Access      Access         `json:"access"`
	Confirm     bool           `json:"confirm"`
	Idempotent  bool           `json:"idempotent"`
	Scope       string         `json:"scope,omitempty"`
}

// ToolSpec returns the intent surface shown to the model — policy metadata
// (Access/Confirm/Scope) stays harness-side.
func (c Capability) ToolSpec() ToolSpec {
	return ToolSpec{Name: c.Name, Description: c.Description, InputSchema: c.InputSchema}
}

// CapabilityResult is errors-as-data: the outcome of executing a capability. On
// success OK is true and Value holds the result; on failure OK is false and Error
// explains why. The model sees this and adapts — capability failures never abort
// the loop (ENGINEERING.md §2).
type CapabilityResult struct {
	OK        bool   `json:"ok"`
	Value     any    `json:"value,omitempty"`
	Error     string `json:"error,omitempty"`
	Retryable bool   `json:"retryable,omitempty"`
}

// Context is who a run acts for, and its trace identity (invariants §2.7–§2.8).
// Principal is set once at the entry point and propagated into every capability
// call; the model can never set or spoof it.
type Context struct {
	Principal string `json:"principal,omitempty"`
	TraceID   string `json:"trace_id,omitempty"`
}

// RunResult is the final result of a run: output message, why it ended, usage, trace id.
type RunResult struct {
	Output  Message      `json:"output"`
	Reason  FinishReason `json:"reason"`
	Usage   Usage        `json:"usage"`
	TraceID string       `json:"trace_id"`
}

// RunState is the evolving state of a run, passed to stop conditions.
type RunState struct {
	Messages        []Message      `json:"messages,omitempty"`
	Turn            int            `json:"turn"`
	CumulativeUsage Usage          `json:"cumulative_usage"`
	LastResponse    *ModelResponse `json:"last_response,omitempty"`
}

// TraceEvent is one recorded step of a run, for explain()/--trace (M5.1). Kind is
// a short tag; Detail is the human-readable line (capability arguments redacted);
// OK marks whether the step succeeded.
type TraceEvent struct {
	Kind   string `json:"kind"`
	Detail string `json:"detail"`
	OK     bool   `json:"ok"`
}
