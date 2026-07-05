import type { Capability, Message, ToolCall } from './types.js';

/**
 * The policy engine (M7.4): deterministic, code-owned governance for writes. Reads
 * run freely; writes are gated by the autonomy ladder
 * (read_only → draft_writes → approved_writes → trusted, invariant §2.10). Its
 * constraints are enforced here, never described to the model (§2.5). The default
 * never auto-executes a write, and destructive ops are never auto-executed.
 */
export type AutonomyLevel = 'read_only' | 'draft_writes' | 'approved_writes' | 'trusted';

export type Decision = 'allow' | 'deny' | 'require_approval';

/** A verdict plus an optional machine-readable reason. */
export interface PolicyDecision {
  decision: Decision;
  reason: string;
}

/** One capability call the model wants to make, with its descriptor. */
export interface ProposedAction {
  call: ToolCall;
  capability: Capability;
}

/**
 * Policy governs a run's actions. `checkActions` runs before execution;
 * `checkOutput` after (e.g. PII redaction, a later milestone).
 */
export interface Policy {
  checkActions(actions: ProposedAction[], autonomy: AutonomyLevel): PolicyDecision;
  checkOutput(output: Message): PolicyDecision;
}

const decisionRank: Record<Decision, number> = { allow: 0, require_approval: 1, deny: 2 };

/** The autonomy ladder: reads free, writes gated. */
export class DefaultPolicy implements Policy {
  checkActions(actions: ProposedAction[], autonomy: AutonomyLevel): PolicyDecision {
    let verdict: PolicyDecision = { decision: 'allow', reason: '' };
    for (const a of actions) {
      // The batch takes the most restrictive decision.
      const candidate = decideOne(a.capability, autonomy);
      if (decisionRank[candidate.decision] > decisionRank[verdict.decision]) {
        verdict = candidate;
      }
    }
    return verdict;
  }

  checkOutput(_output: Message): PolicyDecision {
    return { decision: 'allow', reason: '' };
  }
}

function decideOne(cap: Capability, autonomy: AutonomyLevel): PolicyDecision {
  if (cap.access === 'read') {
    return { decision: 'allow', reason: '' }; // reads run freely (§2.2)
  }
  switch (autonomy) {
    case 'read_only':
      return { decision: 'deny', reason: 'write_in_read_only' };
    case 'draft_writes':
      return { decision: 'deny', reason: 'draft_only' };
    case 'approved_writes':
      return { decision: 'require_approval', reason: 'approval_required' };
    default: // trusted: plain writes auto-allow; destructive or confirm-marked stay gated
      if (cap.access === 'destructive' || cap.confirm) {
        return { decision: 'require_approval', reason: 'approval_required' };
      }
      return { decision: 'allow', reason: '' };
  }
}
