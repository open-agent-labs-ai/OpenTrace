/**
 * Lightweight, dependency-free OpenTrace validation.
 *
 * A structural validator enforces the core invariants: envelope presence, id
 * formats, per-type required fields, and controlled vocabularies. Mirrors the
 * Python SDK's `validate.py` structural path (the optional jsonschema path is not
 * needed here since this is dependency-free).
 */
import * as ids from "./ids.js";
import { SpanKind, EventName } from "./constants.js";

type Dict = { [key: string]: any };

const SPAN_KINDS = new Set<string>(Object.values(SpanKind));
const EVENT_NAMES = new Set<string>(Object.values(EventName));
const TYPES = new Set([
  "trace.start",
  "trace.end",
  "span.start",
  "span.end",
  "span",
  "event",
]);
const OUTCOMES = new Set(["success", "failure", "partial", "cancelled", "timeout"]);

/** Error thrown for hard validation failures (used by callers that prefer throwing). */
export class ValidationError extends Error {
  readonly line?: number;
  constructor(message: string, line?: number) {
    super((line ? `line ${line}: ` : "") + message);
    this.name = "ValidationError";
    this.line = line;
  }
}

/**
 * Validate one record. Returns a list of problem strings (empty == valid).
 * `strict` treats unknown enum values as errors (default: warnings only).
 */
export function validateRecord(record: Dict, strict = false): string[] {
  const problems: string[] = [];
  const err = (msg: string) => problems.push(msg);

  for (const field of ["ot", "type", "trace_id", "seq", "time"]) {
    if (!(field in record)) err(`missing required envelope field '${field}'`);
  }
  const t = record.type;
  if (t !== undefined && !TYPES.has(t)) err(`unknown record type '${t}'`);
  if ("trace_id" in record && !ids.isValidTraceId(record.trace_id))
    err(`invalid trace_id '${record.trace_id}' (must be 32 lowercase hex, non-zero)`);
  if ("seq" in record && !Number.isInteger(record.seq))
    err("seq must be an integer");

  if (t === "span" || t === "span.start" || t === "span.end" || t === "event") {
    const sid = record.span_id;
    if (sid === undefined) err(`${t} requires span_id`);
    else if (!ids.isValidSpanId(sid))
      err(`invalid span_id '${sid}' (must be 16 lowercase hex, non-zero)`);
  }
  if (record.parent_id !== undefined && record.parent_id !== null) {
    if (!ids.isValidSpanId(record.parent_id))
      err(`invalid parent_id '${record.parent_id}'`);
  }

  if (t === "trace.start") {
    if (!("name" in record)) err("trace.start requires 'name'");
    if (typeof record.producer !== "object" || record.producer === null)
      err("trace.start requires 'producer' object");
  } else if (t === "trace.end") {
    if (!OUTCOMES.has(record.outcome)) err("trace.end requires valid 'outcome'");
  } else if (t === "span" || t === "span.start" || t === "span.end") {
    const kind = record.kind;
    if (kind === undefined) err(`${t} requires 'kind'`);
    else if (!SPAN_KINDS.has(kind)) {
      if (strict) err(`unknown span kind '${kind}'`);
      else problems.push(`warning: unknown span kind '${kind}'`);
    }
  } else if (t === "event") {
    const name = record.name;
    if (name === undefined) err("event requires 'name'");
    else if (!EVENT_NAMES.has(name) && strict) err(`unknown event name '${name}'`);
  }

  return problems;
}

export interface StreamValidation {
  count: number;
  problems: string[];
}

/** Validate an already-parsed array of records. Returns count and problems. */
export function validate(records: Dict[], strict = false): StreamValidation {
  const problems: string[] = [];
  let count = 0;
  let seenHeader = false;
  records.forEach((rec, i) => {
    count += 1;
    if (i === 0 && rec.type !== "trace.start")
      problems.push("line 1: first record should be trace.start");
    if (rec.type === "trace.start") seenHeader = true;
    for (const p of validateRecord(rec, strict)) {
      problems.push(`line ${i + 1}: ${p}`);
    }
  });
  if (!seenHeader) problems.push("stream has no trace.start header");
  return { count, problems };
}

/** Validate an iterable of raw JSONL lines. Returns count and problems. */
export function validateStream(
  lines: Iterable<string>,
  strict = false
): StreamValidation {
  const problems: string[] = [];
  let count = 0;
  let seenHeader = false;
  let i = 0;
  for (const raw of lines) {
    i += 1;
    const line = raw.trim();
    if (!line) continue;
    let rec: Dict;
    try {
      rec = JSON.parse(line);
    } catch (e) {
      problems.push(`line ${i}: invalid JSON: ${(e as Error).message}`);
      continue;
    }
    count += 1;
    if (i === 1 && rec.type !== "trace.start")
      problems.push("line 1: first record should be trace.start");
    if (rec.type === "trace.start") seenHeader = true;
    for (const p of validateRecord(rec, strict)) {
      problems.push(`line ${i}: ${p}`);
    }
  }
  if (!seenHeader) problems.push("stream has no trace.start header");
  return { count, problems };
}
