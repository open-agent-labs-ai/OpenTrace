/**
 * W3C Trace Context compatible identifier generation for OpenTrace.
 *
 *   trace_id : 16 bytes -> 32 lowercase hex chars
 *   span_id  :  8 bytes -> 16 lowercase hex chars
 *
 * These are byte-for-byte compatible with OpenTelemetry, so OpenTrace IDs can be
 * handed straight to an OTLP exporter.
 */
import { randomBytes } from "node:crypto";

const TRACE_RE = /^[0-9a-f]{32}$/;
const SPAN_RE = /^[0-9a-f]{16}$/;
const ZERO_TRACE = "0".repeat(32);
const ZERO_SPAN = "0".repeat(16);

/** Return a random, non-zero 32-hex trace id. */
export function newTraceId(): string {
  for (;;) {
    const tid = randomBytes(16).toString("hex");
    if (tid !== ZERO_TRACE) return tid;
  }
}

/** Return a random, non-zero 16-hex span id. */
export function newSpanId(): string {
  for (;;) {
    const sid = randomBytes(8).toString("hex");
    if (sid !== ZERO_SPAN) return sid;
  }
}

export function isValidTraceId(value: string): boolean {
  return TRACE_RE.test(value) && value !== ZERO_TRACE;
}

export function isValidSpanId(value: string): boolean {
  return SPAN_RE.test(value) && value !== ZERO_SPAN;
}

/** Build a W3C `traceparent` header for cross-process propagation. */
export function formatTraceparent(
  traceId: string,
  spanId: string,
  sampled = true
): string {
  const flags = sampled ? "01" : "00";
  return `00-${traceId}-${spanId}-${flags}`;
}

export interface ParsedTraceparent {
  traceId: string;
  parentId: string;
  sampled: boolean;
}

/**
 * Parse a `traceparent` header. Returns the parsed parts or `null` when the
 * header is malformed.
 */
export function parseTraceparent(header: string): ParsedTraceparent | null {
  const parts = header.trim().split("-");
  if (parts.length !== 4) return null;
  const [, traceId, parentId, flags] = parts;
  if (!isValidTraceId(traceId) || !isValidSpanId(parentId)) return null;
  const sampled = (parseInt(flags, 16) & 0x01) !== 0;
  return { traceId, parentId, sampled };
}
