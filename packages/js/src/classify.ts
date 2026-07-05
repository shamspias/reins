import type { Access } from './types.js';
import { ReinsError } from './errors.js';

/**
 * Read/write classification by the name's leading verb (the easy-by-design
 * heuristic). Ambiguous ⇒ write (invariant §2.4): a wrong guess only ever adds a
 * needless approval; it must never let a write run as a read. The verb sets are
 * kept identical to the Python and Go ports so classification is language-neutral.
 */

const readVerbs = new Set([
  'get',
  'list',
  'find',
  'search',
  'fetch',
  'count',
  'show',
  'read',
  'view',
  'lookup',
  'describe',
]);

const writeVerbs = new Set([
  'create',
  'add',
  'update',
  'set',
  'save',
  'send',
  'apply',
  'edit',
  'modify',
  'insert',
  'put',
  'post',
  'make',
  'assign',
  'schedule',
  'approve',
  'register',
]);

const destructiveVerbs = new Set([
  'delete',
  'remove',
  'cancel',
  'refund',
  'charge',
  'drop',
  'deactivate',
  'purge',
  'archive',
  'revoke',
  'reset',
  'wipe',
]);

/** Classify a capability's access from its name's leading verb (ambiguous ⇒ write, §2.4). */
export function classify(name: string): Access {
  return classifyWith(name, false, false);
}

/**
 * Apply explicit overrides (`reads` / `destructive`) over the heuristic. Setting
 * both is a developer error and throws — as the Python decorator raises and the Go
 * port panics.
 */
export function classifyWith(name: string, reads: boolean, destructive: boolean): Access {
  if (reads && destructive) {
    throw new ReinsError(
      `capability ${name} marked both reads and destructive`,
      'pick one — a capability is either a read or a write/destructive',
    );
  }
  if (destructive) {
    return 'destructive';
  }
  if (reads) {
    return 'read';
  }
  const underscore = name.indexOf('_');
  const verb = (underscore >= 0 ? name.slice(0, underscore) : name).toLowerCase();
  if (destructiveVerbs.has(verb)) {
    return 'destructive';
  }
  if (readVerbs.has(verb)) {
    return 'read';
  }
  if (writeVerbs.has(verb)) {
    return 'write';
  }
  return 'write'; // ambiguous ⇒ write (invariant §2.4)
}
