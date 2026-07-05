/**
 * ReinsError is a control-plane failure carrying a human `hint` — the "→ next step"
 * that invariant §2.19 requires. Capability-level failures are data
 * ({@link CapabilityResult}); only control-plane failures throw. Mirrors Go's
 * `*reins.Error` and Python's `ReinsError`.
 */
export class ReinsError extends Error {
  /** The "→ next step" shown to the user. */
  readonly hint: string;
  /** The plain message without the hint (Go's `Error.Message`). */
  readonly summary: string;

  constructor(message: string, hint: string) {
    super(`${message}\n  → ${hint}`);
    this.name = 'ReinsError';
    this.summary = message;
    this.hint = hint;
    // Keep `instanceof` working regardless of the compile target.
    Object.setPrototypeOf(this, ReinsError.prototype);
  }
}

/** Build a hinted control-plane error (parity with Go's `NewError`). */
export function newError(message: string, hint: string): ReinsError {
  return new ReinsError(message, hint);
}
