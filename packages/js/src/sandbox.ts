import { Worker } from 'node:worker_threads';
import type { CapabilityResult } from './types.js';

/**
 * Code-mode sandboxes (M7.5, invariant §2.11): where agent-written programs run.
 * The model writes a short program that calls capabilities as plain async functions
 * — bulk data stays in the sandbox, only the printed result returns to the model's
 * context. Every capability call the program makes is bridged back to the harness
 * and gated exactly like a direct call (§2.2): the sandbox isolates *computation*,
 * never *authority*.
 *
 * `WorkerSandbox` is the development tier: the program runs in a `worker_threads`
 * worker, inside a bare `vm` context with no `require` / `process` / `fetch` /
 * network / filesystem. **Honestly labeled (§2.9):** a same-process worker is an
 * isolation aid, not a hard security boundary — a determined program can escape a
 * `vm` context to the worker thread. Production deployments should use a stricter
 * backend (a separate process/container) behind the same {@link Sandbox} interface.
 */

/** Seconds → ms: a runaway program is a bug, not an edge case (§2.13). */
export const DEFAULT_TIMEOUT_MS = 10_000;
const MAX_OUTPUT_CHARS = 10_000; // bound what re-enters the model's context
const MAX_PROGRAM_CALLS = 256; // bound how often one program may call capabilities

/** The harness-side handler for one in-program capability call (fully gated). */
export type BridgeCall = (name: string, args: Record<string, unknown>) => Promise<CapabilityResult>;

/** What one program run produced: its printed output, or why it failed. */
export interface SandboxOutcome {
  ok: boolean;
  output: string;
  error?: string;
}

/** Runs one agent-written program with bridged, gated capability calls. */
export interface Sandbox {
  run(
    program: string,
    capabilities: string[],
    call: BridgeCall,
    options?: { timeout?: number },
  ): Promise<SandboxOutcome>;
}

// The worker runtime, run as an eval'd CommonJS string. Self-contained: it exposes
// each capability as an async proxy that RPCs to the parent, runs the program in a
// bare vm context (only ECMAScript intrinsics + print + the proxies — no require /
// process / fetch), and returns the program's print() output.
const RUNNER = `
const { parentPort } = require('node:worker_threads');
const vm = require('node:vm');
let nextId = 0;
const pending = new Map();

function callCap(name, args) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    parentPort.postMessage({ type: 'call', id: id, name: name, arguments: args || {} });
  });
}

parentPort.on('message', async (msg) => {
  if (msg.type === 'reply') {
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.ok) p.resolve(msg.value);
    else p.reject(new Error(msg.error || 'capability call failed'));
    return;
  }
  if (msg.type !== 'run') return;
  const out = [];
  const print = function () {
    out.push(Array.prototype.map.call(arguments, String).join(' '));
  };
  const ctx = { print: print, console: { log: print, error: print, warn: print, info: print } };
  for (const name of msg.capabilities) {
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
      ctx[name] = (args) => callCap(name, args);
    }
  }
  vm.createContext(ctx);
  try {
    const wrapped = '(async () => {\\n' + msg.program + '\\n})()';
    await vm.runInContext(wrapped, ctx, { timeout: msg.timeout });
    parentPort.postMessage({ type: 'done', output: out.join('\\n') });
  } catch (e) {
    parentPort.postMessage({
      type: 'error',
      error: (e && e.message) ? String(e.message) : String(e),
      output: out.join('\\n'),
    });
  }
});
`;

let warned = false;

/** The development-tier sandbox: a worker_threads worker with a bare vm context. */
export class WorkerSandbox implements Sandbox {
  constructor() {
    if (!warned) {
      warned = true;
      process.emitWarning(
        'reins WorkerSandbox is a development-tier code-mode sandbox (isolation aid, not a hard security boundary). Use a stricter backend in production.',
        'ReinsSandboxWarning',
      );
    }
  }

  run(
    program: string,
    capabilities: string[],
    call: BridgeCall,
    options: { timeout?: number } = {},
  ): Promise<SandboxOutcome> {
    const timeout = options.timeout ?? DEFAULT_TIMEOUT_MS;
    return new Promise<SandboxOutcome>((resolve) => {
      const worker = new Worker(RUNNER, { eval: true });
      let settled = false;
      let served = 0;
      // Serialize bridged calls so the shared run tally is mutated deterministically,
      // even if the program fires capability calls concurrently.
      let chain: Promise<void> = Promise.resolve();

      const settle = (outcome: SandboxOutcome): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        void worker.terminate();
        resolve(outcome);
      };

      const timer = setTimeout(() => {
        settle({
          ok: false,
          output: '',
          error: `program timed out after ${timeout}ms\n  → simplify the program, or raise the sandbox timeout`,
        });
      }, timeout);

      worker.on('message', (msg: Record<string, unknown>) => {
        if (msg['type'] === 'call') {
          chain = chain.then(async () => {
            if (settled) return;
            served += 1;
            if (served > MAX_PROGRAM_CALLS) {
              worker.postMessage({
                id: msg['id'],
                type: 'reply',
                ok: false,
                error: `call budget exhausted (${MAX_PROGRAM_CALLS} per program)\n  → batch the work, or split it across programs`,
              });
              return;
            }
            const name = msg['name'];
            const args = msg['arguments'];
            if (typeof name !== 'string' || typeof args !== 'object' || args === null) {
              worker.postMessage({
                id: msg['id'],
                type: 'reply',
                ok: false,
                error: 'malformed capability call from the program',
              });
              return;
            }
            const result = await call(name, args as Record<string, unknown>);
            worker.postMessage(
              result.ok
                ? { id: msg['id'], type: 'reply', ok: true, value: result.value }
                : {
                    id: msg['id'],
                    type: 'reply',
                    ok: false,
                    error: result.error ?? 'capability failed',
                  },
            );
          });
          return;
        }
        const output = bound(String(msg['output'] ?? ''));
        if (msg['type'] === 'done') {
          settle({ ok: true, output });
        } else if (msg['type'] === 'error') {
          settle({ ok: false, output, error: String(msg['error'] ?? 'the program failed') });
        }
      });

      worker.on('error', (err) => {
        settle({
          ok: false,
          output: '',
          error: `the sandbox crashed: ${err.message}\n  → retry; if it persists, report it`,
        });
      });
      worker.on('exit', () => {
        settle({
          ok: false,
          output: '',
          error: 'the sandbox exited unexpectedly\n  → check the program for a crash and retry',
        });
      });

      worker.postMessage({ type: 'run', program, capabilities, timeout });
    });
  }
}

function bound(output: string): string {
  if (output.length > MAX_OUTPUT_CHARS) {
    return output.slice(0, MAX_OUTPUT_CHARS) + `\n… (output trimmed at ${MAX_OUTPUT_CHARS} chars)`;
  }
  return output;
}
