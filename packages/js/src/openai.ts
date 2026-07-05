import type { Message, ModelParams, ModelResponse, ToolCall, ToolSpec, Usage } from './types.js';
import type { Model } from './model.js';
import { defaultModelParams } from './types.js';
import { ReinsError } from './errors.js';

/**
 * OpenAI-compatible model adapter (the TypeScript mirror of the Go and Python
 * ports). One adapter, many providers: the Chat Completions dialect is the de-facto
 * wire format, so this single type serves OpenAI, Groq, vLLM, Ollama, Z.ai (GLM),
 * and Gemini's compat endpoint — only base URL and API key differ. Built on the
 * global `fetch`, so the core package needs no external dependency. It sends the
 * model only each capability's intent surface ({@link ToolSpec}) — never policy
 * metadata (§2.5).
 */

/** The slice of `fetch` the adapter needs; injectable for tests. */
export type FetchLike = (
  url: string,
  init: { method: string; headers: Record<string, string>; body: string },
) => Promise<{ ok: boolean; status: number; text(): Promise<string> }>;

const defaultFetch: FetchLike = (url, init) => fetch(url, init);

/** Options for {@link OpenAICompatModel}. */
export interface OpenAIOptions {
  baseURL?: string;
  apiKey?: string;
  /** Inject an HTTP client (for tests or custom transports). */
  fetch?: FetchLike;
}

/** A {@link Model} backed by any OpenAI-compatible Chat Completions API. */
export class OpenAICompatModel implements Model {
  readonly model: string;
  readonly baseURL: string;
  private readonly apiKey: string;
  private readonly fetchFn: FetchLike;
  // api.openai.com wants max_completion_tokens; compat servers want max_tokens.
  private readonly useMaxCompletionTokens: boolean;

  constructor(model: string, options: OpenAIOptions = {}) {
    this.model = model;
    this.baseURL = (options.baseURL ?? 'https://api.openai.com/v1').replace(/\/+$/, '');
    this.apiKey = options.apiKey ?? '';
    this.fetchFn = options.fetch ?? defaultFetch;
    this.useMaxCompletionTokens = this.baseURL.includes('api.openai.com');
  }

  async complete(
    messages: Message[],
    tools: ToolSpec[],
    params: ModelParams,
  ): Promise<ModelResponse> {
    const maxTokens = params.max_tokens || defaultModelParams().max_tokens;
    const body: Record<string, unknown> = {
      model: params.model || this.model,
      messages: toChatMessages(messages),
    };
    body[this.useMaxCompletionTokens ? 'max_completion_tokens' : 'max_tokens'] = maxTokens;
    if (tools.length > 0) {
      body['tools'] = toChatTools(tools);
    }

    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (this.apiKey) {
      headers['Authorization'] = `Bearer ${this.apiKey}`;
    }

    let resp: { ok: boolean; status: number; text(): Promise<string> };
    try {
      resp = await this.fetchFn(`${this.baseURL}/chat/completions`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
      });
    } catch (e) {
      throw new ReinsError(
        `the model request failed: ${e instanceof Error ? e.message : String(e)}`,
        'check the network and the base URL',
      );
    }
    const payload = await resp.text();
    if (!resp.ok) {
      throw new ReinsError(
        `the provider returned ${resp.status}: ${truncate(payload, 200)}`,
        'check the API key, model name, and base URL',
      );
    }
    return parseChatResponse(payload);
  }
}

// --- request shaping ---------------------------------------------------------------

function toChatTools(tools: ToolSpec[]): unknown[] {
  return tools.map((t) => ({
    type: 'function',
    function: {
      name: t.name,
      description: t.description,
      parameters: t.input_schema ?? { type: 'object', properties: {} },
    },
  }));
}

function toChatMessages(messages: Message[]): unknown[] {
  return messages.map((msg) => {
    switch (msg.role) {
      case 'system':
        return { role: 'system', content: msg.text ?? '' };
      case 'user':
        return { role: 'user', content: msg.text ?? '' };
      case 'tool':
        return { role: 'tool', tool_call_id: msg.tool_call_id ?? '', content: msg.text ?? '' };
      case 'assistant': {
        const turn: Record<string, unknown> = { role: 'assistant', content: msg.text ?? '' };
        if (msg.tool_calls && msg.tool_calls.length > 0) {
          turn['tool_calls'] = msg.tool_calls.map((tc) => ({
            id: tc.id,
            type: 'function',
            function: { name: tc.name, arguments: JSON.stringify(tc.arguments ?? {}) },
          }));
        }
        return turn;
      }
    }
  });
}

// --- response parsing --------------------------------------------------------------

const finishMap: Record<string, ModelResponse['finish_reason']> = {
  stop: 'stop',
  tool_calls: 'tool_calls',
  function_call: 'tool_calls',
  length: 'length',
  content_filter: 'error',
};

interface RawChatResponse {
  choices?: Array<{
    message?: {
      content?: string;
      tool_calls?: Array<{ id?: string; function?: { name?: string; arguments?: string } }>;
    };
    finish_reason?: string;
  }>;
  usage?: { prompt_tokens?: number; completion_tokens?: number };
}

function parseChatResponse(payload: string): ModelResponse {
  let cr: RawChatResponse;
  try {
    cr = JSON.parse(payload) as RawChatResponse;
  } catch {
    throw new ReinsError(
      'the provider returned unparseable JSON',
      'retry; if it persists the endpoint is not OpenAI-compatible',
    );
  }
  const choice = cr.choices?.[0];
  if (!choice) {
    throw new ReinsError('the provider returned no choices', 'retry the run');
  }

  const toolCalls: ToolCall[] = [];
  for (const tc of choice.message?.tool_calls ?? []) {
    let args: Record<string, unknown> = {};
    const raw = tc.function?.arguments;
    if (raw && raw.trim() !== '') {
      try {
        args = JSON.parse(raw) as Record<string, unknown>;
      } catch {
        throw new ReinsError(
          `provider returned malformed tool arguments for "${tc.function?.name ?? ''}"`,
          'retry; if it persists the model does not emit valid JSON',
        );
      }
    }
    toolCalls.push({ id: tc.id ?? '', name: tc.function?.name ?? '', arguments: args });
  }

  let finish = finishMap[choice.finish_reason ?? ''] ?? 'stop';
  if (toolCalls.length > 0) {
    finish = 'tool_calls'; // some compat servers report "stop" alongside calls
  }

  const usage: Usage = {
    input_tokens: cr.usage?.prompt_tokens ?? 0,
    output_tokens: cr.usage?.completion_tokens ?? 0,
    cost: 0,
  };
  const message: Message = { role: 'assistant', text: choice.message?.content ?? '' };
  if (toolCalls.length > 0) {
    message.tool_calls = toolCalls;
  }
  return { message, finish_reason: finish, usage };
}

// --- provider registry + resolution ------------------------------------------------

interface ProviderSpec {
  name: string;
  envKey?: string;
  baseURL: string;
  baseURLEnv?: string;
  defaultModel?: string;
  placeholderKey?: string;
}

/** The built-in table of OpenAI-compatible providers. */
export const Providers: Record<string, ProviderSpec> = {
  openai: {
    name: 'openai',
    envKey: 'OPENAI_API_KEY',
    baseURL: 'https://api.openai.com/v1',
    baseURLEnv: 'OPENAI_BASE_URL',
    defaultModel: 'gpt-5.1',
  },
  gemini: {
    name: 'gemini',
    envKey: 'GEMINI_API_KEY',
    baseURL: 'https://generativelanguage.googleapis.com/v1beta/openai',
    defaultModel: 'gemini-2.5-flash',
  },
  groq: {
    name: 'groq',
    envKey: 'GROQ_API_KEY',
    baseURL: 'https://api.groq.com/openai/v1',
    defaultModel: 'llama-3.3-70b-versatile',
  },
  zai: {
    name: 'zai',
    envKey: 'ZAI_API_KEY',
    baseURL: 'https://api.z.ai/api/paas/v4',
    defaultModel: 'glm-4.6',
  },
  ollama: {
    name: 'ollama',
    baseURL: 'http://localhost:11434/v1',
    baseURLEnv: 'OLLAMA_BASE_URL',
    placeholderKey: 'ollama',
  },
  vllm: {
    name: 'vllm',
    envKey: 'VLLM_API_KEY',
    baseURL: 'http://localhost:8000/v1',
    baseURLEnv: 'VLLM_BASE_URL',
    placeholderKey: 'EMPTY',
  },
};

const providerAliases: Record<string, string> = { glm: 'zai', google: 'gemini' };

const namePrefixes: Record<string, string> = {
  gpt: 'openai',
  o1: 'openai',
  o3: 'openai',
  o4: 'openai',
  gemini: 'gemini',
  glm: 'zai',
};

const detectOrder = ['openai', 'gemini', 'groq', 'zai', 'ollama', 'vllm'];

/**
 * Turn a model string into a {@link Model}. Accepts `"provider:model"`
 * (`openai:gpt-4o`, `ollama:llama3.1`), a bare provider (`openai`), or a bare model
 * name with a known prefix (`gpt-4o`, `glm-4.6`). Anthropic's native API is a
 * different dialect — not yet ported; use an OpenAI-compatible provider.
 */
export function resolveModel(spec: string): Model {
  const colon = spec.indexOf(':');
  const providerPart = colon >= 0 ? spec.slice(0, colon) : spec;
  const modelPart = colon >= 0 ? spec.slice(colon + 1) : '';
  let key = providerPart.toLowerCase();
  key = providerAliases[key] ?? key;

  if (key === 'anthropic' || key === 'claude' || spec.toLowerCase().startsWith('claude')) {
    throw new ReinsError(
      'the Anthropic native API is not yet ported to TypeScript',
      'use an OpenAI-compatible provider (openai:, groq:, gemini:, zai:, ollama:, vllm:)',
    );
  }
  const provider = Providers[key];
  if (provider) {
    return buildProvider(provider, modelPart);
  }
  if (modelPart === '') {
    // Maybe a bare model name like "gpt-4o".
    const dash = spec.indexOf('-');
    const family = (dash >= 0 ? spec.slice(0, dash) : spec).toLowerCase();
    const prov = namePrefixes[family];
    if (prov) {
      return buildProvider(Providers[prov]!, spec);
    }
  }
  throw new ReinsError(
    `unknown model spec "${spec}"`,
    `use 'provider:model' with a provider from ${providerNames()} (e.g. 'openai:gpt-4o', 'ollama:llama3.1')`,
  );
}

/**
 * Resolve a {@link Model} from the environment (OPENAI_API_KEY → GEMINI → GROQ →
 * ZAI → OLLAMA_BASE_URL → VLLM_BASE_URL). Throws a hinted error if none is set.
 */
export function defaultModel(): Model {
  for (const name of detectOrder) {
    const p = Providers[name]!;
    if (p.envKey && process.env[p.envKey]) {
      return buildProvider(p, '');
    }
    if (p.baseURLEnv && process.env[p.baseURLEnv]) {
      return buildProvider(p, '');
    }
  }
  throw new ReinsError(
    'no model provider configured',
    'set OPENAI_API_KEY / GEMINI_API_KEY / GROQ_API_KEY / ZAI_API_KEY (or OLLAMA_BASE_URL / VLLM_BASE_URL), or pass a Model',
  );
}

function buildProvider(p: ProviderSpec, model: string): Model {
  const chosen = model || p.defaultModel;
  if (!chosen) {
    throw new ReinsError(
      `provider "${p.name}" needs an explicit model name`,
      `local servers have no default — use '${p.name}:<model>'`,
    );
  }
  const baseURL = (p.baseURLEnv && process.env[p.baseURLEnv]) || p.baseURL;
  let apiKey = p.envKey ? (process.env[p.envKey] ?? '') : '';
  if (!apiKey && p.placeholderKey) {
    apiKey = p.placeholderKey;
  }
  if (!apiKey && p.envKey) {
    throw new ReinsError(
      `${p.envKey} is not set (needed for "${p.name}")`,
      `export ${p.envKey}=..., or pick another provider/model`,
    );
  }
  return new OpenAICompatModel(chosen, { baseURL, apiKey });
}

function providerNames(): string {
  return Object.keys(Providers).sort().join(', ');
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + '…' : s;
}
