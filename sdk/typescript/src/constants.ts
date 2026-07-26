/**
 * OpenTrace constants: spec version and the controlled vocabularies.
 *
 * These mirror the Python SDK's `constants.py` exactly so both SDKs emit the
 * same string literals in the JSONL output.
 */

export const SPEC_VERSION = "0.1";
export const SDK_NAME = "opentrace-ts";
export const SDK_VERSION = "0.1.0";

export const REDACTED = "__REDACTED__";

/** Record envelope `type` values. */
export const RecordType = {
  TRACE_START: "trace.start",
  TRACE_END: "trace.end",
  SPAN_START: "span.start",
  SPAN_END: "span.end",
  SPAN: "span",
  EVENT: "event",
} as const;
export type RecordType = (typeof RecordType)[keyof typeof RecordType];

/** Controlled vocabulary of span `kind` values. */
export const SpanKind = {
  SESSION: "session",
  TURN: "turn",
  AGENT: "agent",
  WORKFLOW: "workflow",
  CHAIN: "chain",
  LLM: "llm",
  TOOL: "tool",
  RETRIEVER: "retriever",
  EMBEDDING: "embedding",
  RERANKER: "reranker",
  CLASSIFIER: "classifier",
  GUARDRAIL: "guardrail",
  EVALUATOR: "evaluator",
  PLAN: "plan",
  HANDOFF: "handoff",
  HARNESS: "harness",
  HUMAN: "human",
  MEMORY: "memory",
  CUSTOM: "custom",
} as const;
export type SpanKind = (typeof SpanKind)[keyof typeof SpanKind];

/** Controlled vocabulary of point-event `name` values. */
export const EventName = {
  MESSAGE: "message",
  REASONING: "reasoning",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  DECISION: "decision",
  PLAN_UPDATE: "plan_update",
  DELTA: "delta",
  STATE: "state",
  ARTIFACT: "artifact",
  SCORE: "score",
  METRIC: "metric",
  INTERRUPT: "interrupt",
  APPROVAL: "approval",
  LOG: "log",
  ERROR: "error",
  CUSTOM: "custom",
} as const;
export type EventName = (typeof EventName)[keyof typeof EventName];

/** Trace outcomes recorded in the footer. */
export const Outcome = {
  SUCCESS: "success",
  FAILURE: "failure",
  PARTIAL: "partial",
  CANCELLED: "cancelled",
  TIMEOUT: "timeout",
} as const;
export type Outcome = (typeof Outcome)[keyof typeof Outcome];

/** Span status codes. */
export const StatusCode = {
  OK: "ok",
  ERROR: "error",
  CANCELLED: "cancelled",
  TIMEOUT: "timeout",
  IN_PROGRESS: "in_progress",
} as const;
export type StatusCode = (typeof StatusCode)[keyof typeof StatusCode];

/** Emission profiles. */
export const Profile = {
  STREAM: "stream",
  BATCH: "batch",
} as const;
export type Profile = (typeof Profile)[keyof typeof Profile];
