import { describe, it, expect } from 'vitest';
import { lintCapability, type Context } from '../src/index.js';
import {
  buildCapabilities,
  missing,
  singularize,
  type Backend,
  type ColumnInfo,
  type ModelInfo,
} from '../src/orm/build.js';

function col(name: string, jsonType: string, opts: Partial<ColumnInfo> = {}): ColumnInfo {
  return {
    name,
    jsonType,
    nullable: opts.nullable ?? false,
    autoincrement: opts.autoincrement ?? false,
    hasDefault: opts.hasDefault ?? false,
    unique: opts.unique ?? false,
  };
}

const id = col('id', 'integer', { autoincrement: true });
const customers: ModelInfo = {
  singular: 'customer',
  plural: 'customers',
  columns: [
    id,
    col('email', 'string', { unique: true }),
    col('name', 'string', { nullable: true }),
    col('active', 'integer', { hasDefault: true }),
  ],
  primaryKey: id,
};

/** A backend that records which operations ran and returns canned data. */
class FakeBackend implements Backend {
  calls: string[] = [];
  async find(): Promise<Array<Record<string, unknown>>> {
    this.calls.push('find');
    return [{ id: 1, email: 'a@b.com', name: 'Ada' }];
  }
  async count(): Promise<number> {
    this.calls.push('count');
    return 1;
  }
  async get(
    _info: ModelInfo,
    _key: string,
    value: unknown,
  ): Promise<Record<string, unknown> | null> {
    this.calls.push('get');
    return value === 'a@b.com' ? { id: 1, email: 'a@b.com' } : null;
  }
  async create(
    _info: ModelInfo,
    values: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    this.calls.push('create');
    return values;
  }
  async update(
    info: ModelInfo,
    key: string,
    value: unknown,
    changes: Record<string, unknown>,
  ): Promise<Record<string, unknown> | null> {
    this.calls.push('update');
    if (value === 'missing') throw missing(info, key, value);
    return { email: value, ...changes };
  }
  async delete(info: ModelInfo, key: string, value: unknown): Promise<void> {
    this.calls.push('delete');
    if (value === 'missing') throw missing(info, key, value);
  }
}

describe('buildCapabilities — read side', () => {
  it('generates intent-level read capabilities that all pass the linter', () => {
    const { capabilities, schema } = buildCapabilities(new FakeBackend(), [customers], false, 0);
    const byAccess = capabilities.map((c) => [c.spec.name, c.spec.access] as const);
    expect(byAccess).toEqual([
      ['find_customers', 'read'],
      ['count_customers', 'read'],
      ['get_customer_by_email', 'read'], // natural key (unique string) preferred over id
    ]);
    for (const c of capabilities) {
      expect(lintCapability(c.spec, c.returnsRaw).ok).toBe(true);
    }
    expect(schema.tables?.[0]?.name).toBe('customers');
    expect(schema.tables?.[0]?.columns).toEqual(['id', 'email', 'name', 'active']);
  });

  it('falls back to the primary key when there is no natural key', () => {
    const noNatural: ModelInfo = {
      singular: 'event',
      plural: 'events',
      columns: [id, col('payload', 'string', { nullable: true })],
      primaryKey: id,
    };
    const { capabilities } = buildCapabilities(new FakeBackend(), [noNatural], false, 0);
    expect(capabilities.map((c) => c.spec.name)).toContain('get_event_by_id');
  });

  it('omits get/update/delete when a model has no lookup key', () => {
    const keyless: ModelInfo = {
      singular: 'log',
      plural: 'logs',
      columns: [col('message', 'string', { nullable: true })],
    };
    const { capabilities } = buildCapabilities(new FakeBackend(), [keyless], true, 0);
    const names = capabilities.map((c) => c.spec.name);
    expect(names).toEqual(['find_logs', 'count_logs', 'create_log']);
  });
});

describe('buildCapabilities — write side (gated)', () => {
  it('generates create/update/delete only when canWrite is true', () => {
    const readOnly = buildCapabilities(new FakeBackend(), [customers], false, 0);
    expect(readOnly.capabilities.some((c) => c.spec.access !== 'read')).toBe(false);

    const { capabilities } = buildCapabilities(new FakeBackend(), [customers], true, 0);
    const byName = new Map(capabilities.map((c) => [c.spec.name, c.spec]));
    expect(byName.get('create_customer')?.access).toBe('write');
    expect(byName.get('update_customer')?.access).toBe('write');
    expect(byName.get('delete_customer')?.access).toBe('destructive');
  });

  it('requires only non-optional columns on create (id excluded, defaults/nullable optional)', () => {
    const { capabilities } = buildCapabilities(new FakeBackend(), [customers], true, 0);
    const create = capabilities.find((c) => c.spec.name === 'create_customer')!;
    const required = create.spec.input_schema?.['required'] as string[];
    expect(required).toEqual(['email']); // email is unique+notNull; id/name/active are not required
  });
});

describe('generated handlers', () => {
  const ctx: Context = {};

  it('call through to the backend', async () => {
    const backend = new FakeBackend();
    const { capabilities } = buildCapabilities(backend, [customers], true, 0);
    const cap = (name: string) => capabilities.find((c) => c.spec.name === name)!;

    expect(await cap('find_customers').handler(ctx, {})).toEqual([
      { id: 1, email: 'a@b.com', name: 'Ada' },
    ]);
    expect(await cap('get_customer_by_email').handler(ctx, { email: 'a@b.com' })).toEqual({
      id: 1,
      email: 'a@b.com',
    });
    expect(await cap('get_customer_by_email').handler(ctx, { email: 'nope' })).toBeNull();
    expect(await cap('delete_customer').handler(ctx, { email: 'a@b.com' })).toEqual({
      deleted: 'a@b.com',
    });
  });

  it('propagate a not-found error from update/delete (errors-as-data via the loop)', async () => {
    const { capabilities } = buildCapabilities(new FakeBackend(), [customers], true, 0);
    const update = capabilities.find((c) => c.spec.name === 'update_customer')!;
    await expect(update.handler(ctx, { email: 'missing', name: 'X' })).rejects.toThrow(
      /no customer/,
    );
  });

  it('drops unknown and null arguments before hitting the backend (§2.6)', async () => {
    const { capabilities } = buildCapabilities(new FakeBackend(), [customers], true, 0);
    const create = capabilities.find((c) => c.spec.name === 'create_customer')!;
    // sneaky/undefined are not real columns / are nullish → filtered out.
    const created = await create.handler(ctx, { email: 'a@b.com', sneaky: true, name: null });
    expect(created).toEqual({ email: 'a@b.com' });
  });
});

describe('singularize', () => {
  it('handles common plurals', () => {
    const cases: Record<string, string> = {
      customers: 'customer',
      categories: 'category',
      boxes: 'box',
      addresses: 'address',
      data: 'data',
    };
    for (const [input, want] of Object.entries(cases)) {
      expect(singularize(input)).toBe(want);
    }
  });
});
