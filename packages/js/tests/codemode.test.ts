import { describe, it, expect } from 'vitest';
import {
  runLoop,
  CapabilityRegistry,
  FakeModel,
  capability,
  params,
  programResponse,
  finalResponse,
  WorkerSandbox,
  MemoryAuditSink,
  resultOk,
  type BoundCapability,
} from '../src/index.js';

function regWith(...caps: BoundCapability[]): CapabilityRegistry {
  const reg = new CapabilityRegistry();
  for (const c of caps) reg.register(c);
  return reg;
}

function findOrders(onCall?: () => void) {
  return capability(
    'find_orders',
    { description: 'Find orders matching a status.', parameters: params({ status: 'string?' }) },
    () => {
      onCall?.();
      return [];
    },
  );
}

function createOrder(counter: { n: number }) {
  return capability(
    'create_order',
    { description: 'Create a new order.', parameters: params({ sku: 'string' }) },
    () => {
      counter.n += 1;
      return { id: 1 };
    },
  );
}

function toolText(model: FakeModel, turn: number): string {
  return model.calls[turn]?.messages.find((m) => m.role === 'tool')?.text ?? '';
}

describe('WorkerSandbox (dev tier)', () => {
  it('runs a program with a bridged capability call and returns its printed output', async () => {
    const sb = new WorkerSandbox();
    const outcome = await sb.run(
      "const n = await total();\nprint('n=' + n);",
      ['total'],
      async () => resultOk(42),
    );
    expect(outcome.ok).toBe(true);
    expect(outcome.output).toContain('n=42');
  });

  it('gives the program no ambient require / process / fetch', async () => {
    const sb = new WorkerSandbox();
    const outcome = await sb.run(
      'print(typeof require, typeof process, typeof fetch);',
      [],
      async () => resultOk(null),
    );
    expect(outcome.output).toBe('undefined undefined undefined');
  });

  it('times out a runaway program', async () => {
    const sb = new WorkerSandbox();
    const outcome = await sb.run('while (true) {}', [], async () => resultOk(null), {
      timeout: 300,
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.error).toContain('timed out');
  });

  it('enforces the per-program call budget', async () => {
    const sb = new WorkerSandbox();
    const outcome = await sb.run(
      'for (let i = 0; i < 300; i++) { await ping(); }\nprint("done");',
      ['ping'],
      async () => resultOk(1),
    );
    expect(outcome.ok).toBe(false);
    expect(outcome.error).toContain('call budget exhausted');
  });
});

describe('code-mode through the loop (§2.11)', () => {
  it('runs a read program and lists its bridged call in executed', async () => {
    const model = new FakeModel(
      programResponse(
        'p1',
        "const rows = await find_orders({ status: 'open' });\nprint('open=' + rows.length);",
      ),
      finalResponse('There are 0 open orders.'),
    );
    const rep = await runLoop(model, regWith(findOrders()), 'how many open orders?', false, {
      sandbox: new WorkerSandbox(),
    });
    expect(rep.outcome).toBe('completed');
    expect(rep.executed).toEqual(['find_orders']);
    expect(toolText(model, 1)).toContain('open=0'); // the printed result reached the model
  });

  it('executes an approved write made from inside a program and audits it', async () => {
    const counter = { n: 0 };
    const audit = new MemoryAuditSink();
    const model = new FakeModel(
      programResponse(
        'p1',
        "const r = await create_order({ sku: 'A1' });\nprint('created ' + r.id);",
      ),
      finalResponse('Created.'),
    );
    const rep = await runLoop(model, regWith(createOrder(counter)), 'create an order', true, {
      sandbox: new WorkerSandbox(),
      autonomy: 'approved_writes',
      approve: () => true,
      audit,
    });
    expect(rep.outcome).toBe('executed');
    expect(rep.executed).toEqual(['create_order']);
    expect(counter.n).toBe(1);
    expect(
      audit.records.some((r) => r.decision === 'executed' && r.capability === 'create_order'),
    ).toBe(true);
  });

  it('gates a write inside a program: the default deny-all blocks it (errors-as-data)', async () => {
    const counter = { n: 0 };
    const model = new FakeModel(
      programResponse(
        'p1',
        "try { await create_order({ sku: 'A1' }); print('ran'); } catch (e) { print('blocked: ' + e.message); }",
      ),
      finalResponse('Could not create it.'),
    );
    const rep = await runLoop(model, regWith(createOrder(counter)), 'create an order', true, {
      sandbox: new WorkerSandbox(), // default deny-all gate
    });
    expect(rep.executed).toEqual([]);
    expect(counter.n).toBe(0);
    expect(toolText(model, 1)).toContain('blocked');
  });

  it('refuses a write inside a program under ask() (§2.1)', async () => {
    const counter = { n: 0 };
    const model = new FakeModel(
      programResponse(
        'p1',
        "try { await create_order({ sku: 'A1' }); print('ran'); } catch (e) { print('refused: ' + e.message); }",
      ),
      finalResponse('done'),
    );
    const rep = await runLoop(model, regWith(createOrder(counter)), 'try to create', false, {
      sandbox: new WorkerSandbox(),
    });
    expect(counter.n).toBe(0);
    expect(rep.executed).toEqual([]);
    expect(toolText(model, 1)).toContain('read-only');
  });

  it('surfaces a call to an unexposed function as errors-as-data', async () => {
    // Only registered capabilities are exposed as functions, so calling something
    // else is a ReferenceError the program can catch — the run continues.
    const model = new FakeModel(
      programResponse('p1', 'try { await nope(); } catch (e) { print(e.message); }'),
      finalResponse('done'),
    );
    const rep = await runLoop(model, regWith(findOrders()), 'call something missing', false, {
      sandbox: new WorkerSandbox(),
    });
    expect(rep.outcome).toBe('completed');
    expect(toolText(model, 1)).toContain('is not defined');
  });
});
