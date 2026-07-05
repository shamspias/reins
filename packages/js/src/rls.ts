import type { Capability, Context } from './types.js';

/**
 * Row-level security (M7.4): capabilities are scoped to the caller's identity. The
 * principal set at the entry point reaches every call as `Context.principal` (§2.7),
 * and a scope-annotated capability refuses to run without one (fail closed, §2.8).
 * The capability's own code (or the ORM adapters) then filters rows — Reins never
 * guesses which rows belong to whom.
 */

/**
 * Why a call must not run, or null if scoping is satisfied. A scope-annotated
 * capability requires an authenticated principal.
 */
export function scopeViolation(cap: Capability, ctx: Context): string | null {
  if (!cap.scope) {
    return null;
  }
  if (!ctx.principal) {
    return (
      `"${cap.name}" is scoped to "${cap.scope}" but the run has no principal` +
      '\n  → set a principal on the Agent (or the request identity)'
    );
  }
  return null;
}
