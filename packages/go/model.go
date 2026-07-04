package reins

import "fmt"

// ModelParams are the knobs for one model call. An empty Model lets the adapter
// pick its default.
type ModelParams struct {
	Model     string
	MaxTokens int
}

// DefaultModelParams returns sane defaults (MaxTokens 1024), matching the Python port.
func DefaultModelParams() ModelParams {
	return ModelParams{MaxTokens: 1024}
}

// Model is the single stochastic call. Implementations MUST be stateless with
// respect to inputs — the harness owns the conversation history. The adapter is
// handed the tool surface (intent-level ToolSpec only, never policy metadata,
// invariant §2.5) and returns one ModelResponse. Control-plane failures return an
// error; capability failures flow back as data (CapabilityResult) instead.
type Model interface {
	Complete(messages []Message, tools []ToolSpec, params ModelParams) (ModelResponse, error)
}

// FakeCall records exactly what the FakeModel was shown on one Complete call, so
// a test can assert the messages and tool surface the model saw.
type FakeCall struct {
	Messages []Message
	Tools    []ToolSpec
}

// FakeModel is a scriptable, deterministic Model for tests (CLAUDE.md §3). It
// replays a fixed list of ModelResponse in order; every call is recorded in Calls.
// It is the determinism boundary made concrete — the harness is tested against it,
// never a live LLM.
type FakeModel struct {
	Responses []ModelResponse
	Calls     []FakeCall
	index     int
}

// NewFakeModel builds a FakeModel that replays the given responses in order.
func NewFakeModel(responses ...ModelResponse) *FakeModel {
	return &FakeModel{Responses: responses}
}

// Complete records the inputs, then returns the next scripted response, or a
// hinted error once the script is exhausted.
func (f *FakeModel) Complete(messages []Message, tools []ToolSpec, _ ModelParams) (ModelResponse, error) {
	f.Calls = append(f.Calls, FakeCall{Messages: cloneMessages(messages), Tools: cloneTools(tools)})
	if f.index >= len(f.Responses) {
		return ModelResponse{}, NewError(
			fmt.Sprintf("FakeModel script exhausted after %d response(s)", len(f.Responses)),
			"add more responses to the FakeModel, or check the loop is terminating",
		)
	}
	resp := f.Responses[f.index]
	f.index++
	return resp, nil
}

// FinalResponse is a scripted final answer (finish reason Stop).
func FinalResponse(text string) ModelResponse {
	return ModelResponse{
		Message:      Message{Role: RoleAssistant, Text: text},
		FinishReason: FinishStop,
	}
}

// CallResponse is a scripted request to call one capability (finish reason ToolCalls).
func CallResponse(id, name string, args map[string]any) ModelResponse {
	return ModelResponse{
		Message: Message{
			Role:      RoleAssistant,
			ToolCalls: []ToolCall{{ID: id, Name: name, Arguments: args}},
		},
		FinishReason: FinishToolCalls,
	}
}

// ProgramResponse is a scripted code-mode program run (a run_program tool call).
func ProgramResponse(id, code string) ModelResponse {
	return CallResponse(id, "run_program", map[string]any{"code": code})
}

// Compile-time check that FakeModel satisfies Model.
var _ Model = (*FakeModel)(nil)

func cloneMessages(in []Message) []Message {
	out := make([]Message, len(in))
	copy(out, in)
	return out
}

func cloneTools(in []ToolSpec) []ToolSpec {
	out := make([]ToolSpec, len(in))
	copy(out, in)
	return out
}
