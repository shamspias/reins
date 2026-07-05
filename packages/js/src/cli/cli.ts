import { DatabaseSync } from 'node:sqlite';
import type { Model } from '../model.js';
import type { BoundCapability } from '../capability.js';
import { Agent } from '../loop.js';
import { ReinsError } from '../errors.js';
import { fromSqlite } from './sqlite.js';

/**
 * The `reins` command-line tool: point it at a SQLite database URL and inspect /
 * ask / chat over it with zero hand-written code. The commands are plain functions
 * taking a line writer (and, for ask/chat, a reins.Model), so they test with a
 * FakeModel and no network. The TypeScript mirror of Go's `cli` package.
 */

/** A sink for one line of output. */
export type Writer = (line: string) => void;

/**
 * Open a SQLite database from a URL: a bare path, `:memory:`, a `sqlite://` URL
 * (scheme stripped), or a `file:` URI (passed through).
 */
export function openDb(dbUrl: string): DatabaseSync {
  try {
    return new DatabaseSync(sqlitePath(dbUrl));
  } catch (e) {
    throw new ReinsError(
      `cannot open database: ${message(e)}`,
      'pass a SQLite file path, :memory:, or a sqlite:/// URL',
    );
  }
}

function sqlitePath(dbUrl: string): string {
  if (dbUrl.startsWith('file:')) {
    return dbUrl;
  }
  for (const prefix of ['sqlite3://', 'sqlite://', 'sqlite3:', 'sqlite:']) {
    if (dbUrl.startsWith(prefix)) {
      return dbUrl.slice(prefix.length);
    }
  }
  return dbUrl;
}

/** Reflect the database and list the capabilities reins would expose. No model needed. */
export function inspect(dbUrl: string, canWrite: boolean, out: Writer): void {
  const db = openDb(dbUrl);
  try {
    const { capabilities, schema } = fromSqlite(db, canWrite, 0);
    out(`Reflected ${schema.tables?.length ?? 0} table(s) from ${dbUrl}`);
    for (const t of schema.tables ?? []) {
      out(`  • ${t.name} (${t.columns?.length ?? 0} columns)`);
    }
    out('');
    out(`${capabilities.length} capabilities:`);
    for (const line of describe(capabilities)) {
      out(line);
    }
    if (!canWrite) {
      out('');
      out('(read-only — pass --write to also generate gated create/update/delete)');
    }
  } finally {
    db.close();
  }
}

/** Answer one read-only question. The build is read-only (no writes are generated). */
export async function ask(
  dbUrl: string,
  question: string,
  model: Model,
  out: Writer,
): Promise<void> {
  const { agent, db } = readOnlyAgent(dbUrl, model);
  try {
    const res = await agent.ask(question);
    out((res.output.text ?? '').trim());
  } finally {
    db.close();
  }
}

/**
 * Run an interactive read-only question loop. `prompt` reads one line (returning
 * null at EOF); `out` writes replies. Stops on EOF or "exit"/"quit".
 */
export async function chat(
  dbUrl: string,
  model: Model,
  prompt: (label: string) => Promise<string | null>,
  out: Writer,
): Promise<void> {
  const { agent, db } = readOnlyAgent(dbUrl, model);
  out(`reins chat — read-only over ${dbUrl}. Ask a question, or type 'exit'.`);
  try {
    for (;;) {
      const line = await prompt('\n> ');
      if (line === null) {
        break;
      }
      const q = line.trim();
      if (q === '') {
        continue;
      }
      if (q === 'exit' || q === 'quit') {
        break;
      }
      try {
        const res = await agent.ask(q);
        out((res.output.text ?? '').trim());
      } catch (e) {
        out(String(e));
      }
    }
  } finally {
    db.close();
  }
}

function readOnlyAgent(dbUrl: string, model: Model): { agent: Agent; db: DatabaseSync } {
  const db = openDb(dbUrl);
  try {
    const { capabilities, schema } = fromSqlite(db, false, 0);
    const agent = new Agent(model, { capabilities, schema, autonomy: 'read_only' });
    return { agent, db };
  } catch (e) {
    db.close();
    throw e;
  }
}

/** Render capabilities grouped read → write → destructive, then by name. */
function describe(caps: BoundCapability[]): string[] {
  const rank = (a: string): number => (a === 'read' ? 0 : a === 'write' ? 1 : 2);
  return [...caps]
    .sort(
      (x, y) =>
        rank(x.spec.access) - rank(y.spec.access) ||
        (x.spec.name < y.spec.name ? -1 : x.spec.name > y.spec.name ? 1 : 0),
    )
    .map((c) => `  [${c.spec.access.padEnd(11)}] ${c.spec.name} — ${c.spec.description}`);
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
