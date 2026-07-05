import { describe, it, expect } from 'vitest';
import {
  CapabilityRegistry,
  FakeModel,
  capability,
  params,
  runLoop,
  callResponse,
  finalResponse,
  MemoryAuditSink,
  type AuditSink,
  type RunOptions,
} from '../src/index.js';

/** A write capability that records how many times its handler actually ran. */
function writeCap(counter: { n: number }) {
  return capability(
    'create_order',
    { description: 'Create a new order for a customer.', parameters: params({ sku: 'string' }) },
    () => {
      counter.n += 1;
      return { id: 1, sku: 'A1' };
    },
  );
}

function destructiveCap(counter: { n: number }) {
  return capability(
    'delete_order',
    { description: 'Delete an order by its id.', parameters: params({ id: 'integer' }) },
    () => {
      counter.n += 1;
      return { deleted: true };
    },
  );
}

function registryWith(...caps: ReturnType<typeof writeCap>[]): CapabilityRegistry {
  const reg = new CapabilityRegistry();
  for (const c of caps) reg.register(c);
  return reg;
}

function writeThenAnswer(name = 'create_order', args: Record<string, unknown> = { sku: 'A1' }) {
  return new FakeModel(callResponse('c1', name, args), finalResponse('Done.'));
}

describe('run() write gate — policy → approval → audit → execute', () => {
  it('executes an approved write and audits it as executed', async () => {
    const counter = { n: 0 };
    const audit = new MemoryAuditSink();
    const rep = await runLoop(
      writeThenAnswer(),
      registryWith(writeCap(counter)),
      'create an order',
      true,
      {
        autonomy: 'approved_writes',
        approve: () => true,
        audit,
      },
    );
    expect(rep.outcome).toBe('executed');
    expect(counter.n).toBe(1);
    expect(
      audit.records.some((r) => r.decision === 'executed' && r.capability === 'create_order'),
    ).toBe(true);
  });

  it('fails closed on the default deny-all gate (no approval, no write)', async () => {
    const counter = { n: 0 };
    const audit = new MemoryAuditSink();
    const rep = await runLoop(
      writeThenAnswer(),
      registryWith(writeCap(counter)),
      'create an order',
      true,
      {
        autonomy: 'approved_writes',
        audit,
      },
    );
    expect(rep.outcome).toBe('not_executed');
    expect(rep.reason).toBe('approval_denied');
    expect(counter.n).toBe(0);
    expect(audit.records.some((r) => r.decision === 'approval_denied')).toBe(true);
  });

  it('refuses a write under read_only autonomy even in run()', async () => {
    const counter = { n: 0 };
    const rep = await runLoop(
      writeThenAnswer(),
      registryWith(writeCap(counter)),
      'create an order',
      true,
      {
        autonomy: 'read_only',
        approve: () => true,
      },
    );
    expect(rep.outcome).toBe('refused');
    expect(rep.reason).toBe('write_in_read_only');
    expect(counter.n).toBe(0);
  });

  it('auto-executes a plain write when trusted, without calling the gate', async () => {
    const counter = { n: 0 };
    const rep = await runLoop(
      writeThenAnswer(),
      registryWith(writeCap(counter)),
      'create an order',
      true,
      {
        autonomy: 'trusted',
        approve: () => {
          throw new Error('the gate must not be called for a trusted plain write');
        },
      },
    );
    expect(rep.outcome).toBe('executed');
    expect(counter.n).toBe(1);
  });

  it('still gates a destructive write when trusted', async () => {
    const counter = { n: 0 };
    const rep = await runLoop(
      writeThenAnswer('delete_order', { id: 1 }),
      registryWith(destructiveCap(counter)),
      'delete order 1',
      true,
      { autonomy: 'trusted' }, // default deny-all gate
    );
    expect(rep.outcome).toBe('not_executed');
    expect(counter.n).toBe(0);
  });

  it('fails closed if an approved write cannot be audited (no audit, no action)', async () => {
    const counter = { n: 0 };
    const brokenSink: AuditSink = {
      record() {
        throw new Error('sink down');
      },
    };
    const rep = await runLoop(
      writeThenAnswer(),
      registryWith(writeCap(counter)),
      'create an order',
      true,
      {
        autonomy: 'approved_writes',
        approve: () => true,
        audit: brokenSink,
      },
    );
    expect(rep.outcome).toBe('not_executed');
    expect(rep.reason).toBe('audit_failed');
    expect(counter.n).toBe(0);
  });

  it('redacts sensitive arguments in the audit trail', async () => {
    const audit = new MemoryAuditSink();
    const cap = capability(
      'create_account',
      {
        description: 'Create an account with credentials.',
        parameters: params({ email: 'string', password: 'string' }),
      },
      () => ({ ok: true }),
    );
    await runLoop(
      new FakeModel(
        callResponse('c1', 'create_account', { email: 'a@b.com', password: 'hunter2' }),
        finalResponse('Done.'),
      ),
      registryWith(cap),
      'make an account',
      true,
      { autonomy: 'trusted', audit },
    );
    const rec = audit.records.find((r) => r.capability === 'create_account');
    expect(rec?.arguments['password']).toBe('[redacted]');
    expect(rec?.arguments['email']).toBe('a@b.com');
  });
});

describe('bulk confirmation', () => {
  function bulkModel() {
    return new FakeModel(
      callResponse('c1', 'update_orders', { ids: [1, 2, 3] }),
      finalResponse('Done.'),
    );
  }
  function bulkCap(counter: { n: number }) {
    return capability(
      'update_orders',
      { description: 'Update several orders at once.', parameters: params({ ids: 'integer[]' }) },
      () => {
        counter.n += 1;
        return { updated: 3 };
      },
    );
  }

  it('gates a trusted plain write when it touches multiple rows', async () => {
    const counter = { n: 0 };
    const rep = await runLoop(bulkModel(), registryWith(bulkCap(counter)), 'update orders', true, {
      autonomy: 'trusted', // policy would allow, but bulk forces a confirm
      // default deny-all gate
    });
    expect(rep.outcome).toBe('not_executed');
    expect(counter.n).toBe(0);
  });

  it('executes the bulk write once confirmed', async () => {
    const counter = { n: 0 };
    const rep = await runLoop(bulkModel(), registryWith(bulkCap(counter)), 'update orders', true, {
      autonomy: 'trusted',
      approve: (req) => req.bulkCount === 3,
    });
    expect(rep.outcome).toBe('executed');
    expect(counter.n).toBe(1);
  });
});

describe('ask() still refuses writes and audits the refusal', () => {
  it('refuses and records a refused audit entry', async () => {
    const counter = { n: 0 };
    const audit = new MemoryAuditSink();
    const rep = await runLoop(
      writeThenAnswer('delete_order', { id: 1 }),
      registryWith(destructiveCap(counter)),
      'delete order 1',
      false,
      { audit },
    );
    expect(rep.outcome).toBe('refused');
    expect(counter.n).toBe(0);
    expect(audit.records.some((r) => r.decision === 'refused')).toBe(true);
  });
});

describe('row-level security', () => {
  function scopedReadCap(counter: { n: number }) {
    return capability(
      'get_my_orders',
      { description: 'List orders belonging to the caller.', scope: 'user' },
      () => {
        counter.n += 1;
        return [{ id: 1 }];
      },
    );
  }

  const model = () =>
    new FakeModel(callResponse('c1', 'get_my_orders', {}), finalResponse('Here they are.'));

  it('refuses a scoped capability when the run has no principal (fail closed, §2.8)', async () => {
    const counter = { n: 0 };
    const rep = await runLoop(model(), registryWith(scopedReadCap(counter)), 'my orders', false);
    expect(rep.outcome).toBe('completed'); // errors-as-data, the loop continues
    expect(rep.executed).toEqual([]); // the scoped call never reached user code
    expect(counter.n).toBe(0);
  });

  it('runs a scoped capability once a principal is present', async () => {
    const counter = { n: 0 };
    const opts: RunOptions = { principal: 'user-42' };
    const rep = await runLoop(
      model(),
      registryWith(scopedReadCap(counter)),
      'my orders',
      false,
      opts,
    );
    expect(rep.outcome).toBe('completed');
    expect(rep.executed).toEqual(['get_my_orders']);
    expect(counter.n).toBe(1);
  });
});
