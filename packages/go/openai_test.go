package reins

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

// fakeTransport returns a canned response and records the request body, so the
// adapter is tested with no network (the determinism boundary).
type fakeTransport struct {
	status   int
	body     string
	captured map[string]any
}

func (f *fakeTransport) Do(req *http.Request) (*http.Response, error) {
	raw, _ := io.ReadAll(req.Body)
	f.captured = map[string]any{}
	_ = json.Unmarshal(raw, &f.captured)
	status := f.status
	if status == 0 {
		status = 200
	}
	return &http.Response{
		StatusCode: status,
		Body:       io.NopCloser(strings.NewReader(f.body)),
		Header:     make(http.Header),
	}, nil
}

func chatJSON(content string, finish string) string {
	return `{"choices":[{"message":{"content":"` + content + `"},"finish_reason":"` + finish +
		`"}],"usage":{"prompt_tokens":11,"completion_tokens":7}}`
}

func TestOpenAIMapsTextResponse(t *testing.T) {
	ft := &fakeTransport{body: chatJSON("hello", "stop")}
	m := NewOpenAICompatModel("test", WithHTTPClient(ft))
	out, err := m.Complete([]Message{{Role: RoleUser, Text: "hi"}}, nil, DefaultModelParams())
	if err != nil {
		t.Fatalf("complete: %v", err)
	}
	if out.Message.Text != "hello" || out.FinishReason != FinishStop {
		t.Errorf("mapped response = %+v", out)
	}
	if out.Usage.InputTokens != 11 || out.Usage.OutputTokens != 7 {
		t.Errorf("usage = %+v", out.Usage)
	}
}

func TestOpenAIMapsToolCalls(t *testing.T) {
	body := `{"choices":[{"message":{"tool_calls":[{"id":"c1","function":{"name":"refund_order","arguments":"{\"order_id\":8842}"}}]},"finish_reason":"tool_calls"}],"usage":{}}`
	ft := &fakeTransport{body: body}
	m := NewOpenAICompatModel("test", WithHTTPClient(ft))
	out, _ := m.Complete(nil, nil, DefaultModelParams())
	if out.FinishReason != FinishToolCalls || len(out.Message.ToolCalls) != 1 {
		t.Fatalf("tool call mapping = %+v", out)
	}
	tc := out.Message.ToolCalls[0]
	if tc.Name != "refund_order" || toInt(tc.Arguments["order_id"]) != 8842 {
		t.Errorf("tool call = %+v", tc)
	}
}

func TestOpenAIToolCallsWinOverStopFinish(t *testing.T) {
	body := `{"choices":[{"message":{"tool_calls":[{"id":"c1","function":{"name":"f","arguments":"{}"}}]},"finish_reason":"stop"}]}`
	m := NewOpenAICompatModel("test", WithHTTPClient(&fakeTransport{body: body}))
	out, _ := m.Complete(nil, nil, DefaultModelParams())
	if out.FinishReason != FinishToolCalls {
		t.Errorf("tool calls should force ToolCalls finish, got %v", out.FinishReason)
	}
}

func TestOpenAIMalformedToolArgsIsHintedError(t *testing.T) {
	body := `{"choices":[{"message":{"tool_calls":[{"id":"c1","function":{"name":"f","arguments":"{not json"}}]},"finish_reason":"tool_calls"}]}`
	m := NewOpenAICompatModel("test", WithHTTPClient(&fakeTransport{body: body}))
	_, err := m.Complete(nil, nil, DefaultModelParams())
	if err == nil || !strings.Contains(err.Error(), "→") {
		t.Errorf("malformed args should be a hinted error, got %v", err)
	}
}

func TestOpenAISendsOnlyIntentSurface(t *testing.T) {
	ft := &fakeTransport{body: chatJSON("ok", "stop")}
	m := NewOpenAICompatModel("test", WithHTTPClient(ft))
	// A ToolSpec carries only name/description/input_schema by construction (§2.5).
	spec := Capability{
		Name: "delete_order", Description: "Delete an order.",
		InputSchema: map[string]any{"type": "object"},
		Access:      AccessDestructive, Confirm: true, Scope: "user",
	}.ToolSpec()
	m.Complete([]Message{{Role: RoleUser, Text: "x"}}, []ToolSpec{spec}, DefaultModelParams())
	tools, _ := ft.captured["tools"].([]any)
	if len(tools) != 1 {
		t.Fatalf("expected 1 tool in request, got %v", ft.captured["tools"])
	}
	fn := tools[0].(map[string]any)["function"].(map[string]any)
	for _, leaked := range []string{"access", "confirm", "scope"} {
		if _, ok := fn[leaked]; ok {
			t.Errorf("policy field %q leaked to the wire", leaked)
		}
	}
	if fn["name"] != "delete_order" {
		t.Errorf("tool name = %v", fn["name"])
	}
}

func TestOpenAIErrorStatusIsHinted(t *testing.T) {
	m := NewOpenAICompatModel("test", WithHTTPClient(&fakeTransport{status: 401, body: `{"error":"bad key"}`}))
	_, err := m.Complete(nil, nil, DefaultModelParams())
	if err == nil || !strings.Contains(err.Error(), "→") {
		t.Errorf("4xx should be a hinted error, got %v", err)
	}
}

func TestBaseURLUsesMaxTokensNotCompletion(t *testing.T) {
	ft := &fakeTransport{body: chatJSON("ok", "stop")}
	m := NewOpenAICompatModel("test", WithBaseURL("http://localhost:11434/v1"), WithHTTPClient(ft))
	m.Complete(nil, nil, ModelParams{MaxTokens: 99})
	if _, ok := ft.captured["max_completion_tokens"]; ok {
		t.Error("compat servers should get max_tokens, not max_completion_tokens")
	}
	if toInt(ft.captured["max_tokens"]) != 99 {
		t.Errorf("max_tokens = %v", ft.captured["max_tokens"])
	}
}

// --- resolution ------------------------------------------------------------------

func TestResolveProviderColonModel(t *testing.T) {
	t.Setenv("GROQ_API_KEY", "gk")
	m, err := ResolveModel("groq:llama-3.3-70b-versatile")
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	oc := m.(*OpenAICompatModel)
	if oc.baseURL != "https://api.groq.com/openai/v1" || oc.model != "llama-3.3-70b-versatile" {
		t.Errorf("resolved = %+v", oc)
	}
}

func TestResolveBareModelNameSniffsFamily(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", "ok")
	t.Setenv("ZAI_API_KEY", "zk")
	if _, err := ResolveModel("gpt-4o"); err != nil {
		t.Errorf("gpt-4o should resolve to openai: %v", err)
	}
	if _, err := ResolveModel("glm-4.6"); err != nil {
		t.Errorf("glm-4.6 should resolve to zai: %v", err)
	}
}

func TestResolveOllamaNeedsModelAndUsesPlaceholderKey(t *testing.T) {
	if _, err := ResolveModel("ollama"); err == nil {
		t.Error("bare ollama should need an explicit model")
	}
	m, err := ResolveModel("ollama:llama3.1")
	if err != nil {
		t.Fatalf("resolve ollama: %v", err)
	}
	oc := m.(*OpenAICompatModel)
	if oc.baseURL != "http://localhost:11434/v1" || oc.apiKey != "ollama" {
		t.Errorf("ollama model = %+v", oc)
	}
}

func TestResolveMissingKeyIsHinted(t *testing.T) {
	// No OPENAI_API_KEY set → our hinted error, not the provider's raw 401.
	t.Setenv("OPENAI_API_KEY", "")
	_, err := ResolveModel("openai:gpt-4o")
	if err == nil || !strings.Contains(err.Error(), "OPENAI_API_KEY") {
		t.Errorf("missing key should be hinted, got %v", err)
	}
}

func TestResolveAnthropicIsAClearNotYet(t *testing.T) {
	if _, err := ResolveModel("anthropic:claude-opus-4-8"); err == nil || !strings.Contains(err.Error(), "→") {
		t.Errorf("anthropic should return a clear not-yet error, got %v", err)
	}
	if _, err := ResolveModel("claude-opus-4-8"); err == nil {
		t.Error("bare claude name should also route to the not-yet error")
	}
}

func TestResolveUnknownIsHinted(t *testing.T) {
	if _, err := ResolveModel("banana:split"); err == nil || !strings.Contains(err.Error(), "provider") {
		t.Errorf("unknown provider should be hinted, got %v", err)
	}
}

func TestDefaultModelAutoDetects(t *testing.T) {
	for _, k := range []string{"OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "ZAI_API_KEY", "OLLAMA_BASE_URL", "VLLM_BASE_URL"} {
		t.Setenv(k, "")
	}
	if _, err := DefaultModel(); err == nil {
		t.Error("no provider configured should error")
	}
	t.Setenv("GROQ_API_KEY", "gk")
	m, err := DefaultModel()
	if err != nil {
		t.Fatalf("default: %v", err)
	}
	if m.(*OpenAICompatModel).model != Providers["groq"].defaultModel {
		t.Errorf("expected groq default model, got %+v", m)
	}
}

func TestOpenAIIsAModel(t *testing.T) {
	var _ Model = NewOpenAICompatModel("x")
}

// toInt coerces a JSON number (float64 after unmarshal) to int for assertions.
func toInt(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	default:
		return -1
	}
}
