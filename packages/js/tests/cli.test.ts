import { describe, it, expect } from 'vitest';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { inspect, ask, chat, openDb } from '../src/cli/cli.js';
import { FakeModel, callResponse, finalResponse, ReinsError } from '../src/index.js';

/** Create a real on-disk SQLite database seeded with a customers table + two rows. */
function tempDb(): string {
  const path = join(mkdtempSync(join(tmpdir(), 'reins-cli-')), 'app.db');
  const db = new DatabaseSync(path);
  db.exec(
    `CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT, active INTEGER DEFAULT 1)`,
  );
  db.exec(
    `INSERT INTO customers (email,name,active) VALUES ('ada@x.com','Ada',1),('bob@x.com','Bob',0)`,
  );
  db.close();
  return path;
}

function collector() {
  const lines: string[] = [];
  return { out: (s: string) => lines.push(s), text: () => lines.join('\n') };
}

describe('cli inspect', () => {
  it('lists read capabilities read-only, and gated writes with --write', () => {
    const path = tempDb();

    const ro = collector();
    inspect(path, false, ro.out);
    const s = ro.text();
    for (const want of [
      'Reflected 1 table',
      'find_customers',
      'count_customers',
      'get_customer_by_email',
      'read-only',
    ]) {
      expect(s).toContain(want);
    }
    expect(s).not.toContain('create_customer');

    const rw = collector();
    inspect(path, true, rw.out);
    for (const want of ['create_customer', 'update_customer', 'delete_customer']) {
      expect(rw.text()).toContain(want);
    }
  });
});

describe('cli ask', () => {
  it('answers a read question via a scripted model', async () => {
    const path = tempDb();
    const model = new FakeModel(
      callResponse('c1', 'get_customer_by_email', { email: 'ada@x.com' }),
      finalResponse('Ada is active.'),
    );
    const c = collector();
    await ask(path, 'is ada active?', model, c.out);
    expect(c.text().trim()).toBe('Ada is active.');
  });
});

describe('cli chat', () => {
  it('answers then exits on EOF', async () => {
    const path = tempDb();
    const model = new FakeModel(finalResponse('There are 2 customers.'));
    const queued: Array<string | null> = ['how many customers?', null];
    let i = 0;
    const prompt = async (): Promise<string | null> => queued[i++] ?? null;

    const c = collector();
    await chat(path, model, prompt, c.out);
    expect(c.text()).toContain('There are 2 customers.');
    expect(c.text()).toContain('read-only over');
  });
});

describe('openDb', () => {
  it('opens :memory: and errors with a hint on an unopenable path', () => {
    const db = openDb(':memory:');
    db.close();
    expect(() => openDb('/no-such-reins-dir-xyz/app.db')).toThrow(ReinsError);
  });
});
