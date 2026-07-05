import type { DatabaseSync } from 'node:sqlite';
import type { SchemaIndex } from '../types.js';
import type { BoundCapability } from '../capability.js';
import {
  type Backend,
  type ColumnInfo,
  type ModelInfo,
  buildCapabilities,
  missing,
  singularize,
  trimRow,
} from '../orm/build.js';
import { ReinsError } from '../errors.js';

/**
 * Zero-code CLI reflection: read a live SQLite schema through PRAGMA and hand the
 * resulting ORM-neutral ModelInfo to the shared `buildCapabilities` engine — the
 * TypeScript mirror of Go's `orm/sqlreflect.go`. Uses the built-in `node:sqlite`
 * (no driver dependency); the SqliteBackend runs the six operations as parameterized
 * SQL with validated + quoted identifiers (§2.6).
 */

type SqlValue = string | number | bigint | null | Uint8Array;

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;

function isSafeIdent(name: string): boolean {
  return IDENT.test(name);
}

function quoteIdent(name: string): string {
  if (!isSafeIdent(name)) {
    throw new ReinsError(
      `unsafe identifier: ${name}`,
      'reins only operates on plain identifier names',
    );
  }
  return `"${name}"`;
}

/** SQLite has no boolean/object binding; coerce to a supported value. */
function coerce(v: unknown): SqlValue {
  if (typeof v === 'boolean') return v ? 1 : 0;
  if (
    v === null ||
    typeof v === 'string' ||
    typeof v === 'number' ||
    typeof v === 'bigint' ||
    v instanceof Uint8Array
  ) {
    return v;
  }
  return String(v);
}

/** Introspect a live SQLite database into ORM-neutral ModelInfo, one per user table. */
export function reflectSqlite(db: DatabaseSync): ModelInfo[] {
  const tables = (
    db
      .prepare(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
      )
      .all() as Array<{ name: string }>
  ).map((r) => r.name);
  if (tables.length === 0) {
    throw new ReinsError(
      'the database has no tables',
      'point reins at a database with tables, or create one first',
    );
  }
  const infos: ModelInfo[] = [];
  for (const table of tables) {
    const info = reflectTable(db, table);
    if (info && info.columns.length > 0) {
      infos.push(info);
    }
  }
  if (infos.length === 0) {
    throw new ReinsError('no usable tables found', 'reins needs tables with readable columns');
  }
  return infos;
}

function reflectTable(db: DatabaseSync, table: string): ModelInfo | null {
  if (!isSafeIdent(table)) {
    return null; // unusual name — skip rather than risk unsafe SQL
  }
  const rows = db.prepare(`PRAGMA table_info(${quoteIdent(table)})`).all() as Array<{
    name: string;
    type: string;
    notnull: number;
    dflt_value: unknown;
    pk: number;
  }>;
  const columns: ColumnInfo[] = [];
  let primaryKey: ColumnInfo | undefined;
  for (const r of rows) {
    if (!isSafeIdent(r.name)) {
      continue;
    }
    const col: ColumnInfo = {
      name: r.name,
      jsonType: sqliteJsonType(r.type),
      nullable: r.notnull === 0 && r.pk === 0,
      autoincrement: r.pk === 1 && /int/i.test(r.type),
      hasDefault: r.dflt_value !== null,
      unique: false,
    };
    columns.push(col);
    if (r.pk >= 1 && !primaryKey) {
      primaryKey = { ...col };
    }
  }
  const unique = singleColumnUniques(db, table);
  for (const c of columns) {
    if (unique.has(c.name)) {
      c.unique = true;
    }
  }
  const info: ModelInfo = { singular: singularize(table), plural: table, columns };
  if (primaryKey) {
    info.primaryKey = primaryKey;
  }
  return info;
}

/** Columns backed by a single-column UNIQUE index — what makes a natural-key lookup safe. */
function singleColumnUniques(db: DatabaseSync, table: string): Set<string> {
  const out = new Set<string>();
  const indexes = db.prepare(`PRAGMA index_list(${quoteIdent(table)})`).all() as Array<{
    name: string;
    unique: number;
    partial?: number;
  }>;
  for (const idx of indexes) {
    if (idx.unique !== 1 || (idx.partial ?? 0) !== 0 || !isSafeIdent(idx.name)) {
      continue;
    }
    const cols = (
      db.prepare(`PRAGMA index_info(${quoteIdent(idx.name)})`).all() as Array<{
        name: string | null;
      }>
    )
      .map((c) => c.name)
      .filter((n): n is string => n !== null);
    if (cols.length === 1) {
      out.add(cols[0]!);
    }
  }
  return out;
}

/** Map a declared SQLite type to a JSON Schema type by affinity. */
function sqliteJsonType(declared: string): string {
  const t = declared.toUpperCase();
  if (t.includes('INT')) return 'integer';
  if (t.includes('BOOL')) return 'boolean';
  if (/REAL|FLOA|DOUB|NUMERIC|DEC/.test(t)) return 'number';
  return 'string';
}

/** Reflect a live SQLite database and return capabilities plus a schema index. */
export function fromSqlite(
  db: DatabaseSync,
  canWrite: boolean,
  readLimit: number,
): { capabilities: BoundCapability[]; schema: SchemaIndex } {
  return buildCapabilities(new SqliteBackend(db), reflectSqlite(db), canWrite, readLimit);
}

function whereClause(filters: Record<string, unknown>): { clause: string; params: SqlValue[] } {
  const keys = Object.keys(filters).sort();
  if (keys.length === 0) {
    return { clause: '', params: [] };
  }
  const parts = keys.map((k) => `${quoteIdent(k)} = ?`);
  return { clause: ' WHERE ' + parts.join(' AND '), params: keys.map((k) => coerce(filters[k])) };
}

/** Runs the six operations against a node:sqlite database using parameterized SQL. */
class SqliteBackend implements Backend {
  constructor(private readonly db: DatabaseSync) {}

  async find(
    info: ModelInfo,
    filters: Record<string, unknown>,
    limit: number,
  ): Promise<Array<Record<string, unknown>>> {
    const { clause, params } = whereClause(filters);
    const rows = this.db
      .prepare(`SELECT * FROM ${quoteIdent(info.plural)}${clause} LIMIT ${limit}`)
      .all(...params) as Array<Record<string, unknown>>;
    return rows.map((r) => trimRow(info, { ...r }));
  }

  async count(info: ModelInfo, filters: Record<string, unknown>): Promise<number> {
    const { clause, params } = whereClause(filters);
    const row = this.db
      .prepare(`SELECT COUNT(*) AS n FROM ${quoteIdent(info.plural)}${clause}`)
      .get(...params) as { n: number } | undefined;
    return Number(row?.n ?? 0);
  }

  async get(info: ModelInfo, key: string, value: unknown): Promise<Record<string, unknown> | null> {
    const rows = await this.find(info, { [key]: value }, 1);
    return rows[0] ?? null;
  }

  async create(info: ModelInfo, values: Record<string, unknown>): Promise<Record<string, unknown>> {
    const keys = Object.keys(values).sort();
    if (keys.length === 0) {
      throw new ReinsError('nothing to create', 'provide at least one column value');
    }
    const cols = keys.map(quoteIdent).join(', ');
    const placeholders = keys.map(() => '?').join(', ');
    this.db
      .prepare(`INSERT INTO ${quoteIdent(info.plural)} (${cols}) VALUES (${placeholders})`)
      .run(...keys.map((k) => coerce(values[k])));
    return trimRow(info, values);
  }

  async update(
    info: ModelInfo,
    key: string,
    value: unknown,
    changes: Record<string, unknown>,
  ): Promise<Record<string, unknown> | null> {
    if (!(await this.get(info, key, value))) {
      throw missing(info, key, value);
    }
    const keys = Object.keys(changes).sort();
    if (keys.length > 0) {
      const sets = keys.map((k) => `${quoteIdent(k)} = ?`).join(', ');
      this.db
        .prepare(`UPDATE ${quoteIdent(info.plural)} SET ${sets} WHERE ${quoteIdent(key)} = ?`)
        .run(...keys.map((k) => coerce(changes[k])), coerce(value));
    }
    return this.get(info, key, value);
  }

  async delete(info: ModelInfo, key: string, value: unknown): Promise<void> {
    if (!(await this.get(info, key, value))) {
      throw missing(info, key, value);
    }
    this.db
      .prepare(`DELETE FROM ${quoteIdent(info.plural)} WHERE ${quoteIdent(key)} = ?`)
      .run(coerce(value));
  }
}
