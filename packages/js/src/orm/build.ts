import type { Capability, JSONSchema, SchemaIndex, TableInfo } from '../types.js';
import type { BoundCapability, Handler } from '../capability.js';
import { lintCapability } from '../linter.js';
import { ReinsError } from '../errors.js';

/**
 * The ORM-neutral capability-generation engine — the TypeScript mirror of Go's
 * `orm.go`. It turns introspected models (`ModelInfo`) into intent-level, linted
 * capabilities behind a `Backend`, so adding another ORM means implementing only
 * introspection + the six data operations. It has ZERO ORM dependencies; the
 * Drizzle adapter (drizzle.ts) plugs into it.
 */

const MAX_PARAMS = 7; // keep generated capabilities lean (linter §2.15)
const MAX_COLUMNS_RETURNED = 12; // trim wide rows so raw tables never flood context
const DEFAULT_READ_LIMIT = 50; // bounded result sets (§2.13)

/** Column names commonly used as human-facing keys, best first. */
const naturalKeyHints = ['slug', 'email', 'username', 'code', 'key', 'name', 'title', 'uuid'];

/** One column in ORM-neutral terms. */
export interface ColumnInfo {
  name: string;
  jsonType: string;
  nullable: boolean;
  autoincrement: boolean;
  hasDefault: boolean;
  unique: boolean;
}

/** One mapped model in ORM-neutral terms. `model` is the backend's opaque handle. */
export interface ModelInfo {
  model?: unknown;
  singular: string;
  plural: string;
  columns: ColumnInfo[];
  primaryKey?: ColumnInfo;
  description?: string;
}

/**
 * Runs the six data operations for one ORM, returning trimmed rows. `update` and
 * `delete` throw a "not found" error when the row is missing — surfaced to the model
 * as errors-as-data.
 */
export interface Backend {
  find(
    info: ModelInfo,
    filters: Record<string, unknown>,
    limit: number,
  ): Promise<Array<Record<string, unknown>>>;
  count(info: ModelInfo, filters: Record<string, unknown>): Promise<number>;
  get(info: ModelInfo, key: string, value: unknown): Promise<Record<string, unknown> | null>;
  create(info: ModelInfo, values: Record<string, unknown>): Promise<Record<string, unknown>>;
  update(
    info: ModelInfo,
    key: string,
    value: unknown,
    changes: Record<string, unknown>,
  ): Promise<Record<string, unknown> | null>;
  delete(info: ModelInfo, key: string, value: unknown): Promise<void>;
}

function optionalOnCreate(c: ColumnInfo): boolean {
  return c.nullable || c.autoincrement || c.hasDefault;
}

function naturalKey(info: ModelInfo): ColumnInfo | undefined {
  const pkName = info.primaryKey?.name;
  const uniqueStrings = new Map<string, ColumnInfo>();
  for (const c of info.columns) {
    if (c.unique && c.jsonType === 'string' && c.name !== pkName) {
      uniqueStrings.set(c.name, c);
    }
  }
  if (uniqueStrings.size === 0) {
    return undefined;
  }
  for (const hint of naturalKeyHints) {
    const c = uniqueStrings.get(hint);
    if (c) {
      return c;
    }
  }
  return [...uniqueStrings.values()][0];
}

function lookupKey(info: ModelInfo): ColumnInfo | undefined {
  return naturalKey(info) ?? info.primaryKey;
}

/**
 * Turn introspected models into linted capabilities plus a schema index. Reads are
 * always generated; writes only when `canWrite` is true. A generated capability
 * that fails the linter is dropped, never shipped (§2.15).
 */
export function buildCapabilities(
  backend: Backend,
  infos: ModelInfo[],
  canWrite: boolean,
  readLimit: number,
): { capabilities: BoundCapability[]; schema: SchemaIndex } {
  const limit = readLimit > 0 ? readLimit : DEFAULT_READ_LIMIT;
  const capabilities: BoundCapability[] = [];
  const tables: TableInfo[] = [];
  for (const info of infos) {
    const candidates = [
      ...readCaps(backend, info, limit),
      ...(canWrite ? writeCaps(backend, info) : []),
    ];
    for (const c of candidates) {
      if (lintCapability(c.spec, c.returnsRaw).ok) {
        capabilities.push(c);
      }
    }
    tables.push(tableInfo(info));
  }
  return { capabilities, schema: { tables } };
}

// --- read capabilities -------------------------------------------------------------

function readCaps(backend: Backend, info: ModelInfo, readLimit: number): BoundCapability[] {
  const caps = [findCap(backend, info, readLimit), countCap(backend, info)];
  if (lookupKey(info)) {
    caps.push(getCap(backend, info));
  }
  return caps;
}

function findCap(backend: Backend, info: ModelInfo, readLimit: number): BoundCapability {
  const filters = filterColumns(info);
  const spec: Capability = {
    name: `find_${info.plural}`,
    description: `Find ${info.plural} matching optional filters (returns up to ${readLimit} rows, trimmed).`,
    input_schema: objectSchema(columnTypes(filters), []),
    access: 'read',
    confirm: false,
    idempotent: false,
  };
  const handler: Handler = (_ctx, args) => backend.find(info, known(args, filters), readLimit);
  return { spec, handler, returnsRaw: false };
}

function countCap(backend: Backend, info: ModelInfo): BoundCapability {
  const filters = filterColumns(info);
  const spec: Capability = {
    name: `count_${info.plural}`,
    description: `Count ${info.plural} matching optional filters.`,
    input_schema: objectSchema(columnTypes(filters), []),
    access: 'read',
    confirm: false,
    idempotent: false,
  };
  const handler: Handler = (_ctx, args) => backend.count(info, known(args, filters));
  return { spec, handler, returnsRaw: false };
}

function getCap(backend: Backend, info: ModelInfo): BoundCapability {
  const key = lookupKey(info)!;
  const spec: Capability = {
    name: `get_${info.singular}_by_${key.name}`,
    description: `Get one ${info.singular} by its ${key.name} (returns the row, trimmed, or nothing if not found).`,
    input_schema: objectSchema({ [key.name]: key.jsonType }, [key.name]),
    access: 'read',
    confirm: false,
    idempotent: false,
  };
  const handler: Handler = async (_ctx, args) =>
    (await backend.get(info, key.name, args[key.name])) ?? null;
  return { spec, handler, returnsRaw: false };
}

// --- write capabilities (gated) ----------------------------------------------------

function writeCaps(backend: Backend, info: ModelInfo): BoundCapability[] {
  const caps = [createCap(backend, info)];
  if (lookupKey(info)) {
    caps.push(updateCap(backend, info), deleteCap(backend, info));
  }
  return caps;
}

function createCap(backend: Backend, info: ModelInfo): BoundCapability {
  const settable = settableColumns(info);
  const required = settable.filter((c) => !optionalOnCreate(c)).map((c) => c.name);
  const spec: Capability = {
    name: `create_${info.singular}`,
    description: `Create a new ${info.singular} (returns the created row, trimmed).`,
    input_schema: objectSchema(columnTypes(settable), required),
    access: 'write',
    confirm: false,
    idempotent: false,
  };
  const handler: Handler = (_ctx, args) => backend.create(info, known(args, settable));
  return { spec, handler, returnsRaw: false };
}

function updateCap(backend: Backend, info: ModelInfo): BoundCapability {
  const key = lookupKey(info)!;
  // Leave room for the required key under MAX_PARAMS.
  const nonKey = info.columns.filter((c) => !c.autoincrement && c.name !== key.name);
  let settable = prioritize(info, nonKey);
  if (settable.length > MAX_PARAMS - 1) {
    settable = settable.slice(0, MAX_PARAMS - 1);
  }
  const props: Record<string, string> = { [key.name]: key.jsonType };
  for (const c of settable) {
    props[c.name] = c.jsonType;
  }
  const spec: Capability = {
    name: `update_${info.singular}`,
    description: `Update an existing ${info.singular}, found by its ${key.name} (returns the updated row, trimmed).`,
    input_schema: objectSchema(props, [key.name]),
    access: 'write',
    confirm: false,
    idempotent: false,
  };
  const handler: Handler = (_ctx, args) =>
    backend.update(info, key.name, args[key.name], known(args, settable));
  return { spec, handler, returnsRaw: false };
}

function deleteCap(backend: Backend, info: ModelInfo): BoundCapability {
  const key = lookupKey(info)!;
  const spec: Capability = {
    name: `delete_${info.singular}`,
    description: `Delete a ${info.singular}, found by its ${key.name}.`,
    input_schema: objectSchema({ [key.name]: key.jsonType }, [key.name]),
    access: 'destructive',
    confirm: false,
    idempotent: false,
  };
  const handler: Handler = async (_ctx, args) => {
    await backend.delete(info, key.name, args[key.name]);
    return { deleted: args[key.name] };
  };
  return { spec, handler, returnsRaw: false };
}

// --- column selection + helpers ----------------------------------------------------

function filterColumns(info: ModelInfo): ColumnInfo[] {
  return cap7(prioritize(info, info.columns));
}

function settableColumns(info: ModelInfo): ColumnInfo[] {
  return cap7(
    prioritize(
      info,
      info.columns.filter((c) => !c.autoincrement),
    ),
  );
}

function cap7(cols: ColumnInfo[]): ColumnInfo[] {
  return cols.length > MAX_PARAMS ? cols.slice(0, MAX_PARAMS) : cols;
}

function prioritize(info: ModelInfo, columns: ColumnInfo[]): ColumnInfo[] {
  const keyNames = new Set<string>();
  const nk = naturalKey(info);
  if (nk) {
    keyNames.add(nk.name);
  }
  if (info.primaryKey) {
    keyNames.add(info.primaryKey.name);
  }
  const rank = (c: ColumnInfo): number => (keyNames.has(c.name) ? 0 : c.nullable ? 2 : 1);
  return [...columns].sort(
    (a, b) => rank(a) - rank(b) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0),
  );
}

function known(args: Record<string, unknown>, columns: ColumnInfo[]): Record<string, unknown> {
  const allowed = new Set(columns.map((c) => c.name));
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(args)) {
    if (allowed.has(k) && v !== undefined && v !== null) {
      out[k] = v;
    }
  }
  return out;
}

function columnTypes(columns: ColumnInfo[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const c of columns) {
    out[c.name] = c.jsonType;
  }
  return out;
}

function objectSchema(props: Record<string, string>, required: string[]): JSONSchema {
  const properties: Record<string, unknown> = {};
  for (const [name, jsonType] of Object.entries(props)) {
    properties[name] = { type: jsonType };
  }
  return { type: 'object', properties, required, additionalProperties: false };
}

function tableInfo(info: ModelInfo): TableInfo {
  const description = (info.description ?? '').trim() || `The ${info.plural} table.`;
  return { name: info.plural, description, columns: info.columns.map((c) => c.name) };
}

/** A "not found" error for update/delete of a missing row (errors-as-data via the loop). */
export function missing(info: ModelInfo, key: string, value: unknown): ReinsError {
  return new ReinsError(
    `no ${info.singular} with ${key}=${String(value)}`,
    'check the key, or find_ the row first',
  );
}

/** Trim a row to the first {@link MAX_COLUMNS_RETURNED} known columns, JSON-cleaned. */
export function trimRow(info: ModelInfo, row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const c of info.columns.slice(0, MAX_COLUMNS_RETURNED)) {
    out[c.name] = jsonValue(row[c.name]);
  }
  return out;
}

function jsonValue(v: unknown): unknown {
  if (v instanceof Date) {
    return v.toISOString();
  }
  return v;
}

/**
 * A deliberately naive English de-pluralizer for capability names
 * (`get_customer_by_id` from a `customers` table). A wrong guess is still a valid
 * name — just less pretty.
 */
export function singularize(word: string): string {
  const lower = word.toLowerCase();
  if (lower.endsWith('ies') && word.length > 3) {
    return word.slice(0, -3) + 'y';
  }
  if (/(ses|xes|zes|ches|shes)$/.test(lower)) {
    return word.slice(0, -2);
  }
  if (lower.endsWith('s') && !lower.endsWith('ss') && word.length > 1) {
    return word.slice(0, -1);
  }
  return word;
}
