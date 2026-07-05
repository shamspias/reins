import { describe, it, expect } from 'vitest';
import {
  capability,
  params,
  toolSpec,
  lintCapability,
  ReinsError,
  type Context,
} from '../src/index.js';

describe('capability', () => {
  it('classifies access from the name and carries safety metadata', () => {
    const cap = capability(
      'create_order',
      { description: 'Create a new order for a customer.', parameters: params({ sku: 'string' }) },
      () => ({ ok: true }),
    );
    expect(cap.spec.access).toBe('write');
    expect(cap.spec.name).toBe('create_order');
    expect(cap.spec.input_schema).toEqual({
      type: 'object',
      properties: { sku: { type: 'string' } },
      required: ['sku'],
      additionalProperties: false,
    });
    expect(cap.returnsRaw).toBe(false);
  });

  it('honors explicit classification overrides and flags', () => {
    const cap = capability(
      'get_report', // read verb ...
      {
        description: 'Generate and email a report.',
        destructive: true,
        confirm: true,
        scope: 'user',
      },
      () => undefined,
    );
    expect(cap.spec.access).toBe('destructive'); // ... overridden to destructive
    expect(cap.spec.confirm).toBe(true);
    expect(cap.spec.scope).toBe('user');
  });

  it('passes the run context and args to the handler', async () => {
    let seen: { ctx: Context; args: Record<string, unknown> } | undefined;
    const cap = capability(
      'find_orders',
      { description: 'Find orders for the caller.' },
      (ctx, args) => {
        seen = { ctx, args };
        return [];
      },
    );
    await cap.handler({ principal: 'u1' }, { status: 'open' });
    expect(seen?.ctx.principal).toBe('u1');
    expect(seen?.args).toEqual({ status: 'open' });
  });

  it('produces a lint-clean spec and a policy-free tool surface', () => {
    const cap = capability(
      'refund_order',
      {
        description: 'Refund a customer order by its public order number.',
        parameters: params({ order_id: 'integer', reason: 'string?' }),
        destructive: true,
        confirm: true,
      },
      () => undefined,
    );
    expect(lintCapability(cap.spec, cap.returnsRaw).ok).toBe(true);
    const spec = toolSpec(cap.spec);
    expect('access' in spec).toBe(false);
    expect('confirm' in spec).toBe(false);
  });
});

describe('params', () => {
  it('marks non-optional fields required and optional fields not', () => {
    const schema = params({ id: 'integer', note: 'string?' });
    expect(schema['required']).toEqual(['id']);
    const props = schema['properties'] as Record<string, unknown>;
    expect(props['note']).toEqual({ type: 'string' });
  });

  it('supports arrays, alone and combined with optional', () => {
    const schema = params({ tags: 'string[]', ids: 'integer[]?' });
    const props = schema['properties'] as Record<string, unknown>;
    expect(props['tags']).toEqual({ type: 'array', items: { type: 'string' } });
    expect(props['ids']).toEqual({ type: 'array', items: { type: 'integer' } });
    expect(schema['required']).toEqual(['tags']);
  });

  it('throws a hinted error on an unknown type', () => {
    expect(() => params({ when: 'date' })).toThrow(ReinsError);
    expect(() => params({ when: 'date' })).toThrow('→');
  });
});
