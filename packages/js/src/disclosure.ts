import type { CapabilityResult, JSONSchema, SchemaIndex, ToolCall, ToolSpec } from './types.js';
import { resultOk, resultError, toolSpec } from './types.js';
import type { CapabilityRegistry } from './registry.js';
import { ReinsError } from './errors.js';

/**
 * Dual progressive disclosure (M7.5, invariant §2.12): never dump every capability
 * schema or the whole data model into context. Above a small threshold the model
 * sees one constant-size `search_capabilities` tool instead of the full registry;
 * matches are exposed and become directly callable. A {@link SchemaIndex}, when
 * attached, is likewise reachable only through `search_schema`. Mirrors the Python
 * and Go ports so the base-context behavior is language-neutral (asserted by the
 * shared conformance suite).
 */

export const SEARCH_CAPABILITIES = 'search_capabilities';
export const SEARCH_SCHEMA = 'search_schema';

const DEFER_AFTER = 12; // above this many capabilities, defer them behind search
const MAX_CAPABILITY_MATCH = 8;
const MAX_TABLE_MATCH = 5;
const MAX_QUERY_CHARS = 200;

const querySchema: JSONSchema = {
  type: 'object',
  properties: { query: { type: 'string' } },
  required: ['query'],
  additionalProperties: false,
};

const searchCapabilitiesSpec: ToolSpec = {
  name: SEARCH_CAPABILITIES,
  description:
    'Search the available operations by keyword and load the matches. ' +
    'Call this first to find the operations you need; matches become directly callable.',
  input_schema: querySchema,
};

const searchSchemaSpec: ToolSpec = {
  name: SEARCH_SCHEMA,
  description: "Search the application's data model by keyword. Returns only the relevant tables.",
  input_schema: querySchema,
};

/**
 * Per-run state: what the model can currently see. `forceDefer` undefined (auto)
 * defers capabilities behind search when more than {@link DEFER_AFTER} are
 * registered; a boolean forces it on or off.
 */
export class Disclosure {
  private readonly exposed = new Set<string>();

  constructor(
    private readonly registry: CapabilityRegistry,
    private readonly schema?: SchemaIndex,
    private readonly forceDefer?: boolean,
  ) {
    if (this.deferred() && registry.get(SEARCH_CAPABILITIES)) {
      throw new ReinsError(
        `capability "${SEARCH_CAPABILITIES}" collides with the built-in discovery tool`,
        'rename your capability — that name is reserved while disclosure is active',
      );
    }
    if (schema && registry.get(SEARCH_SCHEMA)) {
      throw new ReinsError(
        `capability "${SEARCH_SCHEMA}" collides with the built-in discovery tool`,
        'rename your capability — that name is reserved while a schema is attached',
      );
    }
  }

  private deferred(): boolean {
    return this.forceDefer ?? this.registry.list().length > DEFER_AFTER;
  }

  /** The tool surface for the next model turn — tiny and constant when deferred. */
  tools(): ToolSpec[] {
    const specs: ToolSpec[] = [];
    if (this.deferred()) {
      specs.push(searchCapabilitiesSpec);
      for (const name of [...this.exposed].sort()) {
        const cap = this.registry.get(name);
        if (cap) {
          specs.push(toolSpec(cap.spec));
        }
      }
    } else {
      specs.push(...this.registry.toolSpecs());
    }
    if (this.schema) {
      specs.push(searchSchemaSpec);
    }
    return specs;
  }

  /**
   * Handle a discovery call, or return undefined if `call` is not one. Discovery is
   * a harness-internal read: deterministic, never gated, allowed under ask().
   */
  answer(call: ToolCall): CapabilityResult | undefined {
    if (call.name === SEARCH_CAPABILITIES && this.deferred()) {
      return this.query(call, (q) => this.searchCaps(q));
    }
    if (call.name === SEARCH_SCHEMA && this.schema) {
      return this.query(call, (q) => this.searchTables(q));
    }
    return undefined;
  }

  private query(call: ToolCall, handler: (q: string) => CapabilityResult): CapabilityResult {
    const raw = call.arguments?.['query'];
    let q = typeof raw === 'string' ? raw.trim() : '';
    if (q === '') {
      return resultError(
        `${call.name}: 'query' must be a non-empty string\n  → pass keywords, e.g. query="refund order"`,
      );
    }
    if (q.length > MAX_QUERY_CHARS) {
      q = q.slice(0, MAX_QUERY_CHARS);
    }
    return handler(q);
  }

  private searchCaps(query: string): CapabilityResult {
    const tokens = tokenize(query);
    const ranked = this.registry
      .list()
      .map((spec) => ({ spec, score: score(tokens, spec.name, spec.description) }))
      .filter((r) => r.score > 0)
      .sort((a, b) => b.score - a.score || compareStr(a.spec.name, b.spec.name));
    if (ranked.length === 0) {
      return resultError(`no operations matched "${query}"\n  → try different keywords`);
    }
    const lines = ['Found operation(s); they are now directly callable:'];
    for (const r of ranked.slice(0, MAX_CAPABILITY_MATCH)) {
      this.exposed.add(r.spec.name);
      lines.push(`  ${r.spec.name}(${schemaParamNames(r.spec.input_schema).join(', ')})`);
    }
    return resultOk(lines.join('\n'));
  }

  private searchTables(query: string): CapabilityResult {
    const tokens = tokenize(query);
    const tables = this.schema?.tables ?? [];
    const ranked = tables
      .map((table) => ({ table, score: score(tokens, table.name, table.description ?? '') }))
      .filter((r) => r.score > 0)
      .sort((a, b) => b.score - a.score || compareStr(a.table.name, b.table.name));
    if (ranked.length === 0) {
      return resultError(`no tables matched "${query}"\n  → try different keywords`);
    }
    const lines = ['Found table(s):'];
    for (const r of ranked.slice(0, MAX_TABLE_MATCH)) {
      lines.push(`  ${r.table.name} (fields: ${(r.table.columns ?? []).join(', ')})`);
    }
    return resultOk(lines.join('\n'));
  }
}

function schemaParamNames(schema?: JSONSchema): string[] {
  const props = schema?.['properties'];
  if (typeof props === 'object' && props !== null) {
    return Object.keys(props).sort();
  }
  return [];
}

function compareStr(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/** Lowercase alphanumeric word set — the shared tokenization for keyword matching. */
function tokenize(text: string): Set<string> {
  const out = new Set<string>();
  for (const word of text.toLowerCase().split(/[^a-z0-9]+/)) {
    if (word) {
      out.add(word);
    }
  }
  return out;
}

/** Keyword relevance: exact name token +3, name substring +2, description token +1. */
function score(query: Set<string>, name: string, description: string): number {
  const nameTokens = tokenize(name);
  const descTokens = tokenize(description);
  let total = 0;
  for (const token of query) {
    if (nameTokens.has(token)) {
      total += 3;
    } else {
      for (const nt of nameTokens) {
        if (nt.includes(token)) {
          total += 2;
          break;
        }
      }
    }
    if (descTokens.has(token)) {
      total += 1;
    }
  }
  return total;
}
