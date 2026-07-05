import type { Message, ModelParams, ModelResponse, ToolSpec } from './types.js';
import { ReinsError } from './errors.js';

/**
 * Model is the single stochastic call. Implementations MUST be stateless with
 * respect to inputs — the harness owns the conversation history. The adapter is
 * handed the tool surface (intent-level {@link ToolSpec} only, never policy
 * metadata, invariant §2.5) and returns one {@link ModelResponse}. It is async
 * because real adapters do network I/O; the harness awaits it. Control-plane
 * failures reject; capability failures flow back as data instead.
 */
export interface Model {
  complete(messages: Message[], tools: ToolSpec[], params: ModelParams): Promise<ModelResponse>;
}

/**
 * What the {@link FakeModel} was shown on one `complete()` call, so a test can
 * assert the messages and tool surface the model saw.
 */
export interface FakeCall {
  messages: Message[];
  tools: ToolSpec[];
}

/**
 * A scriptable, deterministic {@link Model} for tests (CLAUDE.md §3). It replays a
 * fixed list of {@link ModelResponse} in order; every call is recorded in `calls`.
 * This is the determinism boundary made concrete — the harness is tested against
 * it, never a live LLM.
 */
export class FakeModel implements Model {
  readonly responses: ModelResponse[];
  readonly calls: FakeCall[] = [];
  private index = 0;

  constructor(...responses: ModelResponse[]) {
    this.responses = responses;
  }

  complete(messages: Message[], tools: ToolSpec[], _params: ModelParams): Promise<ModelResponse> {
    this.calls.push({ messages: [...messages], tools: [...tools] });
    if (this.index >= this.responses.length) {
      return Promise.reject(
        new ReinsError(
          `FakeModel script exhausted after ${this.responses.length} response(s)`,
          'add more responses to the FakeModel, or check the loop is terminating',
        ),
      );
    }
    const resp = this.responses[this.index]!;
    this.index += 1;
    return Promise.resolve(resp);
  }
}

const noUsage = { input_tokens: 0, output_tokens: 0, cost: 0 };

/** A scripted final answer (finish reason `'stop'`). */
export function finalResponse(text: string): ModelResponse {
  return {
    message: { role: 'assistant', text },
    finish_reason: 'stop',
    usage: { ...noUsage },
  };
}

/** A scripted request to call one capability (finish reason `'tool_calls'`). */
export function callResponse(
  id: string,
  name: string,
  args: Record<string, unknown>,
): ModelResponse {
  return {
    message: {
      role: 'assistant',
      tool_calls: [{ id, name, arguments: args }],
    },
    finish_reason: 'tool_calls',
    usage: { ...noUsage },
  };
}

/** A scripted code-mode program run (a `run_program` tool call). */
export function programResponse(id: string, code: string): ModelResponse {
  return callResponse(id, 'run_program', { code });
}
