import { describe, it, expect } from 'vitest';
import { sqliteTable, text, integer } from 'drizzle-orm/sqlite-core';
import { drizzle } from 'drizzle-orm/libsql';
import { createClient } from '@libsql/client';
import { sql } from 'drizzle-orm';
import { fromDrizzle, introspect } from '../src/orm/index.js';
import { type Context } from '../src/index.js';

const customers = sqliteTable('customers', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  email: text('email').notNull().unique(),
  name: text('name'),
  active: integer('active').default(1),
});

const ctx: Context = {};

describe('fromDrizzle — introspection (no database)', () => {
  it('shapes the table into intent-level capabilities', () => {
    const { capabilities, schema } = fromDrizzle({}, { customers }, { canWrite: true });
    const byName = new Map(capabilities.map((c) => [c.spec.name, c.spec.access]));

    expect(byName.get('find_customers')).toBe('read');
    expect(byName.get('count_customers')).toBe('read');
    expect(byName.get('get_customer_by_email')).toBe('read'); // email is the natural key
    expect(byName.get('create_customer')).toBe('write');
    expect(byName.get('update_customer')).toBe('write');
    expect(byName.get('delete_customer')).toBe('destructive');

    expect(schema.tables?.[0]?.name).toBe('customers');
  });

  it('classifies the id column as an autoincrementing integer primary key', () => {
    const info = introspect(customers);
    const id = info.columns.find((c) => c.name === 'id')!;
    expect(id).toMatchObject({ jsonType: 'integer', autoincrement: true });
    expect(info.primaryKey?.name).toBe('id');
    const email = info.columns.find((c) => c.name === 'email')!;
    expect(email).toMatchObject({ jsonType: 'string', unique: true, nullable: false });
  });

  it('generates read-only capabilities when canWrite is omitted', () => {
    const { capabilities } = fromDrizzle({}, [customers]);
    expect(capabilities.every((c) => c.spec.access === 'read')).toBe(true);
  });
});

describe('fromDrizzle — end-to-end round-trip (in-memory libsql)', () => {
  it('runs the generated handlers against a real database', async () => {
    const client = createClient({ url: ':memory:' });
    const db = drizzle(client);
    await db.run(
      sql`CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT, active INTEGER DEFAULT 1)`,
    );

    const { capabilities } = fromDrizzle(db, { customers }, { canWrite: true });
    const cap = (name: string) => capabilities.find((c) => c.spec.name === name)!;

    await cap('create_customer').handler(ctx, { email: 'a@b.com', name: 'Ada', active: 1 });

    const got = (await cap('get_customer_by_email').handler(ctx, { email: 'a@b.com' })) as Record<
      string,
      unknown
    >;
    expect(got).toMatchObject({ email: 'a@b.com', name: 'Ada' });

    expect(await cap('count_customers').handler(ctx, {})).toBe(1);

    const found = (await cap('find_customers').handler(ctx, { active: 1 })) as unknown[];
    expect(found).toHaveLength(1);

    const updated = (await cap('update_customer').handler(ctx, {
      email: 'a@b.com',
      name: 'Ada L.',
    })) as Record<string, unknown>;
    expect(updated).toMatchObject({ name: 'Ada L.' });

    await cap('delete_customer').handler(ctx, { email: 'a@b.com' });
    expect(await cap('get_customer_by_email').handler(ctx, { email: 'a@b.com' })).toBeNull();

    client.close();
  });

  it('surfaces a not-found update as a thrown error', async () => {
    const client = createClient({ url: ':memory:' });
    const db = drizzle(client);
    await db.run(
      sql`CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT, active INTEGER DEFAULT 1)`,
    );
    const { capabilities } = fromDrizzle(db, { customers }, { canWrite: true });
    const update = capabilities.find((c) => c.spec.name === 'update_customer')!;
    await expect(update.handler(ctx, { email: 'nobody@x.com', name: 'X' })).rejects.toThrow(
      /no customer/,
    );
    client.close();
  });
});
