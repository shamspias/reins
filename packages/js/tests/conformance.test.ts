import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as yaml from 'js-yaml';
import { describe, it, expect } from 'vitest';
import {
  runLoop,
  CapabilityRegistry,
  FakeModel,
  finalResponse,
  callResponse,
  classify,
  lint,
  type AutonomyLevel,
  type BoundCapability,
  type ModelResponse,
  type RunOptions,
} from '../src/index.js';

/**
 * The TypeScript adapter for the cross-language conformance suite (M7.5): it loads
 * the same language-neutral cases in spec/conformance/cases/*.yaml and drives the TS
 * harness with a scripted FakeModel, asserting each case's `expect` block. This is
 * how parity (§2.21) and the safety invariants are locked across Python, Go, and TS.
 *
 * Code-mode programs are a language-specific dialect (a {program} step carries
 * Python source), so those golden cases are skipped here with a recorded reason —
 * TS code-mode is validated by native tests instead. Every other case is neutral.
 */

const casesDir = fileURLToPath(new URL('../../../spec/conformance/cases', import.meta.url));

interface ConformanceCase {
  name: string;
  verb?: string;
  goal?: string;
  capabilities?: string[];
  model_script?: Array<Record<string, unknown>>;
  autonomy?: string;
  approvals?: string;
  budget?: { max_turns?: number };
  capability?: Record<string, unknown>;
  expect: Record<string, unknown>;
}

function loadCases(): ConformanceCase[] {
  return readdirSync(casesDir)
    .filter((f) => f.endsWith('.yaml'))
    .sort()
    .map((f) => yaml.load(readFileSync(join(casesDir, f), 'utf8')) as ConformanceCase);
}

/** A permissive stub: additionalProperties, access classified from the name, returns []. */
function stub(name: string): BoundCapability {
  return {
    spec: {
      name,
      description: `Stub capability ${name}.`,
      input_schema: { type: 'object', additionalProperties: true },
      access: classify(name),
      confirm: false,
      idempotent: false,
    },
    handler: () => [],
    returnsRaw: false,
  };
}

function scriptModel(script: Array<Record<string, unknown>>): FakeModel {
  const responses: ModelResponse[] = [];
  script.forEach((step, i) => {
    if ('final' in step) {
      responses.push(finalResponse(String(step['final'])));
    } else if ('call' in step) {
      responses.push(
        callResponse(
          `call_${i}`,
          String(step['call']),
          (step['args'] as Record<string, unknown>) ?? {},
        ),
      );
    }
  });
  return new FakeModel(...responses);
}

async function driveGolden(c: ConformanceCase): Promise<void> {
  const reg = new CapabilityRegistry();
  for (const name of c.capabilities ?? []) {
    reg.register(stub(name));
  }
  const options: RunOptions = { approve: () => c.approvals === 'allow' };
  if (c.autonomy) {
    options.autonomy = c.autonomy as AutonomyLevel;
  }
  if (c.budget?.max_turns) {
    options.budget = { max_turns: c.budget.max_turns };
  }

  const rep = await runLoop(
    scriptModel(c.model_script ?? []),
    reg,
    c.goal ?? '',
    c.verb === 'run',
    options,
  );

  const e = c.expect;
  if (typeof e['outcome'] === 'string') {
    expect(rep.outcome, 'outcome').toBe(e['outcome']);
  }
  if (typeof e['reason'] === 'string') {
    expect(rep.reason, 'reason').toBe(e['reason']);
  }
  if (Array.isArray(e['executed'])) {
    expect(rep.executed, 'executed').toEqual(e['executed']);
  }
  if (typeof e['audited'] === 'boolean') {
    expect(rep.audited, 'audited').toBe(e['audited']);
  }
}

function driveLinter(c: ConformanceCase): void {
  const cap = c.capability!;
  const report = lint(
    String(cap['name'] ?? ''),
    String(cap['description'] ?? ''),
    Number(cap['params'] ?? 0),
    cap['returns'] === 'raw_row',
  );
  const e = c.expect;
  if (e['lint'] === 'pass') {
    expect(report.ok, `violations: ${report.violations.join(', ')}`).toBe(true);
  }
  if (e['lint'] === 'fail') {
    expect(report.ok).toBe(false);
  }
  if (Array.isArray(e['violations'])) {
    expect([...report.violations].sort()).toEqual([...(e['violations'] as string[])].sort());
  }
}

describe('conformance suite (shared spec/conformance/cases)', () => {
  const cases = loadCases();
  expect(cases.length).toBeGreaterThan(0);

  for (const c of cases) {
    if (c.capability) {
      it(c.name, () => driveLinter(c));
    } else if ((c.model_script ?? []).some((s) => 'program' in s)) {
      // Code-mode program dialect is language-specific (Python source) — see contract §7.5.
      it.skip(`${c.name} (code-mode program is a language-specific dialect)`, () => {});
    } else {
      it(c.name, () => driveGolden(c));
    }
  }
});
