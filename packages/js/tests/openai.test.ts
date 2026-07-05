import { describe, it, expect, afterEach, vi } from 'vitest';
import {
  OpenAICompatModel,
  resolveModel,
  defaultModel,
  Providers,
  toolSpec,
  defaultModelParams,
  ReinsError,
  type FetchLike,
  type Model,
} from '../src/index.js';

/** A fetch that records the request body and returns a canned response (no network). */
function fakeFetch(responseBody: string, status = 200) {
  const captured: { url?: string; body?: Record<string, unknown> } = {};
  const fn: FetchLike = async (url, init) => {
    captured.url = url;
    captured.body = JSON.parse(init.body) as Record<string, unknown>;
    return { ok: status < 400, status, text: async () => responseBody };
  };
  return { fn, captured };
}

function chatJSON(content: string, finish: string): string {
  return JSON.stringify({
    choices: [{ message: { content }, finish_reason: finish }],
    usage: { prompt_tokens: 11, completion_tokens: 7 },
  });
}

afterEach(() => vi.unstubAllEnvs());

describe('OpenAICompatModel — request/response mapping', () => {
  it('maps a text response', async () => {
    const { fn } = fakeFetch(chatJSON('hello', 'stop'));
    const m = new OpenAICompatModel('test', { fetch: fn });
    const out = await m.complete([{ role: 'user', text: 'hi' }], [], defaultModelParams());
    expect(out.message.text).toBe('hello');
    expect(out.finish_reason).toBe('stop');
    expect(out.usage).toEqual({ input_tokens: 11, output_tokens: 7, cost: 0 });
  });

  it('maps tool calls', async () => {
    const body = JSON.stringify({
      choices: [
        {
          message: {
            tool_calls: [
              { id: 'c1', function: { name: 'refund_order', arguments: '{"order_id":8842}' } },
            ],
          },
          finish_reason: 'tool_calls',
        },
      ],
    });
    const { fn } = fakeFetch(body);
    const m = new OpenAICompatModel('test', { fetch: fn });
    const out = await m.complete([], [], defaultModelParams());
    expect(out.finish_reason).toBe('tool_calls');
    expect(out.message.tool_calls).toEqual([
      { id: 'c1', name: 'refund_order', arguments: { order_id: 8842 } },
    ]);
  });

  it('lets tool calls win over a "stop" finish', async () => {
    const body = JSON.stringify({
      choices: [
        {
          message: { tool_calls: [{ id: 'c1', function: { name: 'f', arguments: '{}' } }] },
          finish_reason: 'stop',
        },
      ],
    });
    const { fn } = fakeFetch(body);
    const out = await new OpenAICompatModel('test', { fetch: fn }).complete(
      [],
      [],
      defaultModelParams(),
    );
    expect(out.finish_reason).toBe('tool_calls');
  });

  it('reports malformed tool arguments as a hinted error', async () => {
    const body = JSON.stringify({
      choices: [
        {
          message: { tool_calls: [{ id: 'c1', function: { name: 'f', arguments: '{not json' } }] },
          finish_reason: 'tool_calls',
        },
      ],
    });
    const m = new OpenAICompatModel('test', { fetch: fakeFetch(body).fn });
    await expect(m.complete([], [], defaultModelParams())).rejects.toThrow('→');
  });

  it('sends only the intent surface — never policy metadata (§2.5)', async () => {
    const { fn, captured } = fakeFetch(chatJSON('ok', 'stop'));
    const m = new OpenAICompatModel('test', { fetch: fn });
    const spec = toolSpec({
      name: 'delete_order',
      description: 'Delete an order.',
      input_schema: { type: 'object' },
      access: 'destructive',
      confirm: true,
      idempotent: false,
      scope: 'user',
    });
    await m.complete([{ role: 'user', text: 'x' }], [spec], defaultModelParams());
    const tools = captured.body?.['tools'] as Array<{ function: Record<string, unknown> }>;
    expect(tools).toHaveLength(1);
    const fnObj = tools[0]!.function;
    expect(fnObj['name']).toBe('delete_order');
    for (const leaked of ['access', 'confirm', 'scope']) {
      expect(leaked in fnObj).toBe(false);
    }
  });

  it('surfaces a 4xx as a hinted error', async () => {
    const m = new OpenAICompatModel('test', { fetch: fakeFetch('{"error":"bad key"}', 401).fn });
    await expect(m.complete([], [], defaultModelParams())).rejects.toThrow('→');
  });

  it('uses max_tokens for compat servers and max_completion_tokens for OpenAI', async () => {
    const compat = fakeFetch(chatJSON('ok', 'stop'));
    await new OpenAICompatModel('m', {
      baseURL: 'http://localhost:11434/v1',
      fetch: compat.fn,
    }).complete([], [], { max_tokens: 99 });
    expect(compat.captured.body?.['max_completion_tokens']).toBeUndefined();
    expect(compat.captured.body?.['max_tokens']).toBe(99);

    const openai = fakeFetch(chatJSON('ok', 'stop'));
    await new OpenAICompatModel('m', { fetch: openai.fn }).complete([], [], { max_tokens: 99 });
    expect(openai.captured.body?.['max_completion_tokens']).toBe(99);
  });
});

describe('resolveModel', () => {
  const asModel = (m: Model) => m as OpenAICompatModel;

  it('resolves provider:model', () => {
    vi.stubEnv('GROQ_API_KEY', 'gk');
    const m = asModel(resolveModel('groq:llama-3.3-70b-versatile'));
    expect(m.baseURL).toBe('https://api.groq.com/openai/v1');
    expect(m.model).toBe('llama-3.3-70b-versatile');
  });

  it('sniffs a bare model name by family', () => {
    vi.stubEnv('OPENAI_API_KEY', 'ok');
    vi.stubEnv('ZAI_API_KEY', 'zk');
    expect(asModel(resolveModel('gpt-4o')).baseURL).toContain('api.openai.com');
    expect(asModel(resolveModel('glm-4.6')).baseURL).toContain('z.ai');
  });

  it('needs an explicit model for ollama and uses a placeholder key', () => {
    expect(() => resolveModel('ollama')).toThrow(/explicit model/);
    const m = asModel(resolveModel('ollama:llama3.1'));
    expect(m.baseURL).toBe('http://localhost:11434/v1');
    expect(m.model).toBe('llama3.1');
  });

  it('reports a missing key with a hint, not a raw 401', () => {
    vi.stubEnv('OPENAI_API_KEY', '');
    expect(() => resolveModel('openai:gpt-4o')).toThrow(/OPENAI_API_KEY/);
  });

  it('returns a clear not-yet error for Anthropic', () => {
    expect(() => resolveModel('anthropic:claude-opus-4')).toThrow('→');
    expect(() => resolveModel('claude-opus-4')).toThrow(ReinsError);
  });

  it('reports an unknown provider with a hint', () => {
    expect(() => resolveModel('banana:split')).toThrow(/provider/);
  });
});

describe('defaultModel', () => {
  it('auto-detects a provider from the environment', () => {
    for (const key of [
      'OPENAI_API_KEY',
      'GEMINI_API_KEY',
      'GROQ_API_KEY',
      'ZAI_API_KEY',
      'OLLAMA_BASE_URL',
      'VLLM_BASE_URL',
    ]) {
      vi.stubEnv(key, '');
    }
    expect(() => defaultModel()).toThrow(/no model provider/);

    vi.stubEnv('GROQ_API_KEY', 'gk');
    expect((defaultModel() as OpenAICompatModel).model).toBe(Providers['groq']!.defaultModel);
  });
});
