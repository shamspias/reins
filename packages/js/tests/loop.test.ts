import { describe, it, expect } from 'vitest';
import {
  Agent,
  CapabilityRegistry,
  FakeModel,
  capability,
  params,
  runLoop,
  callResponse,
  finalResponse,
  type Context,
} from '../src/index.js';

function readCap(onCall?: (ctx: Context) => void) {
  return capability(
    'get_order',
    { description: 'Get an order by its id.', parameters: params({ id: 'integer' }) },
    (ctx) => {
      onCall?.(ctx);
      return { id: 1, total: 42 };
    },
  );
}

let deleteCalls = 0;
function deleteCap() {
  return capability(
    'delete_order',
    { description: 'Delete an order by its id.', parameters: params({ id: 'integer' }) },
    () => {
      deleteCalls += 1;
      return { deleted: true };
    },
  );
}

describe('runLoop — reads', () => {
  it('completes a read goal, calling the capability then answering', async () => {
    const model = new FakeModel(
      callResponse('c1', 'get_order', { id: 1 }),
      finalResponse('The order total is 42.'),
    );
    const reg = new CapabilityRegistry();
    reg.register(readCap());

    const rep = await runLoop(model, reg, 'what is order 1 total?', false);
    expect(rep.outcome).toBe('completed');
    expect(rep.executed).toEqual(['get_order']);
    expect(rep.output.text).toBe('The order total is 42.');
    expect(rep.finish).toBe('stop');
  });

  it('feeds an unknown capability back to the model as errors-as-data', async () => {
    const model = new FakeModel(
      callResponse('c1', 'get_ordr', {}),
      finalResponse('Sorry, retried.'),
    );
    const reg = new CapabilityRegistry();
    reg.register(readCap());

    const rep = await runLoop(model, reg, 'try a typo', false);
    expect(rep.outcome).toBe('completed');
    expect(rep.executed).toEqual([]); // the unknown call did not execute
    // The tool result the model saw on its second turn carried the error + suggestion.
    const secondTurn = model.calls[1]!.messages;
    const toolMsg = secondTurn.find((m) => m.role === 'tool');
    expect(toolMsg?.text).toContain('no capability named "get_ordr"');
    expect(toolMsg?.text).toContain('get_order');
  });
});

describe('runLoop — the ask/run write boundary', () => {
  it('ask() structurally refuses a write before it executes (§2.1)', async () => {
    deleteCalls = 0;
    const model = new FakeModel(
      callResponse('c1', 'delete_order', { id: 1 }),
      finalResponse('should never reach here'),
    );
    const reg = new CapabilityRegistry();
    reg.register(deleteCap());

    const rep = await runLoop(model, reg, 'delete order 1', false);
    expect(rep.outcome).toBe('refused');
    expect(rep.reason).toBe('write_in_read_only');
    expect(rep.executed).toEqual([]);
    expect(deleteCalls).toBe(0); // the handler was never invoked
  });

  it('run() fails closed on a write with the default deny-all gate', async () => {
    deleteCalls = 0;
    const model = new FakeModel(
      callResponse('c1', 'delete_order', { id: 1 }),
      finalResponse('should never reach here'),
    );
    const reg = new CapabilityRegistry();
    reg.register(deleteCap());

    // No approve handler → the fail-closed deny-all gate denies the destructive write.
    const rep = await runLoop(model, reg, 'delete order 1', true);
    expect(rep.outcome).toBe('not_executed');
    expect(rep.reason).toBe('approval_denied');
    expect(deleteCalls).toBe(0);
  });
});

describe('runLoop — budgets', () => {
  it('stops a runaway loop at the turn cap', async () => {
    // The model never answers — it keeps calling a read capability.
    const model = new FakeModel(
      ...Array.from({ length: 6 }, (_, i) => callResponse(`c${i}`, 'get_order', { id: 1 })),
    );
    const reg = new CapabilityRegistry();
    reg.register(readCap());

    const rep = await runLoop(model, reg, 'loop forever', false, { budget: { max_turns: 2 } });
    expect(rep.outcome).toBe('stopped');
    expect(rep.reason).toBe('budget_exhausted');
    expect(rep.turn).toBe(2);
  });
});

describe('Agent', () => {
  it('ask() returns a RunResult with a trace id and answer', async () => {
    const model = new FakeModel(finalResponse('Hello.'));
    const agent = new Agent(model, { capabilities: [readCap()] });

    const res = await agent.ask('say hi');
    expect(res.output.text).toBe('Hello.');
    expect(res.reason).toBe('stop');
    expect(res.trace_id).toMatch(/^[0-9a-f]{32}$/);
  });

  it('propagates the principal into capability calls (identity → RLS, §2.7)', async () => {
    let seen: string | undefined;
    const model = new FakeModel(callResponse('c1', 'get_order', { id: 1 }), finalResponse('done'));
    const agent = new Agent(model, {
      capabilities: [readCap((ctx) => (seen = ctx.principal))],
      principal: 'user-42',
    });

    await agent.ask('who am i');
    expect(seen).toBe('user-42');
  });

  it('throws on a duplicate capability name (control-plane)', () => {
    const model = new FakeModel();
    expect(() => new Agent(model, { capabilities: [readCap(), readCap()] })).toThrow();
  });
});
