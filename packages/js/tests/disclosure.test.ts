import { describe, it, expect } from 'vitest';
import {
  Disclosure,
  CapabilityRegistry,
  capability,
  SEARCH_CAPABILITIES,
  SEARCH_SCHEMA,
  ReinsError,
  type SchemaIndex,
} from '../src/index.js';

const TOPICS = [
  'refunds',
  'orders',
  'users',
  'products',
  'invoices',
  'payments',
  'shipments',
  'returns',
  'reviews',
  'carts',
  'coupons',
  'sessions',
  'logs',
];

function bigRegistry(): CapabilityRegistry {
  const reg = new CapabilityRegistry();
  for (const t of TOPICS) {
    reg.register(capability(`find_${t}`, { description: `Find ${t}.` }, () => []));
  }
  return reg; // 13 capabilities → above the defer threshold
}

describe('Disclosure — deferral', () => {
  it('shows only the search tool when many capabilities are registered', () => {
    const d = new Disclosure(bigRegistry());
    const tools = d.tools();
    expect(tools).toHaveLength(1);
    expect(tools[0]?.name).toBe(SEARCH_CAPABILITIES);
  });

  it('exposes the full surface when few capabilities are registered', () => {
    const reg = new CapabilityRegistry();
    reg.register(capability('find_orders', { description: 'Find orders.' }, () => []));
    const d = new Disclosure(reg);
    expect(d.tools().map((t) => t.name)).toEqual(['find_orders']);
  });

  it('search exposes the matches and makes them directly callable', () => {
    const d = new Disclosure(bigRegistry());
    const result = d.answer({
      id: 'c1',
      name: SEARCH_CAPABILITIES,
      arguments: { query: 'refunds' },
    });
    expect(result?.ok).toBe(true);
    expect(String(result?.value)).toContain('find_refunds');
    // After the search, the matched capability appears in the tool surface.
    const names = d.tools().map((t) => t.name);
    expect(names).toContain(SEARCH_CAPABILITIES);
    expect(names).toContain('find_refunds');
    expect(names).not.toContain('find_logs'); // unrelated, not exposed
  });

  it('reports no matches with a hint', () => {
    const d = new Disclosure(bigRegistry());
    const result = d.answer({ id: 'c1', name: SEARCH_CAPABILITIES, arguments: { query: 'zzzzz' } });
    expect(result?.ok).toBe(false);
    expect(result?.error).toContain('no operations matched');
  });

  it('rejects an empty query', () => {
    const d = new Disclosure(bigRegistry());
    const result = d.answer({ id: 'c1', name: SEARCH_CAPABILITIES, arguments: { query: '  ' } });
    expect(result?.ok).toBe(false);
    expect(result?.error).toContain("'query' must be a non-empty string");
  });

  it('does not answer a non-discovery call', () => {
    const d = new Disclosure(bigRegistry());
    expect(d.answer({ id: 'c1', name: 'find_refunds', arguments: {} })).toBeUndefined();
  });
});

describe('Disclosure — schema search', () => {
  const schema: SchemaIndex = {
    tables: [
      { name: 'refunds', description: 'Issued refunds.', columns: ['id', 'order_id', 'amount'] },
      { name: 'orders', description: 'Customer orders.', columns: ['id', 'status'] },
    ],
  };

  it('adds search_schema and returns matching tables', () => {
    const reg = new CapabilityRegistry();
    reg.register(capability('find_orders', { description: 'Find orders.' }, () => []));
    const d = new Disclosure(reg, schema);
    expect(d.tools().map((t) => t.name)).toContain(SEARCH_SCHEMA);

    const result = d.answer({ id: 'c1', name: SEARCH_SCHEMA, arguments: { query: 'refunds' } });
    expect(result?.ok).toBe(true);
    expect(String(result?.value)).toContain('refunds');
    expect(String(result?.value)).toContain('amount');
  });
});

describe('Disclosure — collisions (control-plane)', () => {
  it('rejects a capability that collides with the discovery tool when deferred', () => {
    const reg = bigRegistry();
    reg.register(
      capability(SEARCH_CAPABILITIES, { description: 'A colliding capability.' }, () => []),
    );
    expect(() => new Disclosure(reg)).toThrow(ReinsError);
  });
});
