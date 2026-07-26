/**
 * OpenTrace TypeScript SDK — capture and write OpenTrace v0.1 agent traces.
 *
 * See https://openagentlabs.ai/opentrace and the SPECIFICATION.md in this repo.
 */
export {
  SPEC_VERSION,
  SDK_NAME,
  SDK_VERSION,
  REDACTED,
  RecordType,
  SpanKind,
  EventName,
  Outcome,
  StatusCode,
  Profile,
} from "./constants.js";

export {
  Tracer,
  Trace,
  Span,
  currentSpan,
} from "./tracer.js";
export type {
  TracerOptions,
  TraceFinishOptions,
  Producer,
  SpanCallback,
  LlmOptions,
  ToolOptions,
  AgentOptions,
  PlanOptions,
  HandoffOptions,
  SetOutputOptions,
} from "./tracer.js";

export {
  MultiExporter,
  MemoryExporter,
  JsonlExporter,
} from "./exporters.js";
export type { Exporter, Record, JsonlExporterOptions } from "./exporters.js";

export {
  recordsToOtlp,
  OtelExporter,
  spanOtelAttributes,
  otelStatusCode,
  KIND_MAP,
} from "./otel.js";
export type { OtelExporterOptions } from "./otel.js";

export {
  validate,
  validateRecord,
  validateStream,
  ValidationError,
} from "./validate.js";
export type { StreamValidation } from "./validate.js";

export { load, TraceModel, splitTraces } from "./analyze.js";
export type { Rollup } from "./analyze.js";

// Namespaced helpers, so callers can do `import { content as C } from "..."`.
export * as content from "./content.js";
export * as ids from "./ids.js";
export type { Message, Part, Content, Role } from "./content.js";
