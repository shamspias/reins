import { describe, it, expect } from 'vitest';
import { classify, classifyWith, ReinsError } from '../src/index.js';

describe('classify', () => {
  it('maps leading read verbs to read', () => {
    for (const name of [
      'get_user',
      'list_orders',
      'find_products',
      'count_customers',
      'search_docs',
    ]) {
      expect(classify(name)).toBe('read');
    }
  });

  it('maps leading write verbs to write', () => {
    for (const name of ['create_order', 'update_user', 'send_email', 'assign_ticket']) {
      expect(classify(name)).toBe('write');
    }
  });

  it('maps leading destructive verbs to destructive', () => {
    for (const name of ['delete_order', 'refund_payment', 'cancel_subscription', 'purge_logs']) {
      expect(classify(name)).toBe('destructive');
    }
  });

  it('defaults ambiguous names to write (§2.4 — never read)', () => {
    for (const name of ['frobnicate_widget', 'process_order', 'handle_thing', 'sync']) {
      expect(classify(name)).toBe('write');
    }
  });

  it('reads the verb from the segment before the first underscore', () => {
    expect(classify('get_user_by_email')).toBe('read');
    expect(classify('refund_order_line')).toBe('destructive');
  });
});

describe('classifyWith overrides', () => {
  it('lets an explicit override beat the heuristic', () => {
    // A read-verb name forced to destructive, and a write-verb name forced to read.
    expect(classifyWith('get_report', false, true)).toBe('destructive');
    expect(classifyWith('export_data', true, false)).toBe('read');
  });

  it('throws when both reads and destructive are set', () => {
    expect(() => classifyWith('do_thing', true, true)).toThrow(ReinsError);
  });
});
