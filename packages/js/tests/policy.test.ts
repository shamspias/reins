import { describe, it, expect } from 'vitest';
import {
  DefaultPolicy,
  type Access,
  type AutonomyLevel,
  type ProposedAction,
} from '../src/index.js';

function action(name: string, access: Access, confirm = false): ProposedAction {
  return {
    call: { id: 'c', name, arguments: {} },
    capability: { name, description: 'x', access, confirm, idempotent: false },
  };
}

const policy = new DefaultPolicy();
const read = action('get_x', 'read');
const write = action('create_x', 'write');
const destructive = action('delete_x', 'destructive');
const confirmWrite = action('send_x', 'write', true);

const levels: AutonomyLevel[] = ['read_only', 'draft_writes', 'approved_writes', 'trusted'];

describe('DefaultPolicy — the autonomy ladder', () => {
  it('allows reads at every autonomy level (§2.2)', () => {
    for (const level of levels) {
      expect(policy.checkActions([read], level)).toEqual({ decision: 'allow', reason: '' });
    }
  });

  it('gates a plain write by autonomy level', () => {
    expect(policy.checkActions([write], 'read_only')).toEqual({
      decision: 'deny',
      reason: 'write_in_read_only',
    });
    expect(policy.checkActions([write], 'draft_writes')).toEqual({
      decision: 'deny',
      reason: 'draft_only',
    });
    expect(policy.checkActions([write], 'approved_writes')).toEqual({
      decision: 'require_approval',
      reason: 'approval_required',
    });
    expect(policy.checkActions([write], 'trusted')).toEqual({ decision: 'allow', reason: '' });
  });

  it('never auto-executes a destructive or confirm-marked write, even when trusted', () => {
    expect(policy.checkActions([destructive], 'trusted').decision).toBe('require_approval');
    expect(policy.checkActions([confirmWrite], 'trusted').decision).toBe('require_approval');
  });

  it('takes the most restrictive decision across a batch', () => {
    expect(policy.checkActions([read, write], 'approved_writes').decision).toBe('require_approval');
    expect(policy.checkActions([read, write], 'read_only').decision).toBe('deny');
    expect(policy.checkActions([write, destructive], 'trusted').decision).toBe('require_approval');
  });

  it('allows output by default (checkOutput)', () => {
    expect(policy.checkOutput({ role: 'assistant', text: 'hi' }).decision).toBe('allow');
  });
});
