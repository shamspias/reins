import { describe, it, expect } from 'vitest';
import {
  toolSpec,
  resultOk,
  resultError,
  defaultModelParams,
  type Capability,
  type Message,
} from '../src/index.js';

describe('toolSpec', () => {
  it('exposes only the intent surface — never policy metadata (§2.5)', () => {
    const cap: Capability = {
      name: 'delete_order',
      description: 'Delete an order by its id.',
      input_schema: { type: 'object', properties: { order_id: { type: 'integer' } } },
      access: 'destructive',
      confirm: true,
      idempotent: false,
      scope: 'user',
    };
    const spec = toolSpec(cap);
    expect(spec).toEqual({
      name: 'delete_order',
      description: 'Delete an order by its id.',
      input_schema: { type: 'object', properties: { order_id: { type: 'integer' } } },
    });
    // The dangerous fields must not have leaked through.
    expect('access' in spec).toBe(false);
    expect('confirm' in spec).toBe(false);
    expect('scope' in spec).toBe(false);
  });

  it('omits input_schema when the capability has none', () => {
    const cap: Capability = {
      name: 'ping',
      description: 'Health check.',
      access: 'read',
      confirm: false,
      idempotent: true,
    };
    expect(toolSpec(cap)).toEqual({ name: 'ping', description: 'Health check.' });
  });
});

describe('serialization', () => {
  it('round-trips a Message with tool calls through JSON', () => {
    const msg: Message = {
      role: 'assistant',
      tool_calls: [{ id: 'c1', name: 'refund_order', arguments: { order_id: 8842 } }],
    };
    expect(JSON.parse(JSON.stringify(msg))).toEqual(msg);
  });

  it('round-trips a Capability through JSON with contract field names', () => {
    const cap: Capability = {
      name: 'find_orders',
      description: 'Find orders matching optional filters.',
      input_schema: { type: 'object' },
      access: 'read',
      confirm: false,
      idempotent: false,
    };
    const json = JSON.stringify(cap);
    expect(json).toContain('"input_schema"');
    expect(JSON.parse(json)).toEqual(cap);
  });
});

describe('CapabilityResult helpers', () => {
  it('builds success and failure results', () => {
    expect(resultOk({ id: 1 })).toEqual({ ok: true, value: { id: 1 } });
    expect(resultError('not found')).toEqual({ ok: false, error: 'not found', retryable: false });
    expect(resultError('rate limited', true)).toEqual({
      ok: false,
      error: 'rate limited',
      retryable: true,
    });
  });
});

describe('defaultModelParams', () => {
  it('defaults max_tokens to 1024', () => {
    expect(defaultModelParams()).toEqual({ max_tokens: 1024 });
  });
});
