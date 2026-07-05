import * as readline from 'node:readline';
import type { ProposedAction } from './policy.js';

/**
 * The human gate (M7.4): pause a gated write for approval before it executes. The
 * policy decides WHETHER approval is needed; the handler decides HOW the human is
 * asked. Everything fails closed (invariant §2.2): the default handler denies, and
 * TerminalApprovalHandler denies on EOF / no interactive input.
 */

const VALUE_WIDTH = 48; // bound each rendered argument value in the preview
const MAX_PROMPTS = 5; // bound the terminal re-prompt loop (§2.13)
const PREVIEW_CAPS = 8; // bound the listed actions

/** One gated batch: the exact calls about to run, and why. */
export interface ApprovalRequest {
  actions: ProposedAction[];
  reason: string;
  bulkCount?: number;
}

/** The gate: return true to execute the gated actions, false to deny. May be async. */
export interface ApprovalHandler {
  approve(request: ApprovalRequest): boolean | Promise<boolean>;
}

/** A plain function form of {@link ApprovalHandler}. */
export type ApprovalFn = (request: ApprovalRequest) => boolean | Promise<boolean>;

/** Normalize a handler or a plain function into an {@link ApprovalHandler}. */
export function toApprovalHandler(a: ApprovalHandler | ApprovalFn): ApprovalHandler {
  return typeof a === 'function' ? { approve: a } : a;
}

/** The fail-closed default gate — no approval, no write. */
export const denyAll: ApprovalHandler = { approve: () => false };

/** The human-facing rendering: exact calls, values bounded. */
export function previewApproval(request: ApprovalRequest): string {
  const lines: string[] = [];
  if (request.actions.length === 1) {
    const a = request.actions[0]!;
    lines.push(`⚠ Reins wants to run a ${accessWord(a)}: ${renderCall(a)}`);
  } else {
    lines.push(`⚠ Reins wants to run ${request.actions.length} gated operations:`);
    request.actions.forEach((a, i) => {
      if (i >= PREVIEW_CAPS) {
        return;
      }
      lines.push(`    ${renderCall(a)}  [${accessWord(a).toLowerCase()}]`);
    });
    if (request.actions.length > PREVIEW_CAPS) {
      lines.push(`    … and ${request.actions.length - PREVIEW_CAPS} more`);
    }
  }
  if (request.bulkCount !== undefined) {
    lines.push(`  This will touch up to ${request.bulkCount} items.`);
  }
  return lines.join('\n');
}

/**
 * The largest count of a >1-item list argument on a write, or undefined. The
 * pre-ORM row-limit default: such a call is treated as touching that many rows.
 */
export function bulkCount(actions: ProposedAction[]): number | undefined {
  let best = 0;
  for (const a of actions) {
    if (a.capability.access === 'read') {
      continue;
    }
    for (const v of Object.values(a.call.arguments ?? {})) {
      if (Array.isArray(v) && v.length > 1 && v.length > best) {
        best = v.length;
      }
    }
  }
  return best === 0 ? undefined : best;
}

type Writer = { write(chunk: string): unknown };

/**
 * TerminalApprovalHandler prints the preview and asks [y]es / [n]o / [s]how on a
 * terminal, failing closed on EOF or an exhausted re-prompt bound. `input` and
 * `output` are injectable for tests; defaults are stdin/stdout.
 */
export class TerminalApprovalHandler implements ApprovalHandler {
  constructor(
    private readonly input: NodeJS.ReadableStream = process.stdin,
    private readonly output: Writer = process.stdout,
  ) {}

  async approve(request: ApprovalRequest): Promise<boolean> {
    const out = (s: string): void => {
      this.output.write(s + '\n');
    };
    out(previewApproval(request));

    const rl = readline.createInterface({ input: this.input });
    const lines = rl[Symbol.asyncIterator]();
    try {
      for (let i = 0; i < MAX_PROMPTS; i++) {
        this.output.write('Approve? [y]es / [n]o / [s]how: ');
        const next = await lines.next();
        if (next.done) {
          out(
            'No interactive terminal — denying.\n  → run interactively, or pass an approve handler.',
          );
          return false; // EOF / closed stdin ⇒ deny (fail closed)
        }
        const answer = String(next.value).trim().toLowerCase();
        if (answer === 'y' || answer === 'yes') {
          return true;
        }
        if (answer === 'n' || answer === 'no') {
          return false;
        }
        if (answer === 's' || answer === 'show') {
          out(detail(request));
        } else {
          out('Unrecognized — answer y, n, or s.');
        }
      }
      return false; // re-prompt bound exhausted ⇒ deny
    } finally {
      rl.close();
    }
  }
}

function accessWord(a: ProposedAction): string {
  if (a.capability.access === 'destructive') {
    return 'DESTRUCTIVE WRITE';
  }
  if (a.capability.access === 'read') {
    return 'READ';
  }
  return 'WRITE';
}

function renderCall(a: ProposedAction): string {
  const args = a.call.arguments ?? {};
  const parts = Object.keys(args)
    .sort()
    .map((k) => `${k}=${bounded(args[k])}`);
  return `${a.call.name}(${parts.join(', ')})`;
}

function detail(request: ApprovalRequest): string {
  return request.actions
    .map((a) => `  ${a.call.name} ${JSON.stringify(a.call.arguments ?? {})}`)
    .join('\n');
}

function bounded(v: unknown): string {
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return s.length > VALUE_WIDTH ? s.slice(0, VALUE_WIDTH) + '…' : s;
}
