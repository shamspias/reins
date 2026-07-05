import type { Capability } from './types.js';

/**
 * The Capability Linter (invariant §2.15): a build-time check that a capability is
 * one a model can actually use — intent-level name, a real description, lean
 * parameters, trimmed responses. Auto-generated capabilities (the ORM adapters)
 * must pass it before exposure. The violation codes are language-neutral
 * (`spec/contract.md` §3) and asserted by the shared conformance suite.
 */

export const VIOLATION_OPAQUE_NAME = 'opaque_name';
export const VIOLATION_MISSING_DESCRIPTION = 'missing_description';
export const VIOLATION_TOO_MANY_PARAMS = 'too_many_params';
export const VIOLATION_RAW_RESPONSE = 'raw_response';

/** Keep generated capabilities lean; more than this many params fails the linter. */
export const MAX_PARAMS = 7;
/** A description shorter than this is treated as missing. */
export const MIN_DESCRIPTION_CHARS = 10;

const SNAKE_NAME = /^[a-z][a-z0-9_]*$/;

/** The result of linting one capability: the violation codes it triggered (sorted). */
export interface LintReport {
  violations: string[];
  ok: boolean;
}

/** Check a capability's agent-facing surface against the §2.15 quality bar. */
export function lint(
  name: string,
  description: string,
  paramCount: number,
  returnsRaw: boolean,
): LintReport {
  const violations: string[] = [];
  if (isOpaque(name)) {
    violations.push(VIOLATION_OPAQUE_NAME);
  }
  if (description.trim().length < MIN_DESCRIPTION_CHARS) {
    violations.push(VIOLATION_MISSING_DESCRIPTION);
  }
  if (paramCount > MAX_PARAMS) {
    violations.push(VIOLATION_TOO_MANY_PARAMS);
  }
  if (returnsRaw) {
    violations.push(VIOLATION_RAW_RESPONSE);
  }
  violations.sort();
  return { violations, ok: violations.length === 0 };
}

/** Lint a {@link Capability}'s spec (counting its schema's properties). */
export function lintCapability(cap: Capability, returnsRaw: boolean): LintReport {
  let count = 0;
  const props = cap.input_schema?.['properties'];
  if (typeof props === 'object' && props !== null) {
    count = Object.keys(props).length;
  }
  return lint(cap.name, cap.description, count, returnsRaw);
}

/**
 * Opaque = not clean lowercase snake_case, or built only from short abbreviation-
 * like tokens with no real word (`tbl_ord_upd` is opaque; `refund_order` /
 * `get_user` are fine).
 */
function isOpaque(name: string): boolean {
  if (!SNAKE_NAME.test(name)) {
    return true;
  }
  let longest = 0;
  for (const token of name.split('_')) {
    longest = Math.max(longest, token.length);
  }
  return longest < 4;
}
