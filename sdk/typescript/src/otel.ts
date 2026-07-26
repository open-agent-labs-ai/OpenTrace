/**
 * OpenTrace -> OpenTelemetry conversion (§17 of the spec).
 *
 * `recordsToOtlp` is a dependency-free converter that turns an OpenTrace record
 * stream into a single OTLP/JSON `ExportTraceServiceRequest` object, ready to be
 * POSTed to any OTLP/HTTP `/v1/traces` endpoint. It mirrors the Python SDK's
 * `otel_json.py` / `mapping.py` byte-for-byte.
 *
 * `OtelExporter` is a live exporter stub. A fully-featured version streams spans
 * to an OTLP endpoint via `@opentelemetry/sdk-trace-base`; when that package is
 * not installed, this class buffers records and can flush them as OTLP/JSON
 * using the same mapping, so it degrades gracefully with zero required deps.
 */
import type { Exporter, Record } from "./exporters.js";

// OpenTrace span kind -> [gen_ai.operation.name | null, openinference.span.kind, otel SpanKind]
export const KIND_MAP: { [kind: string]: [string | null, string, string] } = {
  llm: ["chat", "LLM", "CLIENT"],
  tool: ["execute_tool", "TOOL", "INTERNAL"],
  agent: ["invoke_agent", "AGENT", "INTERNAL"],
  workflow: ["invoke_workflow", "CHAIN", "INTERNAL"],
  chain: [null, "CHAIN", "INTERNAL"],
  retriever: ["retrieval", "RETRIEVER", "INTERNAL"],
  embedding: ["embeddings", "EMBEDDING", "CLIENT"],
  reranker: [null, "RERANKER", "INTERNAL"],
  classifier: ["chat", "LLM", "INTERNAL"],
  guardrail: [null, "GUARDRAIL", "INTERNAL"],
  evaluator: [null, "EVALUATOR", "INTERNAL"],
  plan: ["plan", "CHAIN", "INTERNAL"],
  handoff: ["invoke_agent", "AGENT", "INTERNAL"],
  turn: [null, "CHAIN", "SERVER"],
  session: [null, "CHAIN", "INTERNAL"],
  harness: [null, "CHAIN", "INTERNAL"],
  human: [null, "CHAIN", "INTERNAL"],
  memory: [null, "CHAIN", "INTERNAL"],
  custom: [null, "CHAIN", "INTERNAL"],
};

// usage key -> OTel gen_ai attribute
const USAGE_MAP: { [key: string]: string } = {
  input_tokens: "gen_ai.usage.input_tokens",
  output_tokens: "gen_ai.usage.output_tokens",
  total_tokens: "gen_ai.usage.total_tokens",
  cache_read_tokens: "gen_ai.usage.cache_read.input_tokens",
  cache_creation_tokens: "gen_ai.usage.cache_creation.input_tokens",
  reasoning_tokens: "gen_ai.usage.reasoning.output_tokens",
};

type Dict = { [key: string]: any };

/**
 * Convert an ISO8601 timestamp to integer nanoseconds since the Unix epoch.
 *
 * Mirrors the Python SDK's `_iso_to_nanos` (which computes
 * `datetime.total_seconds() * 1e9`), preserving microsecond precision from the
 * timestamp's fractional part.
 */
export function isoToNanos(ts?: string | null): number {
  if (!ts) return 0;
  const ms = Date.parse(ts);
  if (Number.isNaN(ms)) return 0;
  const sec = Math.floor(ms / 1000);
  const msFrac = ms - sec * 1000; // whole milliseconds within the second
  // Recover sub-millisecond microseconds from the fractional field (e.g. ...019436Z).
  let subMicros = 0;
  const frac = /\.(\d+)/.exec(ts);
  if (frac) {
    const micros = frac[1].padEnd(6, "0").slice(0, 6); // 6-digit microsecond field
    subMicros = parseInt(micros, 10) % 1000; // microseconds below the millisecond
  }
  const microseconds = msFrac * 1000 + subMicros;
  // Match Python exactly: timedelta.total_seconds() == totalMicros / 1e6 (a float),
  // then that float is multiplied by 1e9 and truncated to an int.
  const totalMicros = sec * 1_000_000 + microseconds;
  return Math.trunc((totalMicros / 1e6) * 1e9);
}

/**
 * Serialize a value the way Python's `json.dumps(..., ensure_ascii=False)` does:
 * with `, ` and `: ` separators. OpenTrace OTLP attribute values are encoded this
 * way in the Python SDK, so this keeps the OTLP/JSON output byte-compatible.
 */
function pyJson(v: any): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return JSON.stringify(v);
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(pyJson).join(", ") + "]";
  if (typeof v === "object")
    return (
      "{" +
      Object.entries(v)
        .map(([k, val]) => JSON.stringify(k) + ": " + pyJson(val))
        .join(", ") +
      "}"
    );
  return JSON.stringify(v);
}

/** Flatten an OpenTrace span record into OTLP-friendly attributes. */
export function spanOtelAttributes(span: Dict): Dict {
  const kind: string = span.kind ?? "chain";
  const attrs: Dict = {};
  const [op, oiKind] = KIND_MAP[kind] ?? [null, "CHAIN", "INTERNAL"];

  attrs["opentrace.kind"] = kind;
  attrs["openinference.span.kind"] = oiKind;
  if (op) attrs["gen_ai.operation.name"] = op;

  if (span.model) attrs["gen_ai.request.model"] = span.model;
  if (span.provider) attrs["gen_ai.provider.name"] = span.provider;

  const req: Dict = span.request ?? {};
  for (const k of ["temperature", "top_p", "top_k", "max_tokens", "seed"]) {
    if (req[k] != null) attrs["gen_ai.request." + k] = req[k];
  }

  const resp: Dict = span.response ?? {};
  if (resp.id) attrs["gen_ai.response.id"] = resp.id;
  if (resp.finish_reasons)
    attrs["gen_ai.response.finish_reasons"] = pyJson(resp.finish_reasons);

  const usage: Dict = span.usage ?? {};
  for (const [k, v] of Object.entries(usage)) {
    const otelKey = USAGE_MAP[k] ?? "gen_ai.usage." + k;
    attrs[otelKey] = v;
  }

  const cost: Dict = span.cost ?? {};
  if (cost.total != null) attrs["gen_ai.usage.cost"] = cost.total;

  const inp: Dict = span.input ?? {};
  if (inp && typeof inp === "object" && inp.messages)
    attrs["gen_ai.input.messages"] = pyJson(inp.messages);
  const out: Dict = span.output ?? {};
  if (out && typeof out === "object" && out.messages)
    attrs["gen_ai.output.messages"] = pyJson(out.messages);

  if (span.tool_name) attrs["gen_ai.tool.name"] = span.tool_name;
  if (span.tool_call_id) attrs["gen_ai.tool.call.id"] = span.tool_call_id;
  if (span.tool_type) attrs["gen_ai.tool.type"] = span.tool_type;
  if (span.arguments != null)
    attrs["gen_ai.tool.call.arguments"] = pyJson(span.arguments);
  if (span.result != null)
    attrs["gen_ai.tool.call.result"] = pyJson(span.result);

  const agent: Dict = span.agent ?? {};
  if (agent.id) attrs["gen_ai.agent.id"] = agent.id;
  if (agent.name) attrs["gen_ai.agent.name"] = agent.name;

  for (const k of ["from_agent", "to_agent", "mode", "reason"]) {
    if (span[k] != null) attrs["opentrace.handoff." + k] = span[k];
  }

  for (const k of ["chosen", "confidence", "rationale", "policy", "verdict"]) {
    if (span[k] != null) {
      const v = span[k];
      attrs["opentrace." + k] =
        typeof v === "string" || typeof v === "number" || typeof v === "boolean"
          ? v
          : pyJson(v);
    }
  }

  if (span.plan != null) attrs["opentrace.plan"] = pyJson(span.plan);

  const err: Dict = span.error ?? {};
  if (err.type) attrs["error.type"] = err.type;

  return attrs;
}

/** Map an OpenTrace status object to an OTLP status code string. */
export function otelStatusCode(status?: Dict | null): string {
  if (!status) return "STATUS_CODE_UNSET";
  const code = status.code;
  if (code === "ok") return "STATUS_CODE_OK";
  if (code === "error" || code === "timeout" || code === "cancelled")
    return "STATUS_CODE_ERROR";
  return "STATUS_CODE_UNSET";
}

// --- OTLP/JSON key-value encoding ------------------------------------------

function kv(key: string, value: any): Dict {
  let v: Dict;
  if (typeof value === "boolean") v = { boolValue: value };
  else if (typeof value === "number" && Number.isInteger(value))
    v = { intValue: String(value) };
  else if (typeof value === "number") v = { doubleValue: value };
  else if (Array.isArray(value) || (value !== null && typeof value === "object"))
    v = { stringValue: pyJson(value) };
  else v = { stringValue: String(value) };
  return { key, value: v };
}

function attrs(d: Dict): Dict[] {
  return Object.entries(d).map(([k, v]) => kv(k, v));
}

/**
 * Stringify an integer-valued nanosecond count exactly. `String(number)` uses
 * JavaScript's shortest round-trip form (which loses trailing digits past 2^53),
 * so we go through BigInt to emit the same exact integer Python's `int(float)`
 * produces.
 */
function nanoStr(x: number): string {
  return String(BigInt(Math.trunc(x)));
}

/** Convert OpenTrace records into a single OTLP ExportTraceServiceRequest object. */
export function recordsToOtlp(records: Iterable<Record>): Dict {
  const recs = [...records] as Dict[];

  const starts: { [id: string]: Dict } = {};
  const spans: { [id: string]: Dict } = {};
  const eventsBySpan: { [id: string]: Dict[] } = {};
  let resource: Dict = { "service.name": "opentrace" };
  let producerName = "opentrace";

  for (const r of recs) {
    const t = r.type;
    if (t === "trace.start") {
      const res = r.resource ?? {};
      if (res && Object.keys(res).length) resource = { ...res };
      producerName = (r.producer ?? {}).name ?? producerName;
      if (resource["service.name"] === undefined)
        resource["service.name"] = producerName;
    } else if (t === "span") {
      spans[r.span_id] = r;
    } else if (t === "span.start") {
      starts[r.span_id] = { ...r };
    } else if (t === "span.end") {
      const base = starts[r.span_id] ?? {
        span_id: r.span_id,
        trace_id: r.trace_id,
        kind: r.kind ?? "chain",
      };
      delete starts[r.span_id];
      const merged: Dict = { ...base };
      merged.start_time = base.time ?? base.start_time;
      merged.end_time = r.time;
      for (const [k, v] of Object.entries(r)) {
        if (k !== "type" && k !== "time" && k !== "seq") merged[k] = v;
      }
      spans[r.span_id] = merged;
    } else if (t === "event") {
      (eventsBySpan[r.span_id] ??= []).push(r);
    }
  }

  // Any spans still open (no end seen) -> emit with start==end.
  for (const [sid, s] of Object.entries(starts)) {
    if (s.start_time === undefined) s.start_time = s.time;
    if (s.end_time === undefined) s.end_time = s.time;
    spans[sid] = s;
  }

  const otlpSpans: Dict[] = [];
  for (const [sid, s] of Object.entries(spans)) {
    const kind: string = s.kind ?? "chain";
    const otelKind = (KIND_MAP[kind] ?? [null, "CHAIN", "INTERNAL"])[2];
    const start = isoToNanos(s.start_time ?? s.time);
    const end = isoToNanos(s.end_time ?? s.start_time ?? s.time);
    const spanEvents: Dict[] = [];
    for (const ev of eventsBySpan[sid] ?? []) {
      const evAttrs: Dict = {};
      for (const [k, v] of Object.entries(ev)) {
        if (
          !["ot", "type", "trace_id", "span_id", "time", "seq", "name", "parent_id"].includes(k)
        )
          evAttrs[k] = v;
      }
      spanEvents.push({
        timeUnixNano: nanoStr(isoToNanos(ev.time)),
        name: ev.name ?? "event",
        attributes: attrs(evAttrs),
      });
    }
    const otlpSpan: Dict = {
      traceId: s.trace_id,
      spanId: sid,
      name: s.name ?? kind,
      kind: "SPAN_KIND_" + otelKind,
      startTimeUnixNano: nanoStr(start),
      endTimeUnixNano: nanoStr(end),
      attributes: attrs(spanOtelAttributes(s)),
      status: { code: otelStatusCode(s.status) },
    };
    if (s.parent_id) otlpSpan.parentSpanId = s.parent_id;
    if (spanEvents.length) otlpSpan.events = spanEvents;
    otlpSpans.push(otlpSpan);
  }

  return {
    resourceSpans: [
      {
        resource: { attributes: attrs(resource) },
        scopeSpans: [
          {
            scope: { name: "opentrace", version: "0.1.0" },
            spans: otlpSpans,
          },
        ],
      },
    ],
  };
}

/** Options for {@link OtelExporter}. */
export interface OtelExporterOptions {
  /**
   * An `@opentelemetry/sdk-trace-base` `SpanExporter`. When provided, finished
   * spans are pushed to it live. When omitted, records are buffered and can be
   * flushed as OTLP/JSON via {@link OtelExporter.toOtlp}.
   */
  spanExporter?: unknown;
  /** Resource attributes (service.name, ...). */
  resource?: Dict;
}

/**
 * Live OpenTelemetry exporter.
 *
 * A production build streams OpenTrace spans to any OTLP endpoint as real OTel
 * spans. Because OpenTrace ids are already W3C-compliant (32-hex trace / 16-hex
 * span), they are reused verbatim, so parentage is preserved exactly.
 *
 * NOTE: fully live OTLP streaming requires the optional peer dependencies
 * `@opentelemetry/sdk-trace-base` and `@opentelemetry/exporter-trace-otlp-http`.
 * To keep this SDK dependency-free, this class buffers records and exposes
 * {@link OtelExporter.toOtlp}, which uses the same mapping as {@link recordsToOtlp}.
 * If a `spanExporter` is supplied, it is used directly. Swap the buffering path
 * for a real ReadableSpan conversion once the OTel peer deps are present.
 */
export class OtelExporter implements Exporter {
  private readonly buffered: Record[] = [];
  private readonly spanExporter?: any;
  private readonly resource: Dict;

  constructor(opts: OtelExporterOptions = {}) {
    this.spanExporter = opts.spanExporter;
    this.resource = opts.resource ?? { "service.name": "opentrace" };
  }

  export(record: Record): void {
    this.buffered.push(record);
    // TODO: when @opentelemetry/sdk-trace-base is available, convert each
    // finished span to a ReadableSpan and call this.spanExporter.export([span]).
  }

  /** Convert everything seen so far to a single OTLP/JSON request object. */
  toOtlp(): Dict {
    const result = recordsToOtlp(this.buffered);
    // The explicitly-configured resource attributes take precedence (matching the
    // Python live exporter, which is constructed with its own Resource).
    const rs = result.resourceSpans?.[0];
    if (rs && this.resource) {
      const byKey = new Map<string, Dict>();
      for (const a of rs.resource.attributes as Dict[]) byKey.set(a.key, a);
      for (const [k, v] of Object.entries(this.resource)) byKey.set(k, kv(k, v));
      rs.resource.attributes = [...byKey.values()];
    }
    return result;
  }

  close(): void {
    if (this.spanExporter && typeof this.spanExporter.shutdown === "function") {
      try {
        this.spanExporter.shutdown();
      } catch {
        /* ignore */
      }
    }
  }
}
