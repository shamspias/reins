import type { RunState } from './types.js';

/** The outcome of evaluating a stop condition. */
export interface StopVerdict {
  reason: string;
  stop: boolean;
}

/**
 * A StopCondition decides whether a run should stop, given its evolving state. The
 * loop owns stopping deterministically; conditions are pure and testable.
 */
export interface StopCondition {
  evaluate(state: RunState): StopVerdict;
}

/** Stops when the model produced a final answer (finish reason `'stop'`). */
export class GoalReached implements StopCondition {
  evaluate(state: RunState): StopVerdict {
    if (state.last_response?.finish_reason === 'stop') {
      return { reason: 'goal_reached', stop: true };
    }
    return { reason: '', stop: false };
  }
}

/** Stops once the turn count reaches the cap. */
export class MaxTurns implements StopCondition {
  constructor(private readonly limit: number) {}

  evaluate(state: RunState): StopVerdict {
    if (state.turn >= this.limit) {
      return { reason: 'max_turns', stop: true };
    }
    return { reason: '', stop: false };
  }
}

/** The reason of the first condition that fires, or a non-stop verdict. */
export function firstStop(conditions: StopCondition[], state: RunState): StopVerdict {
  for (const c of conditions) {
    const verdict = c.evaluate(state);
    if (verdict.stop) {
      return verdict;
    }
  }
  return { reason: '', stop: false };
}
