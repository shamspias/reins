import type { Access } from './types.js';

/**
 * The audit log (M7.4): a structured record of every write attempt. Whenever the
 * harness blocks, gates, or executes a write it emits an {@link AuditRecord} —
 * principal, capability, PII-redacted arguments, decision, timestamp, trace id — to
 * the sink. Auditing is part of the gate chain (§2.2): a write that cannot be
 * recorded does not run (no audit, no action). Reads are not audited.
 */

/** Replaces a sensitive argument value in the audit copy. */
export const REDACTED = '[redacted]';

const sensitiveKeyParts = [
  'password',
  'passwd',
  'secret',
  'token',
  'api_key',
  'apikey',
  'authorization',
  'credential',
  'private_key',
  'ssn',
  'card_number',
  'cvv',
];

/**
 * One write attempt: who, what, with which (redacted) args, and the decision
 * (refused / denied / approved / approval_denied / executed).
 */
export interface AuditRecord {
  timestamp: string;
  principal: string;
  capability: string;
  arguments: Record<string, unknown>;
  access: Access;
  decision: string;
  reason: string;
  trace_id: string;
}

/**
 * Where records go. `record` persists (or forwards) one record; throwing (or a
 * rejected promise) means the write it logs must not run.
 */
export interface AuditSink {
  record(record: AuditRecord): void | Promise<void>;
}

/** The default sink: an in-process, in-order list. */
export class MemoryAuditSink implements AuditSink {
  readonly records: AuditRecord[] = [];

  record(record: AuditRecord): void {
    this.records.push(record);
  }
}

/**
 * A deep copy of args with sensitive-looking keys masked; the original is never
 * mutated (the capability still receives the real values).
 */
export function redactArguments(args: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(args)) {
    out[k] = redactValue(k, v);
  }
  return out;
}

function redactValue(key: string, value: unknown): unknown {
  if (isSensitive(key)) {
    return REDACTED;
  }
  if (Array.isArray(value)) {
    return value.map((inner) => redactValue(key, inner));
  }
  if (typeof value === 'object' && value !== null) {
    const out: Record<string, unknown> = {};
    for (const [k, inner] of Object.entries(value)) {
      out[k] = redactValue(k, inner);
    }
    return out;
  }
  return value;
}

/** Whether a key name looks sensitive (substring match, case-insensitive). */
export function isSensitive(key: string): boolean {
  const lowered = key.toLowerCase();
  return sensitiveKeyParts.some((part) => lowered.includes(part));
}
