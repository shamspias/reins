import type { BoundCapability } from './capability.js';
import type { Capability, CapabilityResult, Context, JSONSchema, ToolSpec } from './types.js';
import { resultOk, resultError, toolSpec } from './types.js';
import { ReinsError } from './errors.js';
import { scopeViolation } from './rls.js';

/**
 * CapabilityRegistry is the only path by which the agent reaches a capability. It
 * validates arguments against the schema BEFORE executing (model output is
 * untrusted — the schema is the prepared statement, invariant §2.6), then invokes
 * the handler and wraps the outcome as errors-as-data ({@link CapabilityResult}).
 * Only control-plane misuse (duplicate registration) throws.
 */
export class CapabilityRegistry {
  private readonly caps = new Map<string, BoundCapability>();
  private readonly order: string[] = [];

  /** Register a capability; a duplicate name is a control-plane error. */
  register(cap: BoundCapability): void {
    if (this.caps.has(cap.spec.name)) {
      throw new ReinsError(
        `capability "${cap.spec.name}" is already registered`,
        'give each capability a unique name',
      );
    }
    this.caps.set(cap.spec.name, cap);
    this.order.push(cap.spec.name);
  }

  /** The capability specs in registration order. */
  list(): Capability[] {
    return this.order.map((name) => this.caps.get(name)!.spec);
  }

  /** The intent surfaces shown to the model (never policy metadata, §2.5). */
  toolSpecs(): ToolSpec[] {
    return this.order.map((name) => toolSpec(this.caps.get(name)!.spec));
  }

  /** The bound capability, or undefined if it is not registered. */
  get(name: string): BoundCapability | undefined {
    return this.caps.get(name);
  }

  /** Check args against the capability's schema; throws a hinted error if invalid. */
  validate(name: string, args: Record<string, unknown>): void {
    const cap = this.caps.get(name);
    if (!cap) {
      throw new ReinsError(`no capability named "${name}"`, `available: ${this.order.join(', ')}`);
    }
    const err = validateArgs(cap.spec.input_schema, args, name);
    if (err) {
      throw err;
    }
  }

  /**
   * Validate then invoke, returning the outcome as data (never throwing over the
   * loop boundary). The run's {@link Context} is passed to the handler (identity,
   * §2.7).
   */
  async call(name: string, args: Record<string, unknown>, ctx: Context): Promise<CapabilityResult> {
    const cap = this.caps.get(name);
    if (!cap) {
      const matches = closestMatches(name, this.order, 3);
      const fix = matches.length
        ? `closest matches: ${matches.join(', ')}`
        : 'register it with a capability';
      return resultError(`no capability named "${name}"\n  → ${fix}`);
    }
    // RLS before anything runs (§2.8): the registry is the only path to a
    // capability, so it guards scope itself — not just the loop (defense in depth).
    const violation = scopeViolation(cap.spec, ctx);
    if (violation) {
      return resultError(violation);
    }
    const err = validateArgs(cap.spec.input_schema, args, name);
    if (err) {
      return resultError(err.message);
    }
    try {
      const value = await cap.handler(ctx, args);
      return resultOk(value);
    } catch (e) {
      return resultError(e instanceof Error ? e.message : String(e));
    }
  }
}

// --- schema validation (top-level keys + JSON types; fail-closed) ------------------

/** Returns a hinted error if args do not satisfy the schema, else null. */
export function validateArgs(
  schema: JSONSchema | undefined,
  args: Record<string, unknown>,
  capName: string,
): ReinsError | null {
  const properties = asObject(schema?.['properties']) ?? {};
  const allowExtra = schema?.['additionalProperties'] === true;

  // Use own-property checks throughout: args are untrusted model output (§2.6), so
  // keys like "toString"/"constructor"/"__proto__" must not resolve via the
  // prototype chain and slip past the schema (Go's maps have no such chain).
  for (const key of requiredKeys(schema)) {
    if (!Object.hasOwn(args, key)) {
      return new ReinsError(`${capName}: missing required argument "${key}"`, `include ${key}`);
    }
  }
  for (const [key, value] of Object.entries(args)) {
    if (!Object.hasOwn(properties, key)) {
      if (allowExtra) {
        continue;
      }
      return new ReinsError(
        `${capName}: unexpected argument "${key}"`,
        `allowed: ${Object.keys(properties).sort().join(', ')}`,
      );
    }
    const expected = asObject(properties[key])?.['type'];
    if (typeof expected === 'string' && !typeMatches(expected, value)) {
      return new ReinsError(
        `${capName}: argument "${key}" must be of type ${expected}`,
        `got ${describeType(value)}`,
      );
    }
  }
  return null;
}

function requiredKeys(schema: JSONSchema | undefined): string[] {
  const req = schema?.['required'];
  if (Array.isArray(req)) {
    return req.filter((v): v is string => typeof v === 'string');
  }
  return [];
}

function typeMatches(expected: string, value: unknown): boolean {
  switch (expected) {
    case 'integer':
      return typeof value === 'number' && Number.isInteger(value);
    case 'number':
      return typeof value === 'number';
    case 'boolean':
      return typeof value === 'boolean';
    case 'string':
      return typeof value === 'string';
    case 'array':
      return Array.isArray(value);
    case 'object':
      return typeof value === 'object' && value !== null && !Array.isArray(value);
    default:
      return true; // unknown type — accept
  }
}

function asObject(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function describeType(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

// --- closest-match suggestions ----------------------------------------------------

/**
 * Up to n candidates within a small edit distance of name, nearest first — the
 * "did you mean" for an unknown capability (mirrors Python's difflib and the Go
 * Levenshtein helper).
 */
export function closestMatches(name: string, candidates: string[], n: number): string[] {
  const cutoff = Math.floor(name.length / 2) + 1;
  return candidates
    .map((c) => ({ name: c, dist: levenshtein(name, c) }))
    .filter((s) => s.dist <= cutoff)
    .sort((a, b) => a.dist - b.dist)
    .slice(0, n)
    .map((s) => s.name);
}

function levenshtein(a: string, b: string): number {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(prev[j]! + 1, cur[j - 1]! + 1, prev[j - 1]! + cost);
    }
    prev = cur;
  }
  return prev[b.length]!;
}
