import { randomBytes } from 'node:crypto';
import type {
  CapabilityResult,
  Context,
  FinishReason,
  Message,
  ModelResponse,
  RunResult,
  RunState,
  SchemaIndex,
  ToolCall,
  ToolSpec,
  Usage,
} from './types.js';
import type { Model } from './model.js';
import type { BoundCapability } from './capability.js';
import type { Sandbox } from './sandbox.js';
import { CapabilityRegistry } from './registry.js';
import { resultOk, resultError, defaultModelParams } from './types.js';
import { type Budget, defaultBudget, overTokens } from './budget.js';
import { GoalReached, MaxTurns, type StopCondition, firstStop } from './stop.js';
import { type AutonomyLevel, type Policy, type ProposedAction, DefaultPolicy } from './policy.js';
import {
  type ApprovalHandler,
  type ApprovalFn,
  type ApprovalRequest,
  bulkCount,
  denyAll,
  toApprovalHandler,
} from './approval.js';
import { type AuditRecord, type AuditSink, MemoryAuditSink, redactArguments } from './audit.js';
import { scopeViolation } from './rls.js';
import { Disclosure } from './disclosure.js';

/**
 * The harness loop and the Agent — the TypeScript mirror of the ETCSLV lifecycle:
 * Engage → (Think → Call → Sense → Loop?) → Verify. The model is the only
 * stochastic step; dispatch, policy, and the gate are deterministic and owned here
 * (invariant §2.14).
 *
 * ask() is structurally read-only — a requested write is refused before anything
 * executes (§2.1). run() may write; a write travels policy → human approval → audit
 * → execution (M7.4). Every write attempt is audited, PII-redacted, and a write
 * that cannot be recorded does not run (no audit, no action). The caller's principal
 * propagates as a Context into every call (§2.7); the gate and policy fail closed,
 * and neither is described to the model (§2.5).
 */

const SYSTEM_PROMPT =
  'You operate an application through its registered capabilities. ' +
  "Use them to accomplish the user's goal, then reply with a final answer.";

const RUN_PROGRAM = 'run_program';
const MAX_PROGRAM_CHARS = 8_000;

// The code-mode tool, offered only when a sandbox is configured (§2.11). The
// program's capability calls are bridged back through the full gate chain.
const runProgramSpec: ToolSpec = {
  name: RUN_PROGRAM,
  description:
    'Run a short JavaScript program in a sandbox. Every available operation is an ' +
    'async function you can await; print(...) what you want back. Bulk data stays in ' +
    'the sandbox — only what you print returns.',
  input_schema: {
    type: 'object',
    properties: { code: { type: 'string' } },
    required: ['code'],
    additionalProperties: false,
  },
};

function newTraceId(): string {
  return randomBytes(16).toString('hex');
}

/** The outcome of a run. */
export type Outcome = 'completed' | 'executed' | 'refused' | 'not_executed' | 'stopped';

/** The detailed result of one run: outcome plus telemetry. */
export interface RunReport {
  outcome: Outcome;
  output: Message;
  finish: FinishReason;
  usage: Usage;
  turn: number;
  executed: string[];
  reason: string;
  audited: boolean;
  audit: AuditRecord[];
  trace_id: string;
}

/** Per-run governance. Zero values are safe defaults (deny-all gate, in-memory audit). */
export interface RunOptions {
  principal?: string;
  budget?: Budget;
  autonomy?: AutonomyLevel;
  policy?: Policy;
  approve?: ApprovalHandler | ApprovalFn;
  audit?: AuditSink;
  confirmBulk?: boolean;
  /** A data-model index reachable via search_schema (progressive disclosure). */
  schema?: SchemaIndex;
  /** Force capability disclosure on/off (default: auto, above the defer threshold). */
  defer?: boolean;
  /** A code-mode sandbox; when set, the model may write sandboxed programs (§2.11). */
  sandbox?: Sandbox;
}

/** The immutable per-run wiring the loop's helpers share. */
interface RunEnv {
  registry: CapabilityRegistry;
  policy: Policy;
  gate: ApprovalHandler;
  confirmBulk: boolean;
  audit: AuditSink;
  ctx: Context;
  canWrite: boolean;
  autonomy: AutonomyLevel;
  disclosure: Disclosure;
  sandbox?: Sandbox;
}

/** The mutable per-run state the loop accumulates. */
interface Tally {
  executed: string[];
  wrote: boolean;
  audit: AuditRecord[];
}

/**
 * Drive the loop with default governance (reads free; writes fail closed unless a
 * gate approves). A control-plane failure (the model rejecting) propagates as a
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

  const autonomy: AutonomyLevel = !canWrite ? 'read_only' : (options.autonomy ?? 'approved_writes');

  const env: RunEnv = {
    registry,
    policy: options.policy ?? new DefaultPolicy(),
    gate: options.approve ? toApprovalHandler(options.approve) : denyAll,
    confirmBulk: options.confirmBulk ?? true,
    audit: options.audit ?? new MemoryAuditSink(),
    ctx: { principal: options.principal, trace_id: newTraceId() },
    canWrite,
    autonomy,
    disclosure: new Disclosure(registry, options.schema, options.defer),
    ...(options.sandbox ? { sandbox: options.sandbox } : {}),
  };
  const tally: Tally = { executed: [], wrote: false, audit: [] };

  const messages: Message[] = [
    { role: 'system', text: SYSTEM_PROMPT },
    { role: 'user', text: goal },
  ];
  const stops: StopCondition[] = [new GoalReached(), new MaxTurns(maxTurns)];
  const params = defaultModelParams();

  let usage: Usage = { input_tokens: 0, output_tokens: 0, cost: 0 };
  let last: ModelResponse | undefined;
  let turn = 0;

  for (;;) {
    if (overTokens(budget, usage)) {
      return report(
        env,
        tally,
        'stopped',
        synthetic('Stopped: token budget exhausted.\n  → raise max_tokens or simplify the goal.'),
        'interrupted',
        usage,
        turn,
        'budget_exhausted',
      );
    }

    const tools = env.sandbox
      ? [...env.disclosure.tools(), runProgramSpec]
      : env.disclosure.tools();
    const resp = await model.complete(messages, tools, params);
    usage = addUsage(usage, resp.usage);
    messages.push(resp.message);
    last = resp;

    if (resp.finish_reason === 'tool_calls' && (resp.message.tool_calls?.length ?? 0) > 0) {
      const blocked = await act(env, tally, resp.message.tool_calls!, messages, usage, turn);
      if (blocked) {
        return blocked;
      }
    }

    turn += 1;
    const state: RunState = { messages, turn, cumulative_usage: usage, last_response: last };
    const stop = firstStop(stops, state);
    if (stop.stop) {
      if (stop.reason === 'goal_reached') {
        const outcome: Outcome = tally.wrote ? 'executed' : 'completed';
        return report(env, tally, outcome, resp.message, 'stop', usage, turn, '');
      }
      return report(
        env,
        tally,
        'stopped',
        synthetic('Stopped: turn budget exhausted.\n  → raise the budget or simplify the goal.'),
        'interrupted',
        usage,
        turn,
        'budget_exhausted',
      );
    }
  }
}

/**
 * Execute one turn's tool calls through the safety chain. Returns a terminal report
 * if the turn is refused/denied/not-executed, otherwise mutates `messages` and the
 * tally and returns undefined so the loop continues.
 */
async function act(
  env: RunEnv,
  tally: Tally,
  toolCalls: NonNullable<Message['tool_calls']>,
  messages: Message[],
  usage: Usage,
  turn: number,
): Promise<RunReport | undefined> {
  // Discovery calls (search_capabilities / search_schema) are harness-internal
  // reads: answered here, free at every autonomy level, allowed under ask() (§2.12).
  const pending: ToolCall[] = [];
  for (const call of toolCalls) {
    const answered = env.disclosure.answer(call);
    if (answered) {
      messages.push(toolMessage(call.id, answered));
      continue;
    }
    // A run_program call executes in the sandbox (code-mode, §2.11); every capability
    // call the program makes is bridged back through the full gate chain.
    if (call.name === RUN_PROGRAM && env.sandbox) {
      messages.push(toolMessage(call.id, await runProgram(env, tally, call)));
      continue;
    }
    pending.push(call);
  }
  const items = pending.map((call) => ({ call, cap: env.registry.get(call.name) }));

  if (!env.canWrite) {
    // ask(): structurally read-only — refuse the whole turn if any known call writes (§2.1).
    for (const { call, cap } of items) {
      if (cap && cap.spec.access !== 'read') {
        await auditAction(
          env,
          tally,
          { call, capability: cap.spec },
          'refused',
          'write_in_read_only',
        );
        return report(
          env,
          tally,
          'refused',
          synthetic(
            `Refused: "${call.name}" would write, but this is a read-only ask().\n  → use run(...) if writing is intended.`,
          ),
          'stop',
          usage,
          turn,
          'write_in_read_only',
        );
      }
    }
  } else {
    const proposed: ProposedAction[] = items
      .filter((it) => it.cap)
      .map((it) => ({ call: it.call, capability: it.cap!.spec }));
    if (proposed.length > 0) {
      const verdict = env.policy.checkActions(proposed, env.autonomy);
      if (verdict.decision === 'deny') {
        for (const a of proposed) {
          if (a.capability.access !== 'read') {
            await auditAction(env, tally, a, 'denied', verdict.reason);
          }
        }
        const outcome: Outcome =
          verdict.reason === 'write_in_read_only' ? 'refused' : 'not_executed';
        return report(
          env,
          tally,
          outcome,
          synthetic(
            `Not run (${verdict.reason}).\n  → adjust the goal or the agent's autonomy level.`,
          ),
          'stop',
          usage,
          turn,
          verdict.reason,
        );
      }
      const blocked = await gateTurn(
        env,
        tally,
        proposed,
        verdict.decision === 'require_approval',
        verdict.reason,
        usage,
        turn,
      );
      if (blocked) {
        return blocked;
      }
    }
  }

  for (const { call, cap } of items) {
    if (!cap) {
      messages.push(toolMessage(call.id, await env.registry.call(call.name, {}, env.ctx)));
      continue;
    }
    // A scope-refused call never reaches user code (§2.8): not audited as executed, not a write.
    const violation = scopeViolation(cap.spec, env.ctx);
    if (violation) {
      messages.push(toolMessage(call.id, resultError(violation)));
      continue;
    }
    if (cap.spec.access !== 'read') {
      const recorded = await auditAction(
        env,
        tally,
        { call, capability: cap.spec },
        'executed',
        '',
      );
      if (!recorded) {
        // no audit, no action (§2.2)
        messages.push(
          toolMessage(
            call.id,
            resultError('audit sink failed — the write was not executed\n  → fix the audit sink'),
          ),
        );
        continue;
      }
      tally.wrote = true;
    }
    const result = await env.registry.call(call.name, call.arguments ?? {}, env.ctx);
    tally.executed.push(call.name);
    messages.push(toolMessage(call.id, result));
  }
  return undefined;
}

async function gateTurn(
  env: RunEnv,
  tally: Tally,
  proposed: ProposedAction[],
  verdictRequires: boolean,
  verdictReason: string,
  usage: Usage,
  turn: number,
): Promise<RunReport | undefined> {
  const gated = proposed.filter((a) => a.capability.access !== 'read');
  const count = env.confirmBulk ? bulkCount(gated) : undefined;

  let needsApproval = verdictRequires;
  let reason = verdictReason || 'approval_required';
  if (count !== undefined && !needsApproval) {
    needsApproval = true;
    reason = 'bulk_confirm';
  }
  if (!needsApproval) {
    return undefined;
  }

  const batch = gated.length > 0 ? gated : proposed;
  const request: ApprovalRequest = { actions: batch, reason };
  if (count !== undefined) {
    request.bulkCount = count;
  }
  const approved = await env.gate.approve(request);
  const decision = approved ? 'approved' : 'approval_denied';

  let recorded = true;
  for (const a of request.actions) {
    if (!(await auditAction(env, tally, a, decision, reason))) {
      recorded = false;
    }
  }
  if (approved && recorded) {
    return undefined;
  }

  let text =
    'Not executed: the write was not approved.\n  → approve it, or pass an approve handler that returns true.';
  let reportReason = 'approval_denied';
  if (approved && !recorded) {
    text =
      'Not executed: the write was approved but could not be audited.\n  → fix the audit sink; no audit, no action.';
    reportReason = 'audit_failed';
  }
  return report(env, tally, 'not_executed', synthetic(text), 'stop', usage, turn, reportReason);
}

async function auditAction(
  env: RunEnv,
  tally: Tally,
  action: ProposedAction,
  decision: string,
  reason: string,
): Promise<boolean> {
  const record: AuditRecord = {
    timestamp: new Date().toISOString(),
    principal: env.ctx.principal ?? '',
    capability: action.capability.name,
    arguments: redactArguments(action.call.arguments ?? {}),
    access: action.capability.access,
    decision,
    reason,
    trace_id: env.ctx.trace_id ?? '',
  };
  try {
    await env.audit.record(record);
  } catch {
    return false;
  }
  tally.audit.push(record);
  return true;
}

/**
 * Execute one agent-written program in the sandbox (code-mode, §2.11). Its
 * capability calls are served by {@link bridgedCall}, so the sandbox isolates
 * computation but never authority — ask()'s read-only wall, policy, approval, bulk
 * confirm, audit, and RLS all apply to every call the program makes.
 */
async function runProgram(env: RunEnv, tally: Tally, call: ToolCall): Promise<CapabilityResult> {
  const code = call.arguments?.['code'];
  if (typeof code !== 'string' || code.trim() === '') {
    return resultError(
      "run_program: 'code' must be a non-empty string\n  → pass JavaScript source in code=",
    );
  }
  if (code.length > MAX_PROGRAM_CHARS) {
    return resultError(
      `run_program: the program is too long (${code.length} chars, max ${MAX_PROGRAM_CHARS})\n  → split the work into smaller programs`,
    );
  }
  const names = env.registry.list().map((c) => c.name);
  const outcome = await env.sandbox!.run(code, names, (name, args) =>
    bridgedCall(env, tally, name, args),
  );
  if (outcome.ok) {
    return resultOk(outcome.output || '(the program produced no output — print() what you need)');
  }
  let error = outcome.error ?? 'the program failed';
  if (outcome.output) {
    error += `\npartial output:\n${outcome.output}`;
  }
  return resultError(error);
}

/**
 * Gate and execute one capability call made from inside a program. Mirrors the
 * direct-call path exactly, per call: the ask() read-only wall (§2.1), policy by
 * autonomy (§2.2), approval + bulk confirm, audit with no-audit-no-action, then RLS
 * + validation + execution. A blocked call is an error the *program* can handle.
 */
async function bridgedCall(
  env: RunEnv,
  tally: Tally,
  name: string,
  args: Record<string, unknown>,
): Promise<CapabilityResult> {
  const cap = env.registry.get(name);
  if (!cap) {
    return env.registry.call(name, args, env.ctx); // errors-as-data, with closest-match suggestions
  }
  const action: ProposedAction = {
    call: { id: `prog_${tally.executed.length}`, name, arguments: args },
    capability: cap.spec,
  };
  if (cap.spec.access !== 'read') {
    if (!env.canWrite) {
      await auditAction(env, tally, action, 'refused', 'write_in_read_only');
      return resultError(
        `"${name}" would write, but this is a read-only ask()\n  → use run(...) if writing is intended`,
      );
    }
    const verdict = env.policy.checkActions([action], env.autonomy);
    if (verdict.decision === 'deny') {
      await auditAction(env, tally, action, 'denied', verdict.reason);
      return resultError(
        `"${name}" was not run (${verdict.reason || 'denied by policy'})\n  → adjust the goal or the agent's autonomy level`,
      );
    }
    let needsApproval = verdict.decision === 'require_approval';
    let reason = verdict.reason || 'approval_required';
    const count = env.confirmBulk ? bulkCount([action]) : undefined;
    if (count !== undefined && !needsApproval) {
      needsApproval = true;
      reason = 'bulk_confirm';
    }
    if (needsApproval) {
      const request: ApprovalRequest = { actions: [action], reason };
      if (count !== undefined) {
        request.bulkCount = count;
      }
      const approved = await env.gate.approve(request);
      const recorded = await auditAction(
        env,
        tally,
        action,
        approved ? 'approved' : 'approval_denied',
        reason,
      );
      if (!approved) {
        return resultError(
          `"${name}" was not approved\n  → the human denied it, or pass an approve handler that returns true`,
        );
      }
      if (!recorded) {
        return resultError(
          'audit sink failed — the write was not executed\n  → fix the audit sink; no audit, no action',
        );
      }
    }
  }
  const violation = scopeViolation(cap.spec, env.ctx);
  if (violation) {
    return resultError(violation);
  }
  try {
    env.registry.validate(name, args);
  } catch (e) {
    return resultError(e instanceof Error ? e.message : String(e));
  }
  if (cap.spec.access !== 'read') {
    if (!(await auditAction(env, tally, action, 'executed', ''))) {
      return resultError('audit sink failed — the write was not executed\n  → fix the audit sink');
    }
    tally.wrote = true;
  }
  const result = await env.registry.call(name, args, env.ctx);
  tally.executed.push(name);
  return result;
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
  env: RunEnv,
  tally: Tally,
  outcome: Outcome,
  output: Message,
  finish: FinishReason,
  usage: Usage,
  turn: number,
  reason: string,
): RunReport {
  return {
    outcome,
    output,
    finish,
    usage,
    turn,
    executed: [...tally.executed],
    reason,
    audited: tally.audit.length > 0,
    audit: [...tally.audit],
    trace_id: env.ctx.trace_id ?? '',
  };
}

// --- the Agent --------------------------------------------------------------------

/** Configuration for an {@link Agent}. */
export interface AgentConfig {
  capabilities?: BoundCapability[];
  budget?: Budget;
  /** Who the agent acts for — propagated into every capability call (identity → RLS). */
  principal?: string;
  /** The autonomy level for run() (default 'approved_writes'). */
  autonomy?: AutonomyLevel;
  /** The human gate for gated writes (default: fail-closed deny-all). */
  approve?: ApprovalHandler | ApprovalFn;
  /** Where write attempts are recorded (default: an in-memory sink, see {@link Agent.audit}). */
  audit?: AuditSink;
  /** Toggle the >1-row bulk-confirm default (on by default). */
  confirmBulk?: boolean;
  /** Override the policy engine (default: the autonomy ladder). */
  policy?: Policy;
  /** A data-model index reachable via search_schema (progressive disclosure). */
  schema?: SchemaIndex;
  /** Force capability disclosure on/off (default: auto, above the defer threshold). */
  defer?: boolean;
  /** A code-mode sandbox; when set, the model may write sandboxed programs (§2.11). */
  sandbox?: Sandbox;
}

/**
 * The user-facing harness: give it your capabilities, hand it a goal. `ask()` is
 * read-only and never prompts; `run()` may write, gated by the autonomy ladder. A
 * duplicate capability name is a control-plane error (thrown).
 */
export class Agent {
  readonly registry: CapabilityRegistry;
  /** The audit sink (default in-memory) — read `agent.audit` for the recorded trail. */
  readonly audit: AuditSink;
  private readonly model: Model;
  private readonly config: AgentConfig;

  constructor(model: Model, config: AgentConfig = {}) {
    this.model = model;
    this.config = config;
    this.audit = config.audit ?? new MemoryAuditSink();
    this.registry = new CapabilityRegistry();
    for (const cap of config.capabilities ?? []) {
      this.registry.register(cap);
    }
  }

  /** Accomplish a goal using only read capabilities (structurally read-only). */
  ask(goal: string): Promise<RunResult> {
    return this.drive(goal, false);
  }

  /** Accomplish a goal that may write; writes are gated by the autonomy ladder. */
  run(goal: string): Promise<RunResult> {
    return this.drive(goal, true);
  }

  private async drive(goal: string, canWrite: boolean): Promise<RunResult> {
    const options: RunOptions = { audit: this.audit };
    if (this.config.budget !== undefined) options.budget = this.config.budget;
    if (this.config.principal !== undefined) options.principal = this.config.principal;
    if (this.config.autonomy !== undefined) options.autonomy = this.config.autonomy;
    if (this.config.approve !== undefined) options.approve = this.config.approve;
    if (this.config.confirmBulk !== undefined) options.confirmBulk = this.config.confirmBulk;
    if (this.config.policy !== undefined) options.policy = this.config.policy;
    if (this.config.schema !== undefined) options.schema = this.config.schema;
    if (this.config.defer !== undefined) options.defer = this.config.defer;
    if (this.config.sandbox !== undefined) options.sandbox = this.config.sandbox;

    const rep = await runLoop(this.model, this.registry, goal, canWrite, options);
    return { output: rep.output, reason: rep.finish, usage: rep.usage, trace_id: rep.trace_id };
  }
}
