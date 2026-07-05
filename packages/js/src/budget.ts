import type { Usage } from './types.js';

/**
 * Budget caps a run so it stops deterministically (invariant §2.13) — a runaway
 * loop is a bug, not an edge case. M7.3 mirrors the Python and Go caps (turns +
 * tokens); cost and time join with the rest of the token engine.
 */
export interface Budget {
  /**
   * Bounds the number of model calls, INCLUDING the final-answer turn: a goal
   * needing k rounds of tool calls plus an answer needs `max_turns >= k + 1`.
   */
  max_turns: number;
  /** Bounds total token spend (undefined/0 = uncapped). */
  max_tokens?: number;
}

/** The out-of-the-box cap (8 turns, tokens uncapped). */
export function defaultBudget(): Budget {
  return { max_turns: 8 };
}

/** Whether cumulative usage has exceeded the token cap. */
export function overTokens(budget: Budget, usage: Usage): boolean {
  if (!budget.max_tokens) {
    return false;
  }
  return usage.input_tokens + usage.output_tokens > budget.max_tokens;
}
