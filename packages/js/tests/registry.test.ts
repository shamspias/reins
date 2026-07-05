import { describe, it, expect } from 'vitest';
import {
  CapabilityRegistry,
  capability,
  params,
  closestMatches,
  ReinsError,
  type Context,
} from '../src/index.js';

const ctx: Context = {};

function orderRegistry(): CapabilityRegistry {
  const reg = new CapabilityRegistry();
  reg.register(
    capability(
      'get_order',
      { description: 'Get an order by its id.', parameters: params({ id: 'integer' }) },
      (_c, args) => ({ id: args['id'], total: 42 }),
    ),
  );
  reg.register(
    capability(
      'delete_order',
      { description: 'Delete an order by its id.', parameters: params({ id: 'integer' }) },
      () => ({ deleted: true }),
    ),
  );
  return reg;
}

describe('CapabilityRegistry', () => {
  it('registers, lists in order, and exposes only the intent surface', () => {
    const reg = orderRegistry();
    expect(reg.list().map((c) => c.name)).toEqual(['get_order', 'delete_order']);
    for (const spec of reg.toolSpecs()) {
      expect('access' in spec).toBe(false);
    }
  });

  it('rejects a duplicate name (control-plane error)', () => {
    const reg = orderRegistry();
    expect(() => reg.register(capability('get_order', { description: 'dup' }, () => null))).toThrow(
      ReinsError,
    );
  });

  it('calls a capability and returns the result as data', async () => {
    const reg = orderRegistry();
    const result = await reg.call('get_order', { id: 7 }, ctx);
    expect(result).toEqual({ ok: true, value: { id: 7, total: 42 } });
  });

  it('returns an unknown capability as data, with closest matches', async () => {
    const reg = orderRegistry();
    const result = await reg.call('get_ordr', {}, ctx);
    expect(result.ok).toBe(false);
    expect(result.error).toContain('no capability named "get_ordr"');
    expect(result.error).toContain('get_order');
  });

  it('surfaces a handler throw as errors-as-data (never over the loop)', async () => {
    const reg = new CapabilityRegistry();
    reg.register(
      capability('get_thing', { description: 'Explodes on purpose.' }, () => {
        throw new Error('boom');
      }),
    );
    const result = await reg.call('get_thing', {}, ctx);
    expect(result).toEqual({ ok: false, error: 'boom', retryable: false });
  });
});

describe('argument validation (§2.6 — model output is untrusted)', () => {
  it('rejects a missing required argument', () => {
    const reg = orderRegistry();
    expect(() => reg.validate('get_order', {})).toThrow(/missing required argument/);
  });

  it('rejects an unexpected argument when additionalProperties is false', () => {
    const reg = orderRegistry();
    expect(() => reg.validate('get_order', { id: 1, sneaky: true })).toThrow(/unexpected argument/);
  });

  it('rejects a wrong argument type', () => {
    const reg = orderRegistry();
    expect(() => reg.validate('get_order', { id: 'not-a-number' })).toThrow(
      /must be of type integer/,
    );
  });

  it('accepts valid arguments', () => {
    const reg = orderRegistry();
    expect(() => reg.validate('get_order', { id: 5 })).not.toThrow();
  });

  it('rejects prototype-named arguments (no prototype-chain bypass, §2.6)', () => {
    const reg = orderRegistry();
    // "toString"/"constructor" are Object.prototype members, not schema properties.
    expect(() => reg.validate('get_order', { id: 1, toString: 'x' })).toThrow(
      /unexpected argument/,
    );
    expect(() => reg.validate('get_order', { id: 1, constructor: 'x' })).toThrow(
      /unexpected argument/,
    );
  });

  it('returns a validation failure as data through call()', async () => {
    const reg = orderRegistry();
    const result = await reg.call('get_order', { id: 'bad' }, ctx);
    expect(result.ok).toBe(false);
    expect(result.error).toContain('must be of type integer');
  });
});

describe('registry-level RLS (defense in depth, §2.8)', () => {
  it('refuses a scoped capability at the chokepoint when there is no principal', async () => {
    const reg = new CapabilityRegistry();
    let ran = false;
    reg.register(
      capability('get_my_orders', { description: 'List the caller orders.', scope: 'user' }, () => {
        ran = true;
        return [];
      }),
    );
    const result = await reg.call('get_my_orders', {}, {}); // no principal
    expect(result.ok).toBe(false);
    expect(result.error).toContain('scoped to');
    expect(ran).toBe(false);
  });
});

describe('closestMatches', () => {
  it('ranks near names first and drops distant ones', () => {
    const matches = closestMatches('get_ordr', ['get_order', 'delete_order', 'list_users'], 3);
    expect(matches[0]).toBe('get_order'); // the nearest is first
    expect(matches).not.toContain('list_users'); // too far to suggest
    expect(closestMatches('zzzzzz', ['get_order'], 3)).toEqual([]);
  });
});
