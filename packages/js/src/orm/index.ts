/**
 * `reins/orm` — auto-expose your ORM models as intent-level Reins capabilities.
 * Lives in a subpath so the core `reins` package never depends on an ORM.
 *
 * - {@link buildCapabilities} + {@link Backend} — the ORM-neutral engine (zero deps).
 * - {@link fromDrizzle} — the Drizzle adapter (requires `drizzle-orm`).
 */

export * from './build.js';
export * from './drizzle.js';
