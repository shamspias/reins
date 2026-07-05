import { describe, it, expect } from 'vitest';
import {
  redactArguments,
  isSensitive,
  MemoryAuditSink,
  REDACTED,
  type AuditRecord,
} from '../src/index.js';

describe('redactArguments', () => {
  it('masks sensitive top-level keys and leaves the rest', () => {
    expect(redactArguments({ password: 'hunter2', name: 'Ada' })).toEqual({
      password: REDACTED,
      name: 'Ada',
    });
  });

  it('redacts nested objects and arrays', () => {
    const out = redactArguments({
      user: { api_key: 'sk-123', id: 7 },
      cards: [{ card_number: '4111' }],
    });
    expect(out).toEqual({
      user: { api_key: REDACTED, id: 7 },
      cards: [{ card_number: REDACTED }],
    });
  });

  it('does not mutate the original arguments', () => {
    const original = { token: 'secret-abc', keep: 1 };
    redactArguments(original);
    expect(original.token).toBe('secret-abc');
  });
});

describe('isSensitive', () => {
  it('matches sensitive substrings case-insensitively', () => {
    for (const key of ['password', 'apiKey', 'access_token', 'Authorization', 'card_number']) {
      expect(isSensitive(key)).toBe(true);
    }
  });

  it('leaves ordinary keys alone', () => {
    for (const key of ['name', 'email', 'order_id', 'quantity']) {
      expect(isSensitive(key)).toBe(false);
    }
  });
});

describe('MemoryAuditSink', () => {
  it('records in order', () => {
    const sink = new MemoryAuditSink();
    const rec: AuditRecord = {
      timestamp: '2026-01-01T00:00:00.000Z',
      principal: 'u1',
      capability: 'create_order',
      arguments: {},
      access: 'write',
      decision: 'executed',
      reason: '',
      trace_id: 't',
    };
    sink.record(rec);
    expect(sink.records).toHaveLength(1);
    expect(sink.records[0]?.capability).toBe('create_order');
  });
});
