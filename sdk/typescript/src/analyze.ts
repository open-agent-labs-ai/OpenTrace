/**
 * In-memory analysis of an OpenTrace stream: reconstruct spans and compute
 * rollups. Mirrors the Python SDK's `analyze.py`, and backs the CLI's `stats`
 * and `tree` commands.
 */
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

type Dict = { [key: string]: any };

/** Load all records from a .ot.jsonl(.gz) file. */
export function load(path: string): Dict[] {
  const raw = readFileSync(path);
  const text = path.endsWith(".gz")
    ? gunzipSync(raw).toString("utf-8")
    : raw.toString("utf-8");
  const records: Dict[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (trimmed) records.push(JSON.parse(trimmed));
  }
  return records;
}

function nanos(ts?: string | null): number {
  if (!ts) return 0;
  const ms = Date.parse(ts);
  return Number.isNaN(ms) ? 0 : Math.trunc(ms * 1e6);
}

function normSpan(s: Dict): Dict {
  if (s.kind === undefined) s.kind = "chain";
  if (s.start_time === undefined) s.start_time = s.time;
  return s;
}

export interface Rollup {
  trace_id: string | null;
  name: string | null;
  outcome: string;
  spans: number;
  events: number;
  by_kind: { [kind: string]: number };
  tokens: { input_tokens: number; output_tokens: number; total_tokens: number };
  cost: number;
  errors: number;
  duration_ms: number | null;
}

/** Reconstructed trace: header, footer, span tree, events. */
export class TraceModel {
  header: Dict | null = null;
  footer: Dict | null = null;
  spans: { [id: string]: Dict } = {};
  events: Dict[] = [];
  traceId: string | null = null;
  eventsBySpan: { [id: string]: Dict[] } = {};

  constructor(records: Dict[]) {
    const starts: { [id: string]: Dict } = {};
    for (const r of records) {
      const t = r.type;
      this.traceId = this.traceId ?? r.trace_id ?? null;
      if (t === "trace.start") this.header = r;
      else if (t === "trace.end") this.footer = r;
      else if (t === "span") this.spans[r.span_id] = normSpan(r);
      else if (t === "span.start") starts[r.span_id] = { ...r };
      else if (t === "span.end") {
        const base = starts[r.span_id] ?? { span_id: r.span_id, kind: r.kind };
        delete starts[r.span_id];
        const merged: Dict = { ...base };
        merged.start_time = base.time ?? base.start_time;
        merged.end_time = r.time;
        for (const [k, v] of Object.entries(r)) {
          if (k !== "type" && k !== "time" && k !== "seq") merged[k] = v;
        }
        this.spans[r.span_id] = normSpan(merged);
      } else if (t === "event") this.events.push(r);
    }
    for (const [sid, s] of Object.entries(starts)) {
      s.start_time = s.time;
      s.open = true;
      this.spans[sid] = normSpan(s);
    }
    for (const s of Object.values(this.spans)) s.children = [];
    for (const [sid, s] of Object.entries(this.spans)) {
      const p = s.parent_id;
      if (p && this.spans[p]) this.spans[p].children.push(sid);
    }
    for (const ev of this.events) {
      (this.eventsBySpan[ev.span_id] ??= []).push(ev);
    }
  }

  roots(): string[] {
    return Object.entries(this.spans)
      .filter(([, s]) => !s.parent_id || !this.spans[s.parent_id])
      .map(([sid]) => sid);
  }

  rollup(): Rollup {
    const byKind: { [k: string]: number } = {};
    const tokens = { input_tokens: 0, output_tokens: 0, total_tokens: 0 };
    let cost = 0;
    let errors = 0;
    for (const s of Object.values(this.spans)) {
      const k = s.kind ?? "?";
      byKind[k] = (byKind[k] ?? 0) + 1;
      const u = s.usage ?? {};
      for (const key of ["input_tokens", "output_tokens", "total_tokens"] as const) {
        if (typeof u[key] === "number") tokens[key] += u[key];
      }
      const c = s.cost ?? {};
      if (typeof c.total === "number") cost += c.total;
      if ((s.status ?? {}).code === "error") errors += 1;
    }
    if (!tokens.total_tokens)
      tokens.total_tokens = tokens.input_tokens + tokens.output_tokens;
    const start = this.header ? nanos(this.header.time) : 0;
    const end = this.footer ? nanos(this.footer.time) : 0;
    return {
      trace_id: this.traceId,
      name: this.header ? this.header.name : null,
      outcome: this.footer ? this.footer.outcome : "incomplete",
      spans: Object.keys(this.spans).length,
      events: this.events.length,
      by_kind: byKind,
      tokens,
      cost: Math.round(cost * 1e6) / 1e6,
      errors,
      duration_ms: start && end ? Math.round(((end - start) / 1e6) * 1000) / 1000 : null,
    };
  }
}

/** Partition a multi-trace file into per-trace record lists. */
export function splitTraces(records: Dict[]): Dict[][] {
  const buckets: { [id: string]: Dict[] } = {};
  const order: string[] = [];
  for (const r of records) {
    const tid = r.trace_id;
    if (!buckets[tid]) {
      buckets[tid] = [];
      order.push(tid);
    }
    buckets[tid].push(r);
  }
  return order.map((t) => buckets[t]);
}
