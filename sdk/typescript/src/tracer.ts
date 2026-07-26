/**
 * OpenTrace tracer: ergonomic production of OpenTrace records.
 *
 * The API mirrors the Python SDK. Because TypeScript has no `with` statement, the
 * primary ergonomic pattern is the **callback form** — pass an `async (span) => {}`
 * and the span is ended (and errors recorded) automatically:
 *
 * ```ts
 * import { Tracer, content as C } from "@openagentlabs/opentrace";
 *
 * const tracer = new Tracer({ name: "my-app", producer: { name: "my-app", version: "1.0" },
 *                             output: "trace.ot.jsonl" });
 * const tr = tracer.trace("fix-test", { inputs: { goal: "fix the failing test" } });
 * await tr.turn({ input: [C.user("Fix the auth test")] }, async (turn) => {
 *   await turn.agent({ id: "main", name: "Coder", role: "orchestrator" }, async (ag) => {
 *     await ag.llm({ model: "anthropic/claude-opus-4-8" }, async (call) => {
 *       call.setOutput({ messages: [C.assistant("...", { finishReason: "tool_call" })],
 *                        usage: { input_tokens: 1500, output_tokens: 60 } });
 *     });
 *   });
 * });
 * tr.finish({ outcome: "success" });
 * tracer.close();
 * ```
 *
 * A manual form is always available too:
 *
 * ```ts
 * const s = ag.llm({ model: "openai/gpt-5" });
 * s.setOutput({ messages: [C.assistant("hi")] });
 * s.end();
 * ```
 *
 * The JSON output uses snake_case keys (e.g. `tool_call_id`, `from_agent`,
 * `start_time`) to stay byte-compatible with the Python SDK, even though the TS
 * API surface uses camelCase.
 */
import * as ids from "./ids.js";
import {
  SPEC_VERSION,
  SDK_NAME,
  SDK_VERSION,
  RecordType,
  SpanKind,
  EventName,
  Outcome,
  StatusCode,
  Profile,
} from "./constants.js";
import type { Exporter, Record } from "./exporters.js";
import { JsonlExporter } from "./exporters.js";
import type { Message } from "./content.js";

// --- small utilities -------------------------------------------------------

/** Wall-clock ISO8601 timestamp in UTC with millisecond precision + trailing Z. */
function nowIso(): string {
  return new Date().toISOString();
}

/** Monotonic millisecond clock for durations. */
function perfMs(): number {
  return performance.now();
}

/** Round to `n` decimal places (matches Python's `round(x, n)` closely enough). */
function round(x: number, n: number): number {
  const f = Math.pow(10, n);
  return Math.round(x * f) / f;
}

/** Drop keys whose value is null or undefined (keeps records compact). */
function clean<T extends Dict>(d: T): Dict {
  const out: Dict = {};
  for (const k of Object.keys(d)) {
    const v = d[k];
    if (v !== null && v !== undefined) out[k] = v;
  }
  return out;
}

/** Convert a camelCase key to snake_case (idempotent for already-snake keys). */
function camelToSnake(key: string): string {
  return key.replace(/[A-Z]/g, (m) => "_" + m.toLowerCase());
}

/**
 * Normalize a passthrough `fields` bag: convert its top-level keys from
 * camelCase to snake_case, leaving values untouched (user data such as message
 * arrays must not be rewritten). This lets callers write either `{ toolCallId }`
 * or `{ tool_call_id }` and get identical JSON.
 */
function normFields(fields?: Dict): Dict {
  const out: Dict = {};
  if (!fields) return out;
  for (const k of Object.keys(fields)) {
    out[camelToSnake(k)] = fields[k];
  }
  return out;
}

type Dict = { [key: string]: any };

function errorDict(err: unknown): Dict {
  if (err instanceof Error) {
    const d: Dict = { type: err.name || "Error", message: err.message };
    if (err.stack) d.stack = String(err.stack).slice(0, 4000);
    return d;
  }
  return { type: "Error", message: String(err) };
}

function agentDict(
  id: unknown,
  name: unknown,
  extra: Dict
): Dict {
  return clean({
    id,
    name,
    role: extra.role,
    model: extra.model,
    version: extra.version,
    description: extra.description,
    framework: extra.framework,
  });
}

// --- simple current-span tracking ------------------------------------------

let _currentSpan: Span | null = null;

/** Return the innermost active span (best-effort, synchronous/callback nesting). */
export function currentSpan(): Span | null {
  return _currentSpan;
}

/** A callback that receives the started span. */
export type SpanCallback<T> = (span: Span) => T | Promise<T>;

// --- Tracer ----------------------------------------------------------------

export interface Producer {
  name: string;
  version?: string;
  sdk?: string;
  sdk_version?: string;
  [key: string]: unknown;
}

export interface TracerOptions {
  name?: string;
  producer?: Producer | Dict;
  exporter?: Exporter;
  output?: string;
  profile?: string;
  environment?: string;
  executionContext?: string;
  resource?: Dict;
}

/** Factory and configuration holder for traces. */
export class Tracer {
  readonly name: string;
  readonly producer: Dict;
  readonly exporter: Exporter;
  readonly profile: string;
  readonly environment?: string;
  readonly executionContext?: string;
  readonly resource?: Dict;

  constructor(opts: TracerOptions = {}) {
    this.name = opts.name ?? "opentrace";
    const producer: Dict = { ...(opts.producer ?? { name: this.name }) };
    if (producer.name === undefined) producer.name = this.name;
    if (producer.sdk === undefined) producer.sdk = SDK_NAME;
    if (producer.sdk_version === undefined) producer.sdk_version = SDK_VERSION;
    this.producer = producer;
    this.exporter = opts.exporter ?? new JsonlExporter(opts.output);
    this.profile = opts.profile ?? Profile.STREAM;
    this.environment = opts.environment;
    this.executionContext = opts.executionContext;
    this.resource = opts.resource;
  }

  /** Begin a new trace. The header (`trace.start`) is written immediately. */
  trace(name?: string, fields: Dict = {}): Trace {
    return new Trace(this, name ?? this.name, fields);
  }

  close(): void {
    this.exporter.close();
  }
}

// --- Trace -----------------------------------------------------------------

export interface TraceFinishOptions {
  outcome?: string;
  outputs?: unknown;
  summary?: string;
}

/** One top-level execution. Writes the `trace.start` header and `trace.end` footer. */
export class Trace {
  readonly traceId: string;
  readonly name: string;
  sessionId?: string;

  private readonly tracer: Tracer;
  private readonly exporter: Exporter;
  seq = 0;
  private readonly startedMs = perfMs();
  private outcome?: string;
  private outputs: unknown;
  private error?: Dict;
  private readonly counts: Dict = {
    llm_calls: 0,
    tool_calls: 0,
    agent_invocations: 0,
    turns: 0,
    handoffs: 0,
    errors: 0,
    retries: 0,
  };
  private readonly usageTotal: Dict = {};
  private costTotal = 0;
  private closed = false;
  private parentRoot?: string;

  constructor(tracer: Tracer, name: string, fields: Dict = {}) {
    this.tracer = tracer;
    this.exporter = tracer.exporter;
    this.name = name;

    const norm = normFields(fields);
    let traceId: string | undefined = norm.trace_id as string | undefined;
    const parentTp = norm.parent_traceparent as string | undefined;
    delete norm.trace_id;
    delete norm.parent_traceparent;

    if (parentTp) {
      const parsed = ids.parseTraceparent(parentTp);
      if (parsed) {
        traceId = parsed.traceId;
        this.parentRoot = parsed.parentId;
      }
    }
    this.traceId = traceId ?? ids.newTraceId();
    this.sessionId = norm.session_id as string | undefined;

    // Write the header immediately (equivalent to Python's __enter__).
    const header: Dict = {
      ot: SPEC_VERSION,
      type: RecordType.TRACE_START,
      trace_id: this.traceId,
      seq: this.nextSeq(),
      time: nowIso(),
      name: this.name,
      producer: this.tracer.producer,
      environment: this.tracer.environment,
      execution_context: this.tracer.executionContext,
      resource: this.tracer.resource,
      ...norm,
    };
    this.emit(clean(header));
  }

  /** Write the `trace.end` footer with rolled-up metrics. Idempotent. */
  finish(opts: TraceFinishOptions = {}): void {
    if (this.closed) return;
    this.closed = true;
    if (opts.outcome !== undefined) this.outcome = opts.outcome;
    if (opts.outputs !== undefined) this.outputs = opts.outputs;

    const metrics: Dict = {
      duration_ms: round(perfMs() - this.startedMs, 3),
      total_cost: round(this.costTotal, 8) || undefined,
    };
    for (const [k, v] of Object.entries(this.counts)) {
      if (v) metrics[k] = v;
    }
    if (Object.keys(this.usageTotal).length) {
      metrics.total_tokens =
        this.usageTotal.total_tokens ||
        (this.usageTotal.input_tokens ?? 0) + (this.usageTotal.output_tokens ?? 0);
    }

    const footer: Dict = {
      ot: SPEC_VERSION,
      type: RecordType.TRACE_END,
      trace_id: this.traceId,
      seq: this.nextSeq(),
      time: nowIso(),
      outcome: this.outcome ?? Outcome.SUCCESS,
      outputs: this.outputs,
      summary: opts.summary,
      error: this.error,
      metrics: clean(metrics),
    };
    this.emit(clean(footer));
  }

  /** A W3C `traceparent` describing this trace, for cross-process propagation. */
  traceparent(): string {
    return ids.formatTraceparent(this.traceId, this.parentRoot ?? "0".repeat(16));
  }

  // -- span factories -------------------------------------------------------

  span(kind: string, name?: string, fields?: Dict): Span;
  span<T>(kind: string, name: string | undefined, fields: Dict, fn: SpanCallback<T>): Promise<T>;
  span<T>(
    kind: string,
    name?: string,
    fields: Dict = {},
    fn?: SpanCallback<T>
  ): Span | Promise<T> {
    const norm = normFields(fields);
    const profile = (norm.profile as string) ?? this.tracer.profile;
    delete norm.profile;
    const parent = (norm.parent as string) ?? this.parentRoot;
    delete norm.parent;
    const span = new Span(this, kind, name, parent, profile, norm);
    return this.run(span, fn);
  }

  turn(fields?: Dict): Span;
  turn<T>(fields: Dict, fn: SpanCallback<T>): Promise<T>;
  turn<T>(fields: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    this.counts.turns += 1;
    const norm = normFields(fields);
    const name = (norm.name as string) ?? "turn";
    delete norm.name;
    return this.span(SpanKind.TURN, name, norm, fn as SpanCallback<T>) as Span | Promise<T>;
  }

  agent(fields?: Dict): Span;
  agent<T>(fields: Dict, fn: SpanCallback<T>): Promise<T>;
  agent<T>(fields: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const norm = normFields(fields);
    const name = norm.name as string | undefined;
    const spanFields: Dict = { agent: agentDict(norm.id, norm.name, norm) };
    for (const k of ["model", "provider", "framework"]) {
      if (norm[k] !== undefined) spanFields[k] = norm[k];
    }
    return this.span(SpanKind.AGENT, name, spanFields, fn as SpanCallback<T>) as
      | Span
      | Promise<T>;
  }

  workflow(fields?: Dict): Span;
  workflow<T>(fields: Dict, fn: SpanCallback<T>): Promise<T>;
  workflow<T>(fields: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const norm = normFields(fields);
    const name = norm.name as string | undefined;
    delete norm.name;
    return this.span(SpanKind.WORKFLOW, name, norm, fn as SpanCallback<T>) as
      | Span
      | Promise<T>;
  }

  // -- internals ------------------------------------------------------------

  private run<T>(span: Span, fn?: SpanCallback<T>): Span | Promise<T> {
    if (!fn) return span;
    return (async () => {
      try {
        const result = await fn(span);
        span.end();
        return result;
      } catch (e) {
        span.recordError(e);
        span.end();
        throw e;
      }
    })();
  }

  /** @internal */
  nextSeq(): number {
    const s = this.seq;
    this.seq += 1;
    return s;
  }

  /** @internal */
  emit(record: Record): void {
    this.exporter.export(record);
  }

  /** @internal Accumulate roll-up counts, usage, and cost. */
  account(kind: string, usage?: Dict, cost?: Dict): void {
    if (kind === SpanKind.LLM) this.counts.llm_calls += 1;
    else if (kind === SpanKind.TOOL) this.counts.tool_calls += 1;
    else if (kind === SpanKind.AGENT) this.counts.agent_invocations += 1;
    else if (kind === SpanKind.HANDOFF) this.counts.handoffs += 1;
    if (usage) {
      for (const [k, v] of Object.entries(usage)) {
        if (typeof v === "number")
          this.usageTotal[k] = (this.usageTotal[k] ?? 0) + v;
      }
    }
    if (cost && typeof cost.total === "number") this.costTotal += cost.total;
  }

  /** @internal */
  incrementErrors(): void {
    this.counts.errors += 1;
  }

  /** @internal */
  recordTraceError(err: unknown): void {
    this.error = errorDict(err);
    this.counts.errors += 1;
    if (this.outcome === undefined) this.outcome = Outcome.FAILURE;
  }
}

// --- Span option shapes ----------------------------------------------------

export interface LlmOptions extends Dict {
  model?: string;
  provider?: string;
  input?: Message[];
  request?: Dict;
  name?: string;
}

export interface ToolOptions extends Dict {
  arguments?: unknown;
  toolCallId?: string;
  toolType?: string;
  name?: string;
}

export interface AgentOptions extends Dict {
  id?: string;
  name?: string;
  role?: string;
  model?: string;
}

export interface PlanOptions extends Dict {
  goal?: string;
  steps?: unknown[];
  revision?: number;
  planId?: string;
  name?: string;
}

export interface HandoffOptions extends Dict {
  fromAgent: string;
  toAgent: string;
  mode?: string;
  reason?: string;
  payload?: unknown;
  returns?: boolean;
  name?: string;
}

export interface SetOutputOptions {
  messages?: Message[];
  usage?: Dict;
  cost?: Dict;
  response?: unknown;
  output?: unknown;
}

// --- Span ------------------------------------------------------------------

/** A unit of work. Emits per the chosen profile (stream: start/end, batch: single). */
export class Span {
  readonly kind: string;
  readonly name: string;
  readonly parentId?: string;
  readonly spanId: string;
  readonly traceId: string;
  turnId?: string;

  private readonly trace: Trace;
  private readonly profile: string;
  private readonly startTime: string;
  private readonly startPerf: number;
  private fields: Dict;
  private output: unknown;
  private usage?: Dict;
  private cost?: Dict;
  private status?: Dict;
  private error?: Dict;
  private metrics?: Dict;
  private closed = false;

  constructor(
    trace: Trace,
    kind: string,
    name: string | undefined,
    parentId: string | undefined,
    profile: string,
    fields: Dict
  ) {
    this.trace = trace;
    this.kind = kind;
    this.name = name ?? kind;
    this.parentId = parentId;
    this.spanId = ids.newSpanId();
    this.traceId = trace.traceId;
    this.profile = profile;
    this.startTime = nowIso();
    this.startPerf = perfMs();
    this.fields = clean(fields);

    this.turnId = this.fields.turn_id as string | undefined;
    if (this.turnId === undefined && kind === SpanKind.TURN) {
      this.turnId = this.spanId;
    }

    // Start immediately (equivalent to Python's __enter__).
    _currentSpan = this;
    if (this.profile === Profile.STREAM) {
      const rec: Dict = {
        ot: SPEC_VERSION,
        type: RecordType.SPAN_START,
        trace_id: this.traceId,
        span_id: this.spanId,
        parent_id: this.parentId,
        seq: this.trace.nextSeq(),
        time: this.startTime,
        kind: this.kind,
        name: this.name,
        turn_id: this.turnId,
        session_id: this.trace.sessionId,
        ...this.fields,
      };
      this.trace.emit(clean(rec));
    }
  }

  /** Emit the span's end (`span.end` for stream, compact `span` for batch). Idempotent. */
  end(status?: string): void {
    if (this.closed) return;
    this.closed = true;
    if (status !== undefined) this.status = { code: status };
    if (this.status === undefined) {
      this.status = { code: this.error ? StatusCode.ERROR : StatusCode.OK };
    }
    const endTime = nowIso();
    const durationMs = round(perfMs() - this.startPerf, 3);
    this.trace.account(this.kind, this.usage, this.cost);

    const common: Dict = {
      ot: SPEC_VERSION,
      trace_id: this.traceId,
      span_id: this.spanId,
      kind: this.kind,
      status: this.status,
      output: this.output,
      usage: this.usage,
      cost: this.cost,
      error: this.error,
      turn_id: this.turnId,
    };

    if (this.profile === Profile.STREAM) {
      const rec: Dict = {
        ...common,
        type: RecordType.SPAN_END,
        seq: this.trace.nextSeq(),
        time: endTime,
      };
      if (this.metrics) rec.metrics = this.metrics;
      this.trace.emit(clean(rec));
    } else {
      const rec: Dict = {
        ...common,
        ...this.fields,
        type: RecordType.SPAN,
        parent_id: this.parentId,
        seq: this.trace.nextSeq(),
        time: this.startTime,
        name: this.name,
        start_time: this.startTime,
        end_time: endTime,
        duration_ms: durationMs,
        session_id: this.trace.sessionId,
      };
      if (this.metrics) rec.metrics = this.metrics;
      this.trace.emit(clean(rec));
    }
    if (_currentSpan === this) _currentSpan = null;
  }

  // -- setters --------------------------------------------------------------

  setField(fields: Dict): this {
    Object.assign(this.fields, clean(normFields(fields)));
    return this;
  }

  setInput(messages?: Message[], extra?: Dict): this {
    if (messages !== undefined) this.fields.input = { messages };
    if (extra) Object.assign(this.fields, clean(normFields(extra)));
    return this;
  }

  setOutput(opts: SetOutputOptions): this {
    if (opts.messages !== undefined) this.output = { messages: opts.messages };
    else if (opts.output !== undefined) this.output = opts.output;
    if (opts.usage !== undefined) this.setUsage(opts.usage);
    if (opts.cost !== undefined) this.cost = opts.cost;
    if (opts.response !== undefined) this.fields.response = opts.response;
    return this;
  }

  setUsage(usage: Dict): this {
    const u: Dict = { ...usage };
    if (
      u.total_tokens === undefined &&
      u.input_tokens !== undefined &&
      u.output_tokens !== undefined
    ) {
      u.total_tokens = u.input_tokens + u.output_tokens;
    }
    this.usage = u;
    return this;
  }

  setCost(cost: Dict): this {
    this.cost = clean(cost);
    return this;
  }

  setResult(result: unknown, isError = false): this {
    this.output = result;
    this.fields.result = result;
    if (isError) this.status = { code: StatusCode.ERROR };
    return this;
  }

  setStatus(code: string, message?: string): this {
    this.status = clean({ code, message });
    return this;
  }

  setMetrics(metrics: Dict): this {
    this.metrics = clean(normFields(metrics));
    return this;
  }

  recordError(err: unknown): this {
    this.error = errorDict(err);
    this.status = { code: StatusCode.ERROR };
    this.trace.incrementErrors();
    return this;
  }

  // -- child span factories -------------------------------------------------

  span(kind: string, name?: string, fields?: Dict): Span;
  span<T>(kind: string, name: string | undefined, fields: Dict, fn: SpanCallback<T>): Promise<T>;
  span<T>(
    kind: string,
    name?: string,
    fields: Dict = {},
    fn?: SpanCallback<T>
  ): Span | Promise<T> {
    const norm = normFields(fields);
    if (norm.turn_id === undefined) norm.turn_id = this.turnId;
    const profile = (norm.profile as string) ?? this.profile;
    delete norm.profile;
    const span = new Span(this.trace, kind, name, this.spanId, profile, norm);
    return this.run(span, fn);
  }

  llm(opts?: LlmOptions): Span;
  llm<T>(opts: LlmOptions, fn: SpanCallback<T>): Promise<T>;
  llm<T>(opts: LlmOptions = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { model, provider, input, request, name, ...rest } = opts;
    const fields: Dict = { model, provider, request, ...normFields(rest) };
    const spanName = name ?? (model ? "chat " + model : "chat");
    const result = this.span(SpanKind.LLM, spanName, fields);
    if (input !== undefined) result.setInput(input);
    return this.run(result, fn);
  }

  tool(toolName: string, opts?: ToolOptions): Span;
  tool<T>(toolName: string, opts: ToolOptions, fn: SpanCallback<T>): Promise<T>;
  tool<T>(
    toolName: string,
    opts: ToolOptions = {},
    fn?: SpanCallback<T>
  ): Span | Promise<T> {
    const { arguments: args, toolCallId, toolType, name, ...rest } = opts;
    const fields: Dict = {
      tool_name: toolName,
      tool_call_id: toolCallId,
      tool_type: toolType ?? "function",
      arguments: args,
      ...normFields(rest),
    };
    const span = this.span(SpanKind.TOOL, name ?? toolName, fields);
    return this.run(span, fn);
  }

  agent(opts?: AgentOptions): Span;
  agent<T>(opts: AgentOptions, fn: SpanCallback<T>): Promise<T>;
  agent<T>(opts: AgentOptions = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { id, name, role, model, ...rest } = opts;
    const norm = normFields(rest);
    const fields: Dict = {
      agent: agentDict(id, name, { role, model, ...norm }),
      model,
      ...norm,
    };
    const span = this.span(SpanKind.AGENT, name, fields);
    return this.run(span, fn);
  }

  plan(opts?: PlanOptions): Span;
  plan<T>(opts: PlanOptions, fn: SpanCallback<T>): Promise<T>;
  plan<T>(opts: PlanOptions = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { goal, steps, revision = 1, planId = "plan_1", name, ...rest } = opts;
    const planObj = clean({ id: planId, goal, revision, steps });
    const span = this.span(SpanKind.PLAN, name ?? "plan", {
      plan: planObj,
      ...normFields(rest),
    });
    return this.run(span, fn);
  }

  handoff(opts: HandoffOptions): Span;
  handoff<T>(opts: HandoffOptions, fn: SpanCallback<T>): Promise<T>;
  handoff<T>(opts: HandoffOptions, fn?: SpanCallback<T>): Span | Promise<T> {
    const {
      fromAgent,
      toAgent,
      mode = "delegate",
      reason,
      payload,
      returns = true,
      name,
      ...rest
    } = opts;
    const span = this.span(SpanKind.HANDOFF, name ?? "handoff -> " + toAgent, {
      from_agent: fromAgent,
      to_agent: toAgent,
      mode,
      reason,
      payload,
      returns,
      ...normFields(rest),
    });
    return this.run(span, fn);
  }

  classifier(opts?: Dict): Span;
  classifier<T>(opts: Dict, fn: SpanCallback<T>): Promise<T>;
  classifier<T>(opts: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { name, candidates, chosen, confidence, scores, rationale, model, ...rest } =
      opts;
    const span = this.span(SpanKind.CLASSIFIER, name ?? "classifier", {
      candidates,
      chosen,
      confidence,
      scores,
      rationale,
      model,
      ...normFields(rest),
    });
    return this.run(span, fn);
  }

  guardrail(opts?: Dict): Span;
  guardrail<T>(opts: Dict, fn: SpanCallback<T>): Promise<T>;
  guardrail<T>(opts: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { policy, verdict = "pass", categories, score, name, ...rest } = opts;
    const span = this.span(SpanKind.GUARDRAIL, name ?? policy, {
      policy,
      verdict,
      categories,
      score,
      ...normFields(rest),
    });
    return this.run(span, fn);
  }

  retriever(opts?: Dict): Span;
  retriever<T>(opts: Dict, fn: SpanCallback<T>): Promise<T>;
  retriever<T>(opts: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { query, documents, name, ...rest } = opts;
    const span = this.span(SpanKind.RETRIEVER, name ?? "retrieval", {
      query,
      documents,
      ...normFields(rest),
    });
    return this.run(span, fn);
  }

  evaluator(opts?: Dict): Span;
  evaluator<T>(opts: Dict, fn: SpanCallback<T>): Promise<T>;
  evaluator<T>(opts: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { name, targetSpanId, ...rest } = opts;
    const span = this.span(SpanKind.EVALUATOR, name ?? "evaluator", {
      target_span_id: targetSpanId,
      ...normFields(rest),
    });
    return this.run(span, fn);
  }

  human(opts?: Dict): Span;
  human<T>(opts: Dict, fn: SpanCallback<T>): Promise<T>;
  human<T>(opts: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { interaction = "approval", name, ...rest } = opts;
    const span = this.span(SpanKind.HUMAN, name ?? interaction, {
      interaction,
      ...normFields(rest),
    });
    return this.run(span, fn);
  }

  harness(opts?: Dict): Span;
  harness<T>(opts: Dict, fn: SpanCallback<T>): Promise<T>;
  harness<T>(opts: Dict = {}, fn?: SpanCallback<T>): Span | Promise<T> {
    const { harnessName, role = "harness", name, ...rest } = opts;
    const span = this.span(SpanKind.HARNESS, name ?? harnessName, {
      harness_name: harnessName,
      role,
      ...normFields(rest),
    });
    return this.run(span, fn);
  }

  // -- point events ---------------------------------------------------------

  /** Emit a point `event` record attached to this span. */
  event(name: string, fields: Dict = {}): void {
    const rec: Dict = {
      ot: SPEC_VERSION,
      type: RecordType.EVENT,
      trace_id: this.traceId,
      span_id: this.spanId,
      seq: this.trace.nextSeq(),
      time: nowIso(),
      name,
      turn_id: this.turnId,
      ...clean(fields),
    };
    this.trace.emit(clean(rec));
  }

  reasoning(
    content: string | Dict[],
    opts: { signature?: string; redacted?: boolean; effort?: string } = {}
  ): void {
    const parts = Array.isArray(content)
      ? content
      : [
          clean({
            type: "reasoning",
            text: content,
            signature: opts.signature,
            redacted: opts.redacted || undefined,
          }),
        ];
    this.event(EventName.REASONING, { content: parts, effort: opts.effort });
  }

  decision(
    chosen: unknown,
    opts: { options?: unknown[]; rationale?: string; branch?: string } = {}
  ): void {
    this.event(EventName.DECISION, {
      chosen,
      options: opts.options,
      rationale: opts.rationale,
      branch: opts.branch,
    });
  }

  planUpdate(
    planId: string,
    stepId: string,
    status: string,
    delta?: unknown
  ): void {
    this.event(EventName.PLAN_UPDATE, {
      plan_id: planId,
      step_id: stepId,
      status,
      delta,
    });
  }

  delta(content: string, channel = "text", index = 0): void {
    this.event(EventName.DELTA, { content, channel, index });
  }

  score(
    name: string,
    value: unknown,
    opts: { dataType?: string; comment?: string; targetSpanId?: string } = {}
  ): void {
    this.event(EventName.SCORE, {
      score_name: name,
      value,
      data_type: opts.dataType ?? "numeric",
      comment: opts.comment,
      target_span_id: opts.targetSpanId ?? this.spanId,
    });
  }

  log(message: string, level = "info", data?: Dict): void {
    this.event(EventName.LOG, {
      message,
      level,
      data: data && Object.keys(data).length ? data : undefined,
    });
  }

  artifact(artifact: Dict): void {
    this.event(EventName.ARTIFACT, { artifact });
  }

  // -- internal -------------------------------------------------------------

  private run<T>(span: Span, fn?: SpanCallback<T>): Span | Promise<T> {
    if (!fn) return span;
    return (async () => {
      try {
        const result = await fn(span);
        span.end();
        return result;
      } catch (e) {
        span.recordError(e);
        span.end();
        throw e;
      }
    })();
  }
}
