import type { Capability, Context, JSONSchema } from './types.js';
import { classifyWith } from './classify.js';
import { ReinsError } from './errors.js';

/**
 * A capability handler runs one operation. It receives the run's {@link Context}
 * (identity, trace id) and the validated argument map, and returns the result (or
 * a promise of it). Failures thrown here become errors-as-data the model can see;
 * they never abort the loop.
 */
export type Handler = (ctx: Context, args: Record<string, unknown>) => unknown | Promise<unknown>;

/** A capability's spec plus the function that runs it (the Go/Python `BoundCapability`). */
export interface BoundCapability {
  spec: Capability;
  handler: Handler;
  /** Whether the handler returns raw rows/blobs — for response linting (§2.15). */
  returnsRaw: boolean;
}

/** Options for {@link capability}. `parameters` is a JSON Schema object. */
export interface CapabilityConfig {
  description: string;
  /**
   * A JSON Schema for the arguments. Build one with {@link params}, hand-write it,
   * or (with Zod v4) pass `z.toJSONSchema(mySchema)`.
   */
  parameters?: JSONSchema;
  /** Force READ classification (mutually exclusive with `destructive`). */
  reads?: boolean;
  /** Force DESTRUCTIVE classification. */
  destructive?: boolean;
  /** Require approval for this write/destructive capability (reads never gate). */
  confirm?: boolean;
  /** Mark the capability safe to retry. */
  idempotent?: boolean;
  /** Tag the capability for row-level security (only the caller's rows, §2.8). */
  scope?: string;
  /** Declare that the handler returns untrimmed raw data (fails the linter). */
  returnsRaw?: boolean;
}

/**
 * Define a capability: a typed, intent-named function plus safety metadata. Access
 * is classified from the name unless `reads`/`destructive` overrides it. The
 * optional type parameter `Args` types the handler's argument object for the caller;
 * at runtime the harness passes the validated argument map.
 */
export function capability<Args extends Record<string, unknown> = Record<string, unknown>>(
  name: string,
  config: CapabilityConfig,
  handler: (ctx: Context, args: Args) => unknown | Promise<unknown>,
): BoundCapability {
  const spec: Capability = {
    name,
    description: config.description,
    access: classifyWith(name, config.reads ?? false, config.destructive ?? false),
    confirm: config.confirm ?? false,
    idempotent: config.idempotent ?? false,
  };
  if (config.parameters !== undefined) {
    spec.input_schema = config.parameters;
  }
  if (config.scope !== undefined) {
    spec.scope = config.scope;
  }
  return { spec, handler: handler as Handler, returnsRaw: config.returnsRaw ?? false };
}

const scalarTypes = new Set(['string', 'integer', 'number', 'boolean']);

/**
 * A tiny JSON Schema builder so you never hand-write schemas or reach for Zod for
 * simple cases. Each value is a scalar type — `string`, `integer`, `number`,
 * `boolean` — optionally suffixed `[]` (array) and/or `?` (optional). Fields
 * without `?` are required.
 *
 * @example
 * params({ order_id: 'integer', reason: 'string?', tags: 'string[]' })
 */
export function params(fields: Record<string, string>): JSONSchema {
  const properties: Record<string, unknown> = {};
  const required: string[] = [];
  for (const [field, rawType] of Object.entries(fields)) {
    let spec = rawType.trim();
    let optional = false;
    if (spec.endsWith('?')) {
      optional = true;
      spec = spec.slice(0, -1);
    }
    let isArray = false;
    if (spec.endsWith('[]')) {
      isArray = true;
      spec = spec.slice(0, -2);
    }
    if (!scalarTypes.has(spec)) {
      throw new ReinsError(
        `unknown parameter type "${rawType}" for field "${field}"`,
        'use string, integer, number, or boolean (optionally with [] or ? — e.g. "string[]", "integer?")',
      );
    }
    properties[field] = isArray ? { type: 'array', items: { type: spec } } : { type: spec };
    if (!optional) {
      required.push(field);
    }
  }
  return { type: 'object', properties, required, additionalProperties: false };
}
