import { describe, it, expect } from 'vitest';
import { lint, lintCapability, type Capability } from '../src/index.js';

describe('lint', () => {
  it('rejects a raw auto-generated capability with all four violations (conformance)', () => {
    // Mirrors spec/conformance/cases/linter_rejects_raw_capability.yaml.
    const report = lint('tbl_ord_upd', '', 14, true);
    expect(report.ok).toBe(false);
    expect(report.violations).toEqual([
      'missing_description',
      'opaque_name',
      'raw_response',
      'too_many_params',
    ]);
  });

  it('accepts a clean intent-level capability (conformance)', () => {
    // Mirrors spec/conformance/cases/linter_accepts_clean_capability.yaml.
    const report = lint(
      'refund_order',
      "Refund a customer's order by its public order number.",
      2,
      false,
    );
    expect(report.ok).toBe(true);
    expect(report.violations).toEqual([]);
  });

  it('flags an opaque name built only from short tokens', () => {
    expect(lint('a_b_c', 'A long enough description.', 1, false).violations).toEqual([
      'opaque_name',
    ]);
    // A standalone verb shorter than 4 chars is opaque; a real word is fine.
    expect(lint('get', 'A long enough description.', 1, false).violations).toEqual(['opaque_name']);
    expect(lint('get_user', 'Get a user by id.', 1, false).ok).toBe(true);
  });

  it('flags names that are not clean snake_case', () => {
    for (const bad of ['GetUser', 'get-user', 'get user', '1get', 'get/user']) {
      expect(lint(bad, 'A long enough description.', 1, false).violations).toContain('opaque_name');
    }
  });

  it('treats a short or blank description as missing', () => {
    expect(lint('refund_order', 'too short', 1, false).violations).toContain('missing_description');
    expect(lint('refund_order', '   ', 1, false).violations).toContain('missing_description');
  });

  it('flags more than seven parameters', () => {
    expect(lint('refund_order', 'A long enough description.', 8, false).violations).toContain(
      'too_many_params',
    );
    expect(lint('refund_order', 'A long enough description.', 7, false).ok).toBe(true);
  });
});

describe('lintCapability', () => {
  it('counts the schema properties', () => {
    const cap: Capability = {
      name: 'x',
      description: '',
      input_schema: {
        type: 'object',
        properties: Object.fromEntries(
          Array.from({ length: 9 }, (_, i) => [`p${i}`, { type: 'string' }]),
        ),
      },
      access: 'write',
      confirm: false,
      idempotent: false,
    };
    const report = lintCapability(cap, false);
    expect(report.violations).toContain('too_many_params');
    expect(report.violations).toContain('opaque_name'); // single-char name
    expect(report.violations).toContain('missing_description');
  });

  it('passes a well-formed capability', () => {
    const cap: Capability = {
      name: 'refund_order',
      description: "Refund a customer's order by its public order number.",
      input_schema: { type: 'object', properties: { order_id: { type: 'integer' } } },
      access: 'destructive',
      confirm: true,
      idempotent: false,
    };
    expect(lintCapability(cap, false).ok).toBe(true);
  });
});
