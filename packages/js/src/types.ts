/**
 * Core value types for the Reins TypeScript port (M7.1) — the same shapes as the
 * Python and Go packages and the language-neutral contract in `spec/contract.md`
 * (invariant §2.21). The shared conformance suite (`spec/conformance`) locks their
 * behavior across all three languages.
 *
 * Field names are snake_case to match the contract wire format (and mainstream LLM
 * APIs — OpenAI's own JSON uses `finish_reason`, `tool_calls`, `input_schema`), so
 * JSON serialization is identity and every conformance case maps straight onto
 * these types with no field renaming.
 */

/** A JSON Schema object. Kept loose on purpose — it is passed straight to the model. */
export type JSONSchema = Record<string, unknown>;

/** Who produced a {@link Message}. */
export type Role = 'system' | 'user' | 'assistant' | 'tool';

/** Why a model turn (or a whole run) ended. */
export type FinishReason = 'stop' | 'tool_calls' | 'length' | 'interrupted' | 'error';

/**
 * A capability's danger classification, ordered by danger. The harness defaults to
 * `'write'` when classification is ambiguous (invariant §2.4) — never `'read'`.
 */
export type Access = 'read' | 'write' | 'destructive';

/** A model's request to call one capability, with its JSON arguments. */
export interface ToolCall {
  id: string;
  name: string;
  arguments?: Record<string, unknown>;
}

/** One turn in the conversation (system / user / assistant / tool). */
export interface Message {
  role: Role;
  text?: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  meta?: Record<string, unknown>;
}

/** Token + cost accounting for a model call (or a cumulative total). */
export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cost: number;
}

/** One model turn: the assistant message, why it stopped, its usage. */
export interface ModelResponse {
  message: Message;
  finish_reason: FinishReason;
  usage: Usage;
}

/**
 * A capability's intent surface — the ONLY shape that crosses to the model
 * (invariant §2.5): name, description, input schema; never policy metadata.
 */
export interface ToolSpec {
  name: string;
  description: string;
  input_schema?: JSONSchema;
}

/**
 * One operation the agent may perform: a typed, intent-named function plus safety
 * metadata. `input_schema` is a JSON Schema object.
 */
export interface Capability {
  name: string;
  description: string;
  input_schema?: JSONSchema;
  access: Access;
  confirm: boolean;
  idempotent: boolean;
  scope?: string;
}

/**
 * The intent surface shown to the model — policy metadata (`access`/`confirm`/
 * `scope`) stays harness-side. Mirrors Go's `Capability.ToolSpec()` and Python's
 * `Capability.tool_spec()`.
 */
export function toolSpec(cap: Capability): ToolSpec {
  const spec: ToolSpec = { name: cap.name, description: cap.description };
  if (cap.input_schema !== undefined) {
    spec.input_schema = cap.input_schema;
  }
  return spec;
}

/**
 * Errors-as-data: the outcome of executing a capability. On success `ok` is true
 * and `value` holds the result; on failure `ok` is false and `error` explains why.
 * The model sees this and adapts — capability failures never abort the loop.
 */
export interface CapabilityResult {
  ok: boolean;
  value?: unknown;
  error?: string;
  retryable?: boolean;
}

/** Build a successful {@link CapabilityResult}. */
export function resultOk(value?: unknown): CapabilityResult {
  return { ok: true, value };
}

/** Build a failed {@link CapabilityResult} the model can see and react to. */
export function resultError(error: string, retryable = false): CapabilityResult {
  return { ok: false, error, retryable };
}

/**
 * Who a run acts for, and its trace identity (invariants §2.7–§2.8). `principal` is
 * set once at the entry point and propagated into every capability call; the model
 * can never set or spoof it.
 */
export interface Context {
  principal?: string;
  trace_id?: string;
}

/** The final result of a run: output message, why it ended, usage, trace id. */
export interface RunResult {
  output: Message;
  reason: FinishReason;
  usage: Usage;
  trace_id: string;
}

/** The evolving state of a run, passed to stop conditions. */
export interface RunState {
  messages?: Message[];
  turn: number;
  cumulative_usage: Usage;
  last_response?: ModelResponse;
}

/**
 * One table (or model) in the application's data model, for progressive disclosure
 * (schema-RAG).
 */
export interface TableInfo {
  name: string;
  description?: string;
  columns?: string[];
}

/** The searchable data model — populated by hand or by the ORM adapters. */
export interface SchemaIndex {
  tables?: TableInfo[];
}

/**
 * One recorded step of a run, for explain()/--trace (M5.1). `kind` is a short tag;
 * `detail` is the human-readable line (capability arguments redacted); `ok` marks
 * whether the step succeeded.
 */
export interface TraceEvent {
  kind: string;
  detail: string;
  ok: boolean;
}

/** The knobs for one model call. An empty `model` lets the adapter pick its default. */
export interface ModelParams {
  model?: string;
  max_tokens: number;
}

/** Sane defaults (max_tokens 1024), matching the Python and Go ports. */
export function defaultModelParams(): ModelParams {
  return { max_tokens: 1024 };
}
