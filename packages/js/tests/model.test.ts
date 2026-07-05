import { describe, it, expect } from 'vitest';
import {
  FakeModel,
  finalResponse,
  callResponse,
  programResponse,
  defaultModelParams,
  ReinsError,
  type Model,
} from '../src/index.js';

describe('FakeModel', () => {
  it('replays scripted responses in order and records every call', async () => {
    const fake = new FakeModel(
      callResponse('c1', 'get_order', { id: 8842 }),
      finalResponse('That order cost $42.'),
    );

    const first = await fake.complete(
      [{ role: 'user', text: 'cost of 8842?' }],
      [],
      defaultModelParams(),
    );
    expect(first.finish_reason).toBe('tool_calls');
    expect(first.message.tool_calls?.[0]?.name).toBe('get_order');

    const second = await fake.complete([], [], defaultModelParams());
    expect(second.finish_reason).toBe('stop');
    expect(second.message.text).toBe('That order cost $42.');

    // Both calls were recorded with the messages the model saw.
    expect(fake.calls).toHaveLength(2);
    expect(fake.calls[0]?.messages[0]?.text).toBe('cost of 8842?');
  });

  it('records a snapshot of the inputs, not a live reference', async () => {
    const fake = new FakeModel(finalResponse('ok'));
    const messages = [{ role: 'user' as const, text: 'hi' }];
    await fake.complete(messages, [], defaultModelParams());
    messages.push({ role: 'user' as const, text: 'mutated after the call' });
    expect(fake.calls[0]?.messages).toHaveLength(1);
  });

  it('rejects with a hinted error once the script is exhausted', async () => {
    const fake = new FakeModel(finalResponse('only one'));
    await fake.complete([], [], defaultModelParams());

    await expect(fake.complete([], [], defaultModelParams())).rejects.toBeInstanceOf(ReinsError);
    await expect(fake.complete([], [], defaultModelParams())).rejects.toThrow('→');
  });

  it('satisfies the Model interface', () => {
    const model: Model = new FakeModel();
    expect(typeof model.complete).toBe('function');
  });
});

describe('scripted response builders', () => {
  it('finalResponse is an assistant stop message', () => {
    const r = finalResponse('done');
    expect(r).toEqual({
      message: { role: 'assistant', text: 'done' },
      finish_reason: 'stop',
      usage: { input_tokens: 0, output_tokens: 0, cost: 0 },
    });
  });

  it('callResponse carries one tool call', () => {
    const r = callResponse('c1', 'refund_order', { order_id: 8842 });
    expect(r.finish_reason).toBe('tool_calls');
    expect(r.message.tool_calls).toEqual([
      { id: 'c1', name: 'refund_order', arguments: { order_id: 8842 } },
    ]);
  });

  it('programResponse is a run_program tool call carrying code', () => {
    const r = programResponse('p1', 'return 1 + 1');
    expect(r.message.tool_calls?.[0]?.name).toBe('run_program');
    expect(r.message.tool_calls?.[0]?.arguments).toEqual({ code: 'return 1 + 1' });
  });
});
