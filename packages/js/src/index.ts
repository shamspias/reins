/**
 * Reins — a tiny agent harness for TypeScript. Point it at functions (or ORM
 * models) you already have and operate your app in plain language, safely and
 * cheaply. This is the public surface; M7.1 ships the core value types, the
 * {@link Model} boundary, and the deterministic {@link FakeModel}.
 */

/** The package version, kept in step with the Python and Go packages. */
export const VERSION = '0.1.0a1';

export * from './types.js';
export * from './errors.js';
export * from './model.js';
export * from './classify.js';
export * from './linter.js';
export * from './capability.js';
