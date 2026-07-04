package reins

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
)

// OpenAI-compatible model adapter (Go port of Python's M2.5). One adapter, many
// providers: the Chat Completions dialect is the de-facto wire format, so this
// single type serves OpenAI, Groq, vLLM, Ollama, Z.ai (GLM), and Gemini's compat
// endpoint — only base URL and API key differ. Built on net/http, so the core
// package needs no external dependency. It sends the model only each capability's
// intent surface (ToolSpec) — never policy metadata (§2.5).

// HTTPDoer is the slice of *http.Client the adapter needs; injectable for tests.
type HTTPDoer interface {
	Do(req *http.Request) (*http.Response, error)
}

// OpenAICompatModel is a Model backed by any OpenAI-compatible Chat Completions API.
type OpenAICompatModel struct {
	model    string
	apiKey   string
	baseURL  string // no trailing slash; e.g. https://api.groq.com/openai/v1
	client   HTTPDoer
	useMaxCT bool // api.openai.com wants max_completion_tokens; compat servers want max_tokens
}

// OpenAIOption configures an OpenAICompatModel.
type OpenAIOption func(*OpenAICompatModel)

// WithBaseURL points the adapter at a specific endpoint (e.g. a local Ollama).
func WithBaseURL(url string) OpenAIOption {
	return func(m *OpenAICompatModel) { m.baseURL = strings.TrimRight(url, "/"); m.useMaxCT = false }
}

// WithAPIKey sets the bearer token.
func WithAPIKey(key string) OpenAIOption { return func(m *OpenAICompatModel) { m.apiKey = key } }

// WithHTTPClient injects the HTTP client (for tests or custom transports).
func WithHTTPClient(c HTTPDoer) OpenAIOption { return func(m *OpenAICompatModel) { m.client = c } }

// NewOpenAICompatModel builds an adapter for the given model. Defaults to
// api.openai.com; use WithBaseURL for other providers.
func NewOpenAICompatModel(model string, opts ...OpenAIOption) *OpenAICompatModel {
	m := &OpenAICompatModel{
		model:    model,
		baseURL:  "https://api.openai.com/v1",
		client:   http.DefaultClient,
		useMaxCT: true,
	}
	for _, opt := range opts {
		opt(m)
	}
	return m
}

var finishMap = map[string]FinishReason{
	"stop":           FinishStop,
	"tool_calls":     FinishToolCalls,
	"function_call":  FinishToolCalls,
	"length":         FinishLength,
	"content_filter": FinishError,
}

func (m *OpenAICompatModel) Complete(messages []Message, tools []ToolSpec, params ModelParams) (ModelResponse, error) {
	model := m.model
	if params.Model != "" {
		model = params.Model
	}
	body := map[string]any{
		"model":    model,
		"messages": toChatMessages(messages),
	}
	maxTokens := params.MaxTokens
	if maxTokens == 0 {
		maxTokens = DefaultModelParams().MaxTokens
	}
	if m.useMaxCT {
		body["max_completion_tokens"] = maxTokens
	} else {
		body["max_tokens"] = maxTokens
	}
	if len(tools) > 0 {
		body["tools"] = toChatTools(tools)
	}

	raw, err := json.Marshal(body)
	if err != nil {
		return ModelResponse{}, NewError("could not encode the request", "this is a reins bug; please report it")
	}
	req, err := http.NewRequest(http.MethodPost, m.baseURL+"/chat/completions", bytes.NewReader(raw))
	if err != nil {
		return ModelResponse{}, NewError("could not build the request: "+err.Error(), "check the base URL")
	}
	req.Header.Set("Content-Type", "application/json")
	if m.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+m.apiKey)
	}
	resp, err := m.client.Do(req)
	if err != nil {
		return ModelResponse{}, NewError("the model request failed: "+err.Error(), "check the network and the base URL")
	}
	defer resp.Body.Close()
	payload, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return ModelResponse{}, NewError(
			fmt.Sprintf("the provider returned %d: %s", resp.StatusCode, truncate(string(payload), 200)),
			"check the API key, model name, and base URL",
		)
	}
	return parseChatResponse(payload)
}

// --- request shaping -------------------------------------------------------------

func toChatTools(tools []ToolSpec) []map[string]any {
	out := make([]map[string]any, 0, len(tools))
	for _, t := range tools {
		params := t.InputSchema
		if params == nil {
			params = map[string]any{"type": "object", "properties": map[string]any{}}
		}
		out = append(out, map[string]any{
			"type": "function",
			"function": map[string]any{
				"name":        t.Name,
				"description": t.Description,
				"parameters":  params,
			},
		})
	}
	return out
}

func toChatMessages(messages []Message) []map[string]any {
	out := make([]map[string]any, 0, len(messages))
	for _, msg := range messages {
		switch msg.Role {
		case RoleSystem:
			out = append(out, map[string]any{"role": "system", "content": msg.Text})
		case RoleUser:
			out = append(out, map[string]any{"role": "user", "content": msg.Text})
		case RoleTool:
			out = append(out, map[string]any{"role": "tool", "tool_call_id": msg.ToolCallID, "content": msg.Text})
		case RoleAssistant:
			turn := map[string]any{"role": "assistant", "content": msg.Text}
			if len(msg.ToolCalls) > 0 {
				calls := make([]map[string]any, 0, len(msg.ToolCalls))
				for _, tc := range msg.ToolCalls {
					args, _ := json.Marshal(tc.Arguments)
					calls = append(calls, map[string]any{
						"id":       tc.ID,
						"type":     "function",
						"function": map[string]any{"name": tc.Name, "arguments": string(args)},
					})
				}
				turn["tool_calls"] = calls
			}
			out = append(out, turn)
		}
	}
	return out
}

// --- response parsing ------------------------------------------------------------

type chatResponse struct {
	Choices []struct {
		Message struct {
			Content   string `json:"content"`
			ToolCalls []struct {
				ID       string `json:"id"`
				Function struct {
					Name      string `json:"name"`
					Arguments string `json:"arguments"`
				} `json:"function"`
			} `json:"tool_calls"`
		} `json:"message"`
		FinishReason string `json:"finish_reason"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
	} `json:"usage"`
}

func parseChatResponse(payload []byte) (ModelResponse, error) {
	var cr chatResponse
	if err := json.Unmarshal(payload, &cr); err != nil {
		return ModelResponse{}, NewError("the provider returned unparseable JSON", "retry; if it persists the endpoint is not OpenAI-compatible")
	}
	if len(cr.Choices) == 0 {
		return ModelResponse{}, NewError("the provider returned no choices", "retry the run")
	}
	choice := cr.Choices[0]
	var toolCalls []ToolCall
	for _, tc := range choice.Message.ToolCalls {
		args := map[string]any{}
		if strings.TrimSpace(tc.Function.Arguments) != "" {
			if err := json.Unmarshal([]byte(tc.Function.Arguments), &args); err != nil {
				return ModelResponse{}, NewError(
					fmt.Sprintf("provider returned malformed tool arguments for %q", tc.Function.Name),
					"retry; if it persists the model does not emit valid JSON",
				)
			}
		}
		toolCalls = append(toolCalls, ToolCall{ID: tc.ID, Name: tc.Function.Name, Arguments: args})
	}
	finish := finishMap[choice.FinishReason]
	if finish == "" {
		finish = FinishStop
	}
	if len(toolCalls) > 0 {
		finish = FinishToolCalls // some compat servers report "stop" alongside calls
	}
	return ModelResponse{
		Message:      Message{Role: RoleAssistant, Text: choice.Message.Content, ToolCalls: toolCalls},
		FinishReason: finish,
		Usage:        Usage{InputTokens: cr.Usage.PromptTokens, OutputTokens: cr.Usage.CompletionTokens},
	}, nil
}

// --- provider registry + resolution ----------------------------------------------

type providerSpec struct {
	name           string
	envKey         string
	baseURL        string
	baseURLEnv     string
	defaultModel   string
	placeholderKey string
}

// Providers is the built-in table of OpenAI-compatible providers.
var Providers = map[string]providerSpec{
	"openai": {name: "openai", envKey: "OPENAI_API_KEY", baseURL: "https://api.openai.com/v1", baseURLEnv: "OPENAI_BASE_URL", defaultModel: "gpt-5.1"},
	"gemini": {name: "gemini", envKey: "GEMINI_API_KEY", baseURL: "https://generativelanguage.googleapis.com/v1beta/openai", defaultModel: "gemini-2.5-flash"},
	"groq":   {name: "groq", envKey: "GROQ_API_KEY", baseURL: "https://api.groq.com/openai/v1", defaultModel: "llama-3.3-70b-versatile"},
	"zai":    {name: "zai", envKey: "ZAI_API_KEY", baseURL: "https://api.z.ai/api/paas/v4", defaultModel: "glm-4.6"},
	"ollama": {name: "ollama", baseURL: "http://localhost:11434/v1", baseURLEnv: "OLLAMA_BASE_URL", placeholderKey: "ollama"},
	"vllm":   {name: "vllm", envKey: "VLLM_API_KEY", baseURL: "http://localhost:8000/v1", baseURLEnv: "VLLM_BASE_URL", placeholderKey: "EMPTY"},
}

var providerAliases = map[string]string{"glm": "zai", "google": "gemini"}

var namePrefixes = map[string]string{
	"gpt": "openai", "o1": "openai", "o3": "openai", "o4": "openai",
	"gemini": "gemini", "glm": "zai",
}

var detectOrder = []string{"openai", "gemini", "groq", "zai", "ollama", "vllm"}

// ResolveModel turns a model string into a Model. Accepts "provider:model"
// ("openai:gpt-4o", "ollama:llama3.1"), a bare provider ("openai"), or a bare
// model name with a known prefix ("gpt-4o", "glm-4.6"). Anthropic's native API is
// a different dialect — not yet ported to Go; use an OpenAI-compatible provider.
func ResolveModel(spec string) (Model, error) {
	providerPart, modelPart, _ := strings.Cut(spec, ":")
	key := strings.ToLower(providerPart)
	if alias, ok := providerAliases[key]; ok {
		key = alias
	}
	if key == "anthropic" || key == "claude" || strings.HasPrefix(strings.ToLower(spec), "claude") {
		return nil, NewError("the Anthropic native API is not yet ported to Go", "use an OpenAI-compatible provider (openai:, groq:, gemini:, zai:, ollama:, vllm:)")
	}
	if p, ok := Providers[key]; ok {
		return buildProvider(p, modelPart)
	}
	if modelPart == "" { // maybe a bare model name like "gpt-4o"
		family := strings.ToLower(spec)
		if i := strings.IndexByte(family, '-'); i >= 0 {
			family = family[:i]
		}
		if prov, ok := namePrefixes[family]; ok {
			return buildProvider(Providers[prov], spec)
		}
	}
	return nil, NewError(
		fmt.Sprintf("unknown model spec %q", spec),
		"use 'provider:model' with a provider from "+providerNames()+" (e.g. 'openai:gpt-4o', 'ollama:llama3.1')",
	)
}

// DefaultModel resolves a Model from the environment (OPENAI_API_KEY → GEMINI →
// GROQ → ZAI → OLLAMA_BASE_URL → VLLM_BASE_URL). Returns a hinted error if none.
func DefaultModel() (Model, error) {
	for _, name := range detectOrder {
		p := Providers[name]
		if p.envKey != "" && os.Getenv(p.envKey) != "" {
			return buildProvider(p, "")
		}
		if p.baseURLEnv != "" && os.Getenv(p.baseURLEnv) != "" {
			return buildProvider(p, "")
		}
	}
	return nil, NewError(
		"no model provider configured",
		"set OPENAI_API_KEY / GEMINI_API_KEY / GROQ_API_KEY / ZAI_API_KEY (or OLLAMA_BASE_URL / VLLM_BASE_URL), or pass a Model",
	)
}

func buildProvider(p providerSpec, model string) (Model, error) {
	chosen := model
	if chosen == "" {
		chosen = p.defaultModel
	}
	if chosen == "" {
		return nil, NewError(
			fmt.Sprintf("provider %q needs an explicit model name", p.name),
			fmt.Sprintf("local servers have no default — use '%s:<model>'", p.name),
		)
	}
	baseURL := p.baseURL
	if p.baseURLEnv != "" && os.Getenv(p.baseURLEnv) != "" {
		baseURL = os.Getenv(p.baseURLEnv)
	}
	apiKey := ""
	if p.envKey != "" {
		apiKey = os.Getenv(p.envKey)
	}
	if apiKey == "" && p.placeholderKey != "" {
		apiKey = p.placeholderKey
	}
	if apiKey == "" && p.envKey != "" {
		return nil, NewError(
			fmt.Sprintf("%s is not set (needed for %q)", p.envKey, p.name),
			fmt.Sprintf("export %s=..., or pick another provider/model", p.envKey),
		)
	}
	return NewOpenAICompatModel(chosen, WithBaseURL(baseURL), WithAPIKey(apiKey)), nil
}

func providerNames() string {
	names := make([]string, 0, len(Providers))
	for name := range Providers {
		names = append(names, name)
	}
	sort.Strings(names)
	return fmt.Sprintf("%v", names)
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}

var _ Model = (*OpenAICompatModel)(nil)
