import { getTableColumns, getTableName, eq, and, count } from 'drizzle-orm';
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
} from './build.js';

/**
 * The Drizzle adapter — the TypeScript mirror of Go's GORM `FromORM`. It introspects
 * Drizzle table objects into ORM-neutral `ModelInfo` (no database needed) and runs
 * the six data operations through Drizzle's query builder, feeding the shared
 * `buildCapabilities` engine.
 *
 * Drizzle's `db` and table types are deeply generic (parameterized by driver and
 * schema), so they are treated structurally as `any` here — the queries are dynamic
 * by nature. This module lives under the `reins/orm` subpath so the core package
 * never depends on drizzle-orm.
 */

type DrizzleTable = any;
type DrizzleDb = any;
type Columns = Record<string, any>;

/** Options for {@link fromDrizzle}. Writes are only generated when `canWrite` is true. */
export interface FromDrizzleOptions {
  canWrite?: boolean;
  readLimit?: number;
}

/**
 * Introspect Drizzle tables and return intent-level capabilities plus a schema
 * index. Pass the tables as an array or as a Drizzle schema object
 * (`{ users, orders }`). Reads are always generated; writes only with `canWrite`
 * (and are then gated by the autonomy ladder like any capability).
 */
export function fromDrizzle(
  db: DrizzleDb,
  tables: Record<string, DrizzleTable> | DrizzleTable[],
  options: FromDrizzleOptions = {},
): { capabilities: BoundCapability[]; schema: SchemaIndex } {
  const list = Array.isArray(tables) ? tables : Object.values(tables);
  const infos = list.map(introspect);
  return buildCapabilities(
    new DrizzleBackend(db),
    infos,
    options.canWrite ?? false,
    options.readLimit ?? 0,
  );
}

/** Reflect one Drizzle table object into ORM-neutral ModelInfo (no database access). */
export function introspect(table: DrizzleTable): ModelInfo {
  const columns: ColumnInfo[] = [];
  let primaryKey: ColumnInfo | undefined;
  // getTableColumns keys by the JS property name — the identity used to query and
  // the identity returned in rows — so capabilities expose those names.
  for (const [propKey, column] of Object.entries(getTableColumns(table) as Columns)) {
    const col: ColumnInfo = {
      name: propKey,
      jsonType: jsonType(column),
      nullable: !column.notNull && !column.primary,
      autoincrement: column.primary && jsonType(column) === 'integer',
      hasDefault: Boolean(column.hasDefault),
      unique: Boolean(column.isUnique),
    };
    columns.push(col);
    if (column.primary && !primaryKey) {
      primaryKey = { ...col };
    }
  }
  const plural = getTableName(table);
  const info: ModelInfo = { model: table, singular: singularize(plural), plural, columns };
  if (primaryKey) {
    info.primaryKey = primaryKey;
  }
  return info;
}

function jsonType(column: any): string {
  if (column.dataType === 'boolean') {
    return 'boolean';
  }
  if (column.dataType === 'number' || column.dataType === 'bigint') {
    return /int|serial/i.test(String(column.columnType)) ? 'integer' : 'number';
  }
  return 'string'; // string, date, json, and anything else
}

/** Runs the six operations via Drizzle's query builder against the caller's `db`. */
class DrizzleBackend implements Backend {
  constructor(private readonly db: DrizzleDb) {}

  private table(info: ModelInfo): DrizzleTable {
    return info.model;
  }

  private condition(info: ModelInfo, filters: Record<string, unknown>): unknown {
    const cols = getTableColumns(this.table(info)) as Columns;
    const conds = Object.entries(filters).map(([k, v]) => eq(cols[k], v));
    return conds.length > 0 ? and(...conds) : undefined;
  }

  async find(
    info: ModelInfo,
    filters: Record<string, unknown>,
    limit: number,
  ): Promise<Array<Record<string, unknown>>> {
    let q = this.db.select().from(this.table(info)).$dynamic();
    const cond = this.condition(info, filters);
    if (cond) {
      q = q.where(cond);
    }
    const rows: Array<Record<string, unknown>> = await q.limit(limit);
    return rows.map((r) => trimRow(info, r));
  }

  async count(info: ModelInfo, filters: Record<string, unknown>): Promise<number> {
    let q = this.db.select({ n: count() }).from(this.table(info)).$dynamic();
    const cond = this.condition(info, filters);
    if (cond) {
      q = q.where(cond);
    }
    const rows: Array<{ n: number }> = await q;
    return Number(rows[0]?.n ?? 0);
  }

  async get(info: ModelInfo, key: string, value: unknown): Promise<Record<string, unknown> | null> {
    const cols = getTableColumns(this.table(info)) as Columns;
    const rows: Array<Record<string, unknown>> = await this.db
      .select()
      .from(this.table(info))
      .where(eq(cols[key], value))
      .limit(1);
    return rows.length > 0 ? trimRow(info, rows[0]!) : null;
  }

  async create(info: ModelInfo, values: Record<string, unknown>): Promise<Record<string, unknown>> {
    await this.db.insert(this.table(info)).values(values);
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
    if (Object.keys(changes).length > 0) {
      const cols = getTableColumns(this.table(info)) as Columns;
      await this.db.update(this.table(info)).set(changes).where(eq(cols[key], value));
    }
    return this.get(info, key, value);
  }

  async delete(info: ModelInfo, key: string, value: unknown): Promise<void> {
    if (!(await this.get(info, key, value))) {
      throw missing(info, key, value);
    }
    const cols = getTableColumns(this.table(info)) as Columns;
    await this.db.delete(this.table(info)).where(eq(cols[key], value));
  }
}
