import { describe, it, expect } from 'vitest';
import { Readable } from 'node:stream';
import {
  denyAll,
  toApprovalHandler,
  previewApproval,
  bulkCount,
  TerminalApprovalHandler,
  type Access,
  type ProposedAction,
  type ApprovalRequest,
} from '../src/index.js';

function action(name: string, access: Access, args: Record<string, unknown> = {}): ProposedAction {
  return {
    call: { id: 'c', name, arguments: args },
    capability: { name, description: 'x', access, confirm: false, idempotent: false },
  };
}

function fakeOut() {
  const chunks: string[] = [];
  return {
    write: (s: string) => {
      chunks.push(s);
      return true;
    },
    text: () => chunks.join(''),
  };
}

describe('gate defaults', () => {
  it('denyAll fails closed', async () => {
    expect(await denyAll.approve({ actions: [], reason: 'x' })).toBe(false);
  });

  it('toApprovalHandler wraps a plain function and passes through a handler', async () => {
    const fromFn = toApprovalHandler(() => true);
    expect(await fromFn.approve({ actions: [], reason: 'x' })).toBe(true);
    expect(toApprovalHandler(denyAll)).toBe(denyAll);
  });
});

describe('previewApproval', () => {
  it('renders a single gated call with its bounded arguments', () => {
    const preview = previewApproval({
      actions: [action('create_order', 'write', { id: 1 })],
      reason: 'x',
    });
    expect(preview).toContain('⚠');
    expect(preview).toContain('WRITE');
    expect(preview).toContain('create_order(id=1)');
  });

  it('renders a batch and the bulk count', () => {
    const req: ApprovalRequest = {
      actions: [action('update_a', 'write'), action('delete_b', 'destructive')],
      reason: 'x',
      bulkCount: 42,
    };
    const preview = previewApproval(req);
    expect(preview).toContain('2 gated operations');
    expect(preview).toContain('up to 42 items');
  });
});

describe('bulkCount', () => {
  it('reports the largest >1 list argument on a write', () => {
    expect(bulkCount([action('update_orders', 'write', { ids: [1, 2, 3] })])).toBe(3);
  });

  it('ignores reads and single/absent lists', () => {
    expect(bulkCount([action('find_orders', 'read', { ids: [1, 2, 3] })])).toBeUndefined();
    expect(bulkCount([action('update_order', 'write', { ids: [1] })])).toBeUndefined();
    expect(bulkCount([action('update_order', 'write', { id: 1 })])).toBeUndefined();
  });
});

describe('TerminalApprovalHandler (fails closed)', () => {
  const req: ApprovalRequest = {
    actions: [action('create_order', 'write', { id: 1 })],
    reason: 'x',
  };

  it('approves on "y"', async () => {
    const h = new TerminalApprovalHandler(Readable.from(['y\n']), fakeOut());
    expect(await h.approve(req)).toBe(true);
  });

  it('denies on "n"', async () => {
    const h = new TerminalApprovalHandler(Readable.from(['n\n']), fakeOut());
    expect(await h.approve(req)).toBe(false);
  });

  it('denies on EOF (no interactive input)', async () => {
    const out = fakeOut();
    const h = new TerminalApprovalHandler(Readable.from([]), out);
    expect(await h.approve(req)).toBe(false);
    expect(out.text()).toContain('No interactive terminal');
  });

  it('shows detail then honors the next answer', async () => {
    const out = fakeOut();
    const h = new TerminalApprovalHandler(Readable.from(['s\n', 'y\n']), out);
    expect(await h.approve(req)).toBe(true);
    expect(out.text()).toContain('create_order');
  });
});
