#!/usr/bin/env node
import { createInterface } from 'node:readline/promises';
import { VERSION } from '../index.js';
import { resolveModel, defaultModel } from '../openai.js';
import type { Model } from '../model.js';
import { ReinsError } from '../errors.js';

// The CLI commands live in ./cli.js, which imports node:sqlite. It is imported
// lazily (only for the commands that touch a database) so `version`/`help` don't
// trigger node:sqlite's experimental-feature warning.
type Writer = (line: string) => void;

/**
 * The `reins` CLI entry point. Point it at a SQLite database and inspect / ask /
 * chat over it with no hand-written code:
 *
 *   reins inspect ./app.db
 *   reins ask ./app.db "how many active customers?"
 *   reins chat ./app.db --model groq:llama-3.3-70b-versatile
 *
 * The model is resolved from --model or the environment (OPENAI_API_KEY,
 * GEMINI_API_KEY, GROQ_API_KEY, ZAI_API_KEY, OLLAMA_BASE_URL, VLLM_BASE_URL).
 */

const usage = `reins ${VERSION} — operate your app in plain language (TypeScript)

usage:
  reins inspect <db-url> [--write]              list the capabilities reins would expose
  reins ask     <db-url> "<question>" [--model <spec>]
  reins chat    <db-url> [--model <spec>]
  reins version

db-url:  ./app.db   :memory:   sqlite:///abs/path/app.db
--model: <spec> is "provider:model" (e.g. groq:llama-3.3-70b-versatile) or a bare
         model name; omit to auto-detect from the environment.
`;

interface Flags {
  write: boolean;
  model?: string;
}

function parseArgs(args: string[]): { positional: string[]; flags: Flags } {
  const positional: string[] = [];
  const flags: Flags = { write: false };
  for (let i = 0; i < args.length; i++) {
    const a = args[i]!;
    if (a === '--write' || a === '-w') {
      flags.write = true;
    } else if (a === '--model' || a === '-m') {
      if (i + 1 < args.length) {
        flags.model = args[++i];
      }
    } else {
      positional.push(a);
    }
  }
  return { positional, flags };
}

function requireDbUrl(positional: string[], cmd: string): string {
  if (positional.length === 0) {
    throw new ReinsError(
      `${cmd} needs a database URL`,
      `reins ${cmd} <db-url>  (e.g. ./app.db or :memory:)`,
    );
  }
  return positional[0]!;
}

function resolve(spec: string | undefined): Model {
  return spec ? resolveModel(spec) : defaultModel();
}

async function run(args: string[]): Promise<void> {
  if (args.length === 0) {
    process.stdout.write(usage);
    return;
  }
  const [cmd, ...rest] = args;
  const out: Writer = (line) => process.stdout.write(line + '\n');

  switch (cmd) {
    case 'version':
    case '--version':
    case '-v':
      out(`reins ${VERSION}`);
      return;
    case 'help':
    case '-h':
    case '--help':
      process.stdout.write(usage);
      return;
    case 'inspect': {
      const { positional, flags } = parseArgs(rest);
      const cli = await import('./cli.js');
      cli.inspect(requireDbUrl(positional, 'inspect'), flags.write, out);
      return;
    }
    case 'ask': {
      const { positional, flags } = parseArgs(rest);
      const dbUrl = requireDbUrl(positional, 'ask');
      if (positional.length < 2) {
        throw new ReinsError('ask needs a question', 'reins ask <db-url> "your question"');
      }
      const question = positional.slice(1).join(' ');
      const cli = await import('./cli.js');
      await cli.ask(dbUrl, question, resolve(flags.model), out);
      return;
    }
    case 'chat': {
      const { positional, flags } = parseArgs(rest);
      const dbUrl = requireDbUrl(positional, 'chat');
      const model = resolve(flags.model);
      const cli = await import('./cli.js');
      const rl = createInterface({ input: process.stdin, output: process.stdout });
      const prompt = async (label: string): Promise<string | null> => {
        try {
          return await rl.question(label);
        } catch {
          return null; // EOF / closed stdin
        }
      };
      try {
        await cli.chat(dbUrl, model, prompt, out);
      } finally {
        rl.close();
      }
      return;
    }
    default:
      throw new ReinsError(
        `unknown command: ${cmd}`,
        'run `reins help` to see the available commands',
      );
  }
}

run(process.argv.slice(2)).catch((e: unknown) => {
  process.stderr.write(String(e) + '\n');
  process.exit(1);
});
