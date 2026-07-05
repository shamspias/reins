import { randomBytes } from 'node:crypto';
import type {
  CapabilityResult,
  Context,
  FinishReason,
  Message,
  ModelResponse,
  RunResult,
  RunState,
  Usage,
} from './types.js';
import type { Model } from './model.js';
import type { BoundCapability } from './capability.js';
import { CapabilityRegistry } from './registry.js';
import { defaultModelParams } from './types.js';
import { type Budget, defaultBudget, overTokens } from './budget.js';
import { GoalReached, MaxTurns, type StopCondition, firstStop } from './stop.js';

/**
 * The harness loop and the Agent — the TypeScript mirror of the ETCSLV lifecycle:
 * Engage → (Think → Call → Sense → Loop?) → Verify. The model is the only
 * stochastic step; dispatch and stopping are deterministic and owned here
 * (invariant §2.14).
 *
 * ask() is structurally read-only — a requested write is refused before anything
 * executes (§2.1). run() may write, but the policy → approval → audit gate arrives
 * in M7.4; until then a write under run() fails closed (denied, never executed).
 * The caller's principal propagates as a Context into every call (§2.7).
 */

const SYSTEM_PROMPT =
  'You operate an application through its registered capabilities. ' +
  "Use them to accomplish the user's goal, then reply with a final answer.";

function newTraceId(): string {
  return randomBytes(16).toString('hex');
}

/** The outcome of a run: `completed` | `refused` | `not_executed` | `stopped`. */
export type Outcome = 'completed' | 'refused' | 'not_executed' | 'stopped';

/** The detailed result of one run: outcome plus telemetry. */
export interface RunReport {
  outcome: Outcome;
  output: Message;
  finish: FinishReason;
  usage: Usage;
  turn: number;
  executed: string[];
  reason: string;
  trace_id: string;
}

/** Per-run governance. M7.3 wires identity and the budget; safety options arrive in M7.4. */
export interface RunOptions {
  principal?: string;
  budget?: Budget;
}

/**
 * Drive the loop. A control-plane failure (the model rejecting) propagates as a
 * thrown error; everything the model does wrong is data.
 */
export async function runLoop(
  model: Model,
  registry: CapabilityRegistry,
  goal: string,
  canWrite: boolean,
  options: RunOptions = {},
): Promise<RunReport> {
  const budget = options.budget ?? defaultBudget();
  const maxTurns = budget.max_turns || defaultBudget().max_turns;
  const traceId = newTraceId();
  const ctx: Context = { principal: options.principal, trace_id: traceId };

  const messages: Message[] = [
    { role: 'system', text: SYSTEM_PROMPT },
    { role: 'user', text: goal },
  ];
  const stops: StopCondition[] = [new GoalReached(), new MaxTurns(maxTurns)];
  const executed: string[] = [];
  const params = defaultModelParams();

  let usage: Usage = { input_tokens: 0, output_tokens: 0, cost: 0 };
  let last: ModelResponse | undefined;
  let turn = 0;

  for (;;) {
    if (overTokens(budget, usage)) {
      return report(
        'stopped',
        synthetic('Stopped: token budget exhausted.\n  → raise max_tokens or simplify the goal.'),
        'interrupted',
        usage,
        turn,
        executed,
        'budget_exhausted',
        traceId,
      );
    }

    const resp = await model.complete(messages, registry.toolSpecs(), params);
    usage = addUsage(usage, resp.usage);
    messages.push(resp.message);
    last = resp;

    if (resp.finish_reason === 'tool_calls' && (resp.message.tool_calls?.length ?? 0) > 0) {
      const blocked = await act(
        registry,
        resp.message.tool_calls!,
        canWrite,
        ctx,
        messages,
        executed,
        usage,
        turn,
        traceId,
      );
      if (blocked) {
        return blocked;
      }
    }

    turn += 1;
    const state: RunState = {
      messages,
      turn,
      cumulative_usage: usage,
      last_response: last,
    };
    const stop = firstStop(stops, state);
    if (stop.stop) {
      if (stop.reason === 'goal_reached') {
        return report('completed', resp.message, 'stop', usage, turn, executed, '', traceId);
      }
      return report(
        'stopped',
        synthetic('Stopped: turn budget exhausted.\n  → raise the budget or simplify the goal.'),
        'interrupted',
        usage,
        turn,
        executed,
        'budget_exhausted',
        traceId,
      );
    }
  }
}

/**
 * Execute one turn's tool calls. Returns a terminal report if the turn is refused
 * (ask + write) or denied (run + write, until M7.4), otherwise mutates `messages`
 * and `executed` and returns undefined so the loop continues.
 */
async function act(
  registry: CapabilityRegistry,
  toolCalls: NonNullable<Message['tool_calls']>,
  canWrite: boolean,
  ctx: Context,
  messages: Message[],
  executed: string[],
  usage: Usage,
  turn: number,
  traceId: string,
): Promise<RunReport | undefined> {
  const items = toolCalls.map((call) => ({ call, cap: registry.get(call.name) }));

  for (const { call, cap } of items) {
    if (cap && cap.spec.access !== 'read') {
      if (!canWrite) {
        // ask(): structurally read-only — refuse the whole turn (§2.1).
        return report(
          'refused',
          synthetic(
            `Refused: "${call.name}" would write, but this is a read-only ask().\n  → use run(...) if writing is intended.`,
          ),
          'stop',
          usage,
          turn,
          executed,
          'write_in_read_only',
          traceId,
        );
      }
      // run(): the policy/approval gate arrives in M7.4; until then writes fail closed.
      return report(
        'not_executed',
        synthetic(
          `Not executed: "${call.name}" is a write, which needs an approval policy.\n  → configure an autonomy level and approval handler (M7.4).`,
        ),
        'stop',
        usage,
        turn,
        executed,
        'write_denied',
        traceId,
      );
    }
  }

  for (const { call, cap } of items) {
    const result = await registry.call(call.name, call.arguments ?? {}, ctx);
    if (cap) {
      executed.push(call.name);
    }
    messages.push(toolMessage(call.id, result));
  }
  return undefined;
}

function toolMessage(callId: string, result: CapabilityResult): Message {
  const text = result.ok ? stringifyValue(result.value) : (result.error ?? '');
  return { role: 'tool', tool_call_id: callId, text };
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) {
    return '';
  }
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function synthetic(text: string): Message {
  return { role: 'assistant', text };
}

function addUsage(a: Usage, b: Usage): Usage {
  return {
    input_tokens: a.input_tokens + b.input_tokens,
    output_tokens: a.output_tokens + b.output_tokens,
    cost: a.cost + b.cost,
  };
}

function report(
  outcome: Outcome,
  output: Message,
  finish: FinishReason,
  usage: Usage,
  turn: number,
  executed: string[],
  reason: string,
  traceId: string,
): RunReport {
  return {
    outcome,
    output,
    finish,
    usage,
    turn,
    executed: [...executed],
    reason,
    trace_id: traceId,
  };
}

// --- the Agent --------------------------------------------------------------------

/** Configuration for an {@link Agent}. Safety options (autonomy, approval, audit) arrive in M7.4. */
export interface AgentConfig {
  capabilities?: BoundCapability[];
  budget?: Budget;
  /** Who the agent acts for — propagated into every capability call (identity → RLS). */
  principal?: string;
}

/**
 * The user-facing harness: give it your capabilities, hand it a goal. `ask()` is
 * read-only and never prompts; `run()` may write (gated by the autonomy ladder from
 * M7.4 onward). A duplicate capability name is a control-plane error (thrown).
 */
export class Agent {
  readonly registry: CapabilityRegistry;
  private readonly model: Model;
  private readonly budget: Budget;
  private readonly principal: string | undefined;

  constructor(model: Model, config: AgentConfig = {}) {
    this.model = model;
    this.budget = config.budget ?? defaultBudget();
    this.principal = config.principal;
    this.registry = new CapabilityRegistry();
    for (const cap of config.capabilities ?? []) {
      this.registry.register(cap);
    }
  }

  /** Accomplish a goal using only read capabilities (structurally read-only). */
  ask(goal: string): Promise<RunResult> {
    return this.drive(goal, false);
  }

  /** Accomplish a goal that may write; writes are gated by the autonomy ladder (M7.4). */
  run(goal: string): Promise<RunResult> {
    return this.drive(goal, true);
  }

  private async drive(goal: string, canWrite: boolean): Promise<RunResult> {
    const rep = await runLoop(this.model, this.registry, goal, canWrite, {
      principal: this.principal,
      budget: this.budget,
    });
    return { output: rep.output, reason: rep.finish, usage: rep.usage, trace_id: rep.trace_id };
  }
}
