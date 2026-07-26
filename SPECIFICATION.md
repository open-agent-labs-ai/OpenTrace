# OpenTrace Specification

**Version:** 0.1.0 (Draft for review)
**Status:** Working Draft
**Date:** 2026-06-21
**Editor:** Open Agent Labs
**Canonical URL:** https://openagentlabs.ai/opentrace
**Media type:** `application/x-opentrace+jsonl`
**File extension:** `.ot.jsonl` (optionally `.ot.jsonl.gz`)

> OpenTrace is an open, vendor-neutral specification for recording everything an AI
> agent system does at runtime: multi-turn conversations, LLM inference calls,
> agentic loops, tool/function calls, planning, reasoning, sub-agent handoffs,
> guardrails, classifiers/routers, evaluators, human-in-the-loop steps, and the
> activity of the harness and meta-harness that orchestrates them.
>
> The "AgentTrace" name is used informally to refer to this same format; the
> normative name of the specification is **OpenTrace**.

---

## Table of Contents

1. [Goals and Non-Goals](#1-goals-and-non-goals)
2. [Conformance and Terminology](#2-conformance-and-terminology)
3. [Data Model Overview](#3-data-model-overview)
4. [Physical Format (JSONL)](#4-physical-format-jsonl)
5. [Identifiers and the Trace Graph](#5-identifiers-and-the-trace-graph)
6. [The Record Envelope](#6-the-record-envelope)
7. [Record Types](#7-record-types)
8. [Span Kinds](#8-span-kinds)
9. [Event Names](#9-event-names)
10. [Shared Value Objects](#10-shared-value-objects)
11. [Messages and Content Parts](#11-messages-and-content-parts)
12. [Usage, Cost, and Metrics](#12-usage-cost-and-metrics)
13. [Errors and Status](#13-errors-and-status)
14. [Privacy, Redaction, and Content Capture](#14-privacy-redaction-and-content-capture)
15. [Extensibility](#15-extensibility)
16. [Streaming vs. Batch Profiles](#16-streaming-vs-batch-profiles)
17. [OpenTelemetry Interoperability](#17-opentelemetry-interoperability)
18. [Crosswalks to Other Formats](#18-crosswalks-to-other-formats)
19. [Versioning](#19-versioning)
20. [Appendix A: Complete Field Reference](#appendix-a-complete-field-reference)
21. [Appendix B: Worked Examples](#appendix-b-worked-examples)

---

## 1. Goals and Non-Goals

### 1.1 Goals

OpenTrace aims to be the **single, complete, replayable record of agent execution**.

1. **Total capture.** Record *every* action taken by an LLM, the harness that calls
   it, the meta-harness that orchestrates harnesses, and any associated code —
   including calls to auxiliary models (classifiers, routers, rerankers, judges,
   embedding models, safety models).
2. **Workflow-agnostic.** Support both fully *agentic* workflows (autonomous loops,
   planning, dynamic tool selection, multi-agent teams) and *non-agentic* workflows
   (fixed pipelines, single LLM calls, RAG chains, batch jobs).
3. **Domain-agnostic.** One format for coding agents, general assistants, personal
   agents, and enterprise agents.
4. **Faithful topology.** First-class, *typed* representation of the structures that
   existing formats only imply: turns, agent loops, sub-agent handoffs, plans,
   reasoning, decisions/branches, and loops/retries.
5. **Streamable and append-only.** A trace can be written incrementally, line by
   line, and consumed in real time, or persisted and analyzed later.
6. **Self-contained lines.** Each line is independently parseable and carries enough
   context to be understood without reading the whole file.
7. **OpenTelemetry-compatible on consumption.** A trace MUST be losslessly
   convertible to OpenTelemetry (OTLP) traces and logs, so existing observability
   backends (Jaeger, Grafana Tempo, Honeycomb, Arize Phoenix, Datadog, Langfuse)
   can render OpenTrace in real time.
8. **Human-readable.** A trace is plain JSON Lines; a developer can read and `grep` it
   without special tooling.

### 1.2 Non-Goals

- **Not a code-attribution format.** OpenTrace records *what an agent did*, not which
  lines of a repository an AI authored. (That is the problem solved by
  `cursor/agent-trace`, which is complementary, not overlapping.)
- **Not a wire protocol for model APIs.** OpenTrace records calls; it does not define
  how to *make* them.
- **Not a rollout / RL trajectory format.** OpenTrace can be consumed to build
  training data, but its purpose is observability and replay, not preference data
  collection. It imposes no reward/return semantics.
- **Not a storage engine.** OpenTrace defines a file/stream format; databases and
  query layers are out of scope (though a reference analyzer is provided).

---

## 2. Conformance and Terminology

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**,
**SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this
document are to be interpreted as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119)
and [RFC 8174](https://www.rfc-editor.org/rfc/rfc8174).

A **producer** is any software that writes OpenTrace records (an SDK, an adapter, a
harness). A **consumer** is any software that reads them (a viewer, an analyzer, an
OTLP bridge).

- A **conforming producer** MUST emit records that validate against the OpenTrace
  JSON Schema for the declared `spec_version`, and MUST follow the ordering and ID
  rules in §4 and §5.
- A **conforming consumer** MUST accept all REQUIRED fields, MUST ignore unknown
  fields and unknown enum values (treating unknown `type`/`kind`/`name` as their
  generic fallback), and MUST NOT fail on namespaced extension fields (§15).

### 2.1 Key terms

| Term | Meaning |
|------|---------|
| **Trace** | The complete record of one top-level execution (e.g. one agent task or one workflow run), identified by a `trace_id`. Physically, one JSONL stream/file. |
| **Span** | A unit of work with a duration and a position in the trace graph (e.g. an LLM call, a tool execution, an agent loop). Identified by a `span_id`. |
| **Event** | A point-in-time record attached to a span or trace (e.g. a reasoning step, a routing decision, a streamed delta, an error). |
| **Turn** | One round of interaction, typically `user input → agent response`. Modeled as a span of kind `turn`. |
| **Harness** | The code that drives the LLM (constructs prompts, parses tool calls, runs the loop). |
| **Meta-harness** | Code that orchestrates multiple harnesses/agents (e.g. a multi-agent supervisor, a workflow engine). |
| **Handoff** | Transfer of control and/or context from one agent to another. |

---

## 3. Data Model Overview

OpenTrace has **three layers**:

```
Trace ─── identified by trace_id, delimited by header/footer records
  │
  ├── Spans ─── tree of timed units of work (turn, agent, llm, tool, plan, ...)
  │     │        linked by span_id / parent_id
  │     │
  │     └── Events ─── point-in-time records within a span
  │                    (reasoning, decision, delta, error, score, ...)
```

- The **trace** is bracketed by a `trace.start` (header) record and a `trace.end`
  (footer) record.
- **Spans** form a tree via `parent_id`. Every span belongs to exactly one trace.
- **Events** attach to a span (or the trace root) via `span_id` and never have
  children.

This mirrors OpenTelemetry's trace/span model (for interoperability) while adding the
agent-native span *kinds* and event *names* that OTel lacks, and storing message
content as **native nested JSON** rather than the index-flattened, stringified-JSON
attributes that OTel-based formats are forced into.

---

## 4. Physical Format (JSONL)

1. An OpenTrace stream is a sequence of **UTF-8 encoded JSON values, one per line**,
   separated by a single `\n` (U+000A). This is [JSON Lines](https://jsonlines.org/).
2. Each line MUST be a single JSON **object** (the *record*). Lines MUST NOT contain
   unescaped newlines within the object.
3. Lines SHOULD NOT be pretty-printed; each record is one physical line.
4. A producer MAY emit a blank line; consumers MUST ignore blank lines.
5. A file MAY be gzip-compressed; the recommended extension is `.ot.jsonl.gz`.
6. Records within a stream:
   - The **first** record MUST be a `trace.start` (header).
   - The **last** record SHOULD be a `trace.end` (footer). A trace without a footer
     is **incomplete** but still valid (e.g. a crashed or still-running agent); a
     consumer MUST handle missing footers.
   - All other records are spans (`span.start`/`span.end`/`span`) or events (`event`).
7. **Ordering.** Records SHOULD be emitted in non-decreasing `seq` order (§6).
   Consumers MUST NOT assume strict timestamp ordering (clocks drift; events may be
   flushed out of order) and MUST use `seq` for deterministic ordering and
   `parent_id` for structure.
8. **Multiple traces per file.** A file MAY contain more than one trace (multiple
   `trace.start`…`trace.end` groups concatenated). Consumers MUST partition by
   `trace_id`. This supports log-style aggregation of many short traces.

---

## 5. Identifiers and the Trace Graph

OpenTrace adopts the [W3C Trace Context](https://www.w3.org/TR/trace-context/) ID
primitives verbatim so that IDs are directly usable as OpenTelemetry IDs.

| Field | Format | Notes |
|-------|--------|-------|
| `trace_id` | 16 bytes, **32 lowercase hex** chars | MUST NOT be all zeros. Stable for the life of the trace. |
| `span_id` | 8 bytes, **16 lowercase hex** chars | MUST NOT be all zeros. Unique within the trace. |
| `parent_id` | 16 hex chars, or `null` | The `span_id` of the enclosing span; `null`/absent for trace-root spans. |

Additional correlation identifiers:

- `session_id` (string, OPTIONAL): groups multiple traces belonging to one user
  session / conversation thread that spans multiple top-level runs.
- `turn_id` (string, OPTIONAL): the `span_id` of the enclosing `turn` span, denormalized
  onto child records for fast turn-level filtering.
- `tool_call_id` (string): correlates an LLM's `tool_call` content part with the
  `tool` span (and `tool_result`) that fulfills it. (See §11.)

### 5.1 Cross-process / sub-agent propagation

When control crosses a process or service boundary (e.g. a harness spawns a
sub-agent subprocess, or calls a remote tool service), the producer SHOULD propagate
a W3C `traceparent` header:

```
traceparent: 00-{trace_id}-{parent_span_id}-{trace_flags}
```

The child process MUST reuse the received `trace_id` and set its root span's
`parent_id` to the received `parent_span_id`, so the full distributed agent execution
forms a single trace. `tracestate` MAY carry framework-specific routing keys.

---

## 6. The Record Envelope

Every record (header, footer, span, event) shares a common envelope. Fields below are
the envelope; type-specific fields are layered on top (§7).

| Field | Type | Req. | Description |
|-------|------|:----:|-------------|
| `ot` | string | ✔ | OpenTrace marker + spec version, e.g. `"0.1"`. Present on every line so a single line is self-describing. |
| `type` | string (enum) | ✔ | Record type discriminator: `trace.start`, `trace.end`, `span.start`, `span.end`, `span`, `event`. |
| `trace_id` | string | ✔ | 32-hex trace id (§5). |
| `span_id` | string | cond. | 16-hex span id. REQUIRED on `span.*`/`span` records and on `event` records (the span the event belongs to). Absent on `trace.start`/`trace.end`. |
| `parent_id` | string\|null | – | Parent span id. Present on span records. |
| `seq` | integer | ✔ | Monotonically increasing per-trace sequence number, starting at 0 on the header. Defines deterministic record order. |
| `time` | string | ✔ | RFC 3339 / ISO 8601 timestamp with at least millisecond precision and a timezone offset, e.g. `"2026-06-21T17:04:05.123456Z"`. For `event` this is the event time; for `span.start` the start time; for `span.end` the end time; for compact `span` it is the start time (with `end_time` carrying the end). |
| `session_id` | string | – | See §5. |
| `turn_id` | string | – | See §5. |
| `attributes` | object | – | Reserved bag for namespaced extension attributes (§15). MUST use reverse-DNS or `gen_ai.*`/`otel.*` keys. |

Notes:
- `ot` is the only redundant field across lines; it is intentionally retained so any
  single line can be validated against the right schema version in isolation.
- Producers SHOULD keep `seq` densely numbered and gap-free per trace; consumers
  MUST tolerate gaps (e.g. from sampling/redaction dropping lines).

---

## 7. Record Types

### 7.1 `trace.start` — Header (REQUIRED, first line)

Declares the trace and its global context. Exactly one per trace.

| Field | Type | Req. | Description |
|-------|------|:----:|-------------|
| `ot`, `type`, `trace_id`, `seq`, `time` | | ✔ | Envelope. `type` = `"trace.start"`, `seq` = 0. |
| `name` | string | ✔ | Human-readable name of the run, e.g. `"fix-failing-test"`, `"weekly-report"`. |
| `producer` | object | ✔ | `{ "name", "version", "sdk", "sdk_version" }` — who wrote this trace (e.g. the harness + OpenTrace SDK). |
| `root` | object | – | Top-level entity: `{ "type": "agent"\|"workflow"\|"pipeline", ...agent fields }`. |
| `agent` | object | – | Primary agent descriptor (§10.1) if the root is an agent. |
| `session_id`, `user` | – | – | `user`: `{ "id", "anonymized": bool }` (id SHOULD be hashed/opaque). |
| `environment` | string | – | `"production"`, `"staging"`, `"development"`, `"test"`, etc. |
| `execution_context` | string | – | `"runtime"` (live), `"devtime"` (developer iteration), `"eval"`, `"replay"`, `"shadow"`. |
| `resource` | object | – | OTel-style resource attributes: `service.name`, `service.version`, `host.name`, `os.type`, `process.pid`, `deployment.environment`, etc. Used directly when bridging to OTLP. |
| `system_instructions` | array | – | Initial system prompts as content parts (§11), or `__REDACTED__`. |
| `tool_definitions` | array | – | Catalog of tools available to the agent: `[{ "name", "description", "type", "schema" }]`. |
| `inputs` | object\|array | – | The task/goal/initial input. |
| `tags` | string[] | – | Free-form labels. |
| `metadata` | object | – | Arbitrary producer metadata. |
| `attributes` | object | – | Namespaced extensions (§15). |

### 7.2 `trace.end` — Footer (RECOMMENDED, last line)

Closes the trace and records the final outcome and aggregate metrics.

| Field | Type | Req. | Description |
|-------|------|:----:|-------------|
| `ot`, `type`, `trace_id`, `seq`, `time` | | ✔ | Envelope. `type` = `"trace.end"`. |
| `outcome` | string | ✔ | `"success"`, `"failure"`, `"partial"`, `"cancelled"`, `"timeout"`. |
| `status` | object | – | Status object (§13). |
| `outputs` | object\|array | – | Final result/answer/artifacts of the run. |
| `error` | object | – | Error object (§13) if `outcome` is `failure`. |
| `metrics` | object | – | Aggregate metrics over the whole trace (§12.3): totals for tokens, cost, duration, counts of llm/tool/agent/turn/error, etc. |
| `summary` | string | – | Optional natural-language summary of the run. |

### 7.3 `span.start` / `span.end` — Span pair (streaming profile)

A span emitted incrementally. `span.start` opens it; `span.end` (same `span_id`)
closes it. Use this profile for real-time tracing.

`span.start` carries: envelope + `kind` (§8) + `name` + the span's **input**
(`input`, plus kind-specific request fields) + `tags`/`metadata`.

`span.end` carries: envelope (`time` = end time) + `status` (§13) + the span's
**output** (`output`, plus kind-specific result fields) + `usage`/`cost`/`metrics` +
`error` (if any). `span.end` MUST repeat `span_id` and SHOULD repeat `kind`.

### 7.4 `span` — Compact span (batch profile)

A complete span emitted as a single record after it finishes. Equivalent to a
`span.start`+`span.end` merged. Carries envelope + `kind` + `name` + `start_time`
(== `time`) + `end_time` + `duration_ms` + `input` + `output` + kind-specific fields
+ `status` + `usage`/`cost`/`metrics` + `error`.

> Producers choose **one** profile per span. The streaming profile is RECOMMENDED for
> live agents (enables real-time viewing and capturing crashes mid-span); the compact
> profile is RECOMMENDED for post-hoc export and smaller files. Consumers MUST accept
> both. A consumer reconstructs a span by matching `span.start`/`span.end` on
> `span_id`, or taking a `span` record directly.

### 7.5 `event` — Point event

A point-in-time record (§9). Carries envelope + `name` (event name) + `span_id` (the
owning span) + name-specific payload fields. Events have no duration and no children.

---

## 8. Span Kinds

The `kind` field on span records classifies the unit of work. Consumers MUST treat an
unknown `kind` as `chain` (generic work) and still render it. Each kind defines
additional fields (full schemas in Appendix A); the most important are summarized
here.

| `kind` | Meaning | Key extra fields |
|--------|---------|------------------|
| `session` | A multi-turn conversation/session scope (may also be expressed via `session_id`). | `agent`, turn count |
| `turn` | One interaction round (user input → agent output). | `role`, `index`, `input` (user msg), `output` (agent msg) |
| `agent` | An agent invocation / agentic loop. | `agent` (§10.1), `iterations`, `model`, `instructions_ref` |
| `workflow` | A non-agentic top-level workflow/graph run. | `graph` (`nodes`, `edges`), `workflow_name` |
| `chain` | A generic logical step / pipeline node / function span. | `node_id`, `node_type` |
| `llm` | A single model inference call (generation). | `model`, `provider`, `request`, `input.messages`, `output.messages`, `usage`, `cost`, `response` (§11, §12) |
| `tool` | A tool / function / MCP / API execution. | `tool_call_id`, `tool_name`, `tool_type`, `arguments`, `result` |
| `retriever` | A retrieval / RAG fetch. | `query`, `documents[]` (`id`, `content`, `score`, `metadata`) |
| `embedding` | An embedding model call. | `model`, `input_count`, `dimensions`, `usage` |
| `reranker` | A reranking call. | `query`, `top_k`, `input_documents`, `output_documents` |
| `classifier` | A classifier / **router** / intent / judge model call. | `candidates[]`, `chosen`, `confidence`, `scores`, `rationale` |
| `guardrail` | A safety / policy / moderation check. | `policy`, `verdict` (`pass`\|`block`\|`flag`), `categories[]`, `score` |
| `evaluator` | An evaluation / scoring step (LLM-as-judge or metric). | `criteria`, `scores[]`, `target_span_id` |
| `plan` | A planning step producing or revising a plan. | `plan` (§10.2), `revision` |
| `handoff` | An agent-to-agent control/context transfer. | `from_agent`, `to_agent`, `mode`, `reason`, `payload` |
| `harness` | A scope representing harness/meta-harness orchestration. | `harness_name`, `role` (`harness`\|`meta_harness`) |
| `human` | A human-in-the-loop interaction (approval, input, edit). | `interaction` (`approval`\|`input`\|`edit`\|`review`), `request`, `response`, `actor` |
| `memory` | A memory read/write/search operation. | `operation` (`read`\|`write`\|`search`\|`delete`), `query`, `items[]` |
| `custom` | Any producer-defined span. | `subtype` (namespaced), free `attributes` |

> **Why these kinds.** `plan`, `handoff`, `classifier`/router, `guardrail`,
> `evaluator`, `human`, and the `harness`/`meta_harness` distinction are first-class
> here precisely because OTel GenAI, OpenInference, OpenLLMetry, and Langfuse either
> omit them or bury them in generic spans. This is the core of OpenTrace's added
> fidelity over prior formats.

---

## 9. Event Names

The `name` field on `event` records. Consumers MUST treat unknown names as `log`.

| `name` | Meaning | Key fields |
|--------|---------|-----------|
| `message` | A standalone conversation message not already captured as llm input/output. | `message` (§11) |
| `reasoning` | A reasoning / thinking step (structured, not just a token count). | `content` (parts), `signature`, `redacted`, `effort` |
| `tool_call` | The model's *decision* to call a tool (emitted when distinct from the `tool` span). | `tool_call_id`, `tool_name`, `arguments` |
| `tool_result` | A tool result delivered back to the model. | `tool_call_id`, `result`, `is_error` |
| `decision` | A routing/branch/control-flow decision by the harness or agent. | `options[]`, `chosen`, `rationale`, `branch` |
| `plan_update` | An incremental plan change (step started/completed/added). | `plan_id`, `step_id`, `status`, `delta` |
| `delta` | A streaming partial output chunk (realtime profile). | `channel` (`text`\|`reasoning`\|`tool_args`), `content`, `index` |
| `state` | A state/memory/context snapshot or mutation. | `operation`, `key`, `value`, `scope` |
| `artifact` | A produced artifact (file, image, diff, blob) reference. | `artifact` (`id`, `mime_type`, `name`, `uri`\|`blob`\|`text`, `bytes`) |
| `score` | An evaluation score attached to a span/trace. | `name`, `value`, `data_type`, `comment`, `target_span_id` |
| `metric` | A point metric sample. | `name`, `value`, `unit` |
| `interrupt` | A user/system interrupt, cancellation, or timeout signal. | `reason`, `source` |
| `approval` | A human approval request/response (point form; see also `human` span). | `requested`, `decision`, `actor` |
| `log` | A free-form log/annotation. | `level`, `message`, `data` |
| `error` | A non-fatal error or exception event. | `error` (§13) |
| `custom` | Producer-defined event. | `subtype` (namespaced), free fields |

---

## 10. Shared Value Objects

### 10.1 Agent descriptor (`agent`)

```json
{
  "id": "agent_reviewer",
  "name": "Code Reviewer",
  "version": "1.4.0",
  "description": "Reviews diffs for correctness and security",
  "role": "subagent",
  "model": "anthropic/claude-opus-4-8",
  "framework": "claude-agent-sdk"
}
```

`role` ∈ `orchestrator` | `supervisor` | `worker` | `subagent` | `tool_agent` | `peer`.
`model` uses the **`provider/model`** convention (models.dev style), e.g.
`anthropic/claude-opus-4-8`, `openai/gpt-5`, `meta/llama-4-70b`.

### 10.2 Plan (`plan`)

```json
{
  "id": "plan_1",
  "goal": "Add OAuth login",
  "revision": 2,
  "steps": [
    { "id": "s1", "description": "Add provider config", "status": "done", "depends_on": [] },
    { "id": "s2", "description": "Implement callback route", "status": "in_progress", "depends_on": ["s1"] },
    { "id": "s3", "description": "Write tests", "status": "pending", "depends_on": ["s2"] }
  ]
}
```

Step `status` ∈ `pending` | `in_progress` | `done` | `skipped` | `failed` | `blocked`.

### 10.3 Handoff fields (on a `handoff` span)

```json
{
  "from_agent": "orchestrator",
  "to_agent": "code_reviewer",
  "mode": "delegate",
  "reason": "diff ready for review",
  "payload": { "diff_ref": "artifact_12", "instructions": "focus on auth" },
  "returns": true
}
```

`mode` ∈ `delegate` (caller waits) | `transfer` (control moves, caller exits) |
`spawn` (async/parallel child) | `return` (control returns to caller). `returns`
indicates whether control is expected back.

### 10.4 Tool definition (in header `tool_definitions[]`)

```json
{ "name": "run_bash", "description": "...", "type": "function",
  "schema": { "type": "object", "properties": { "command": { "type": "string" } } } }
```

`type` ∈ `function` | `mcp` | `extension` | `datastore` | `http` | `code` | `computer` | `retrieval` | `agent`.

---

## 11. Messages and Content Parts

OpenTrace stores conversation content as **native nested JSON**, adopting the
OpenTelemetry GenAI structured message-part model directly (so it bridges to OTLP
without transformation) but **without** the index-flattening or stringified-JSON that
the attribute-based OTel formats require.

### 11.1 Message

```json
{
  "role": "assistant",
  "parts": [ /* content parts */ ],
  "finish_reason": "tool_call",
  "name": "optional speaker/tool name"
}
```

`role` ∈ `system` | `user` | `assistant` | `tool`.
`finish_reason` (on output/assistant messages) ∈ `stop` | `length` | `content_filter`
| `tool_call` | `compaction` | `error`.

`input.messages` and `output.messages` on an `llm` span are arrays of Message objects.

### 11.2 Content part `type`s

A superset of the OTel GenAI part types, so any OTel-native part maps 1:1:

| `type` | Fields | Use |
|--------|--------|-----|
| `text` | `text` | Plain text content. |
| `reasoning` | `text`, `signature`, `redacted` | Model thinking / chain-of-thought. |
| `tool_call` | `tool_call_id`, `name`, `arguments` | Model requests a tool (arguments as **native JSON**, not a string). |
| `tool_call_response` | `tool_call_id`, `result`, `is_error` | Result returned to the model. |
| `server_tool_call` | `tool_call_id`, `name`, `arguments` | Provider-executed (server-side) tool call. |
| `server_tool_call_response` | `tool_call_id`, `result` | Provider-side tool result. |
| `blob` | `mime_type`, `data` (base64) or `__REDACTED__` | Inline binary (image/audio). |
| `file` | `mime_type`, `name`, `uri`, `bytes` | File reference. |
| `uri` | `uri`, `mime_type` | External URI reference. |
| `compaction` | `summary`, `replaced_count` | A context-compaction/summary placeholder. |
| `generic` | `content` | Anything not covered above. |

This part vocabulary is intentionally identical to OTel GenAI's
`gen_ai.input.messages` / `gen_ai.output.messages` part types, guaranteeing lossless
round-tripping to OpenTelemetry and to Arize/Datadog/Grafana renderers.

---

## 12. Usage, Cost, and Metrics

### 12.1 Token usage (`usage`)

A flexible map so new token classes don't require a spec bump (Langfuse-style), with
canonical keys aligned to OTel GenAI / OpenInference:

```json
{
  "input_tokens": 1820,
  "output_tokens": 240,
  "total_tokens": 2060,
  "cache_read_tokens": 1500,
  "cache_creation_tokens": 0,
  "reasoning_tokens": 88,
  "audio_tokens": 0
}
```

Producers MAY add provider-specific keys; any key containing `input`/`output` SHOULD
be summable. Canonical names mirror `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`,
`gen_ai.usage.cache_creation.input_tokens`, `gen_ai.usage.reasoning.output_tokens`.

### 12.2 Cost (`cost`)

```json
{ "input": 0.0091, "output": 0.0036, "total": 0.0127, "currency": "USD" }
```

### 12.3 Metrics (`metrics`)

Per-span timing and, on the footer, trace-level aggregates:

```json
{
  "duration_ms": 4210,
  "time_to_first_token_ms": 320,
  "llm_calls": 6, "tool_calls": 9, "agent_invocations": 3,
  "turns": 2, "handoffs": 2, "errors": 1, "retries": 1,
  "total_tokens": 18044, "total_cost": 0.21
}
```

### 12.4 LLM `request` and `response` objects

```json
"request": {
  "temperature": 0.2, "top_p": 1, "top_k": null, "max_tokens": 4096,
  "stop": [], "seed": 42, "stream": true, "reasoning_effort": "high",
  "tool_choice": "auto", "response_format": "text"
},
"response": {
  "id": "msg_01ABC", "model": "anthropic/claude-opus-4-8",
  "finish_reasons": ["tool_call"], "time_to_first_token_ms": 320
}
```

These names mirror `gen_ai.request.*` and `gen_ai.response.*`.

---

## 13. Errors and Status

### 13.1 Status object (`status`)

```json
{ "code": "ok", "message": null }
```

`code` ∈ `ok` | `error` | `cancelled` | `timeout` | `in_progress`. (`in_progress`
appears only transiently in streaming, never on a closed span.)

### 13.2 Error object (`error`)

```json
{
  "type": "RateLimitError",
  "message": "429 Too Many Requests",
  "retryable": true,
  "stack": "__REDACTED__",
  "code": "rate_limit",
  "data": { "provider": "anthropic", "retry_after_ms": 2000 }
}
```

`type` maps to OTel `error.type`; the record additionally bridges to the OTel span
status `STATUS_CODE_ERROR`.

---

## 14. Privacy, Redaction, and Content Capture

Agent traces routinely contain prompts, tool arguments, and outputs that may include
PII or secrets. OpenTrace makes redaction a first-class, *interoperable* concept.

1. **Content capture levels.** A producer declares a level in the header
   `metadata.capture_level`:
   - `none` — no content; only structure, names, counts, usage, timing.
   - `metadata` — names, schemas, sizes, hashes; no values.
   - `redacted` — values present but sensitive fields replaced (default for prod).
   - `full` — everything (default for devtime/eval).
2. **Redaction sentinel.** A redacted value MUST be the exact string `"__REDACTED__"`
   (distinct from `null`/absent, so consumers can tell "hidden" from "not present").
   For redacted binary, use `{ "mime_type": "...", "data": "__REDACTED__" }`.
3. **Hashing.** When a value is removed, producers SHOULD record
   `{ "__redacted__": true, "sha256": "...", "bytes": 1234 }` so identical content can
   be correlated across spans without exposing it.
4. **User identifiers** in `user.id` SHOULD be opaque/hashed with `anonymized: true`.
5. Consumers MUST render `__REDACTED__` as hidden content and MUST NOT error on it.

---

## 15. Extensibility

1. **Namespaced extension fields.** Any record MAY include extra keys under
   `attributes`, keyed by **reverse-DNS** (`ai.openagentlabs.x`, `dev.cursor.y`,
   `com.github.copilot.z`) or by recognized observability prefixes (`gen_ai.*`,
   `otel.*`, `openinference.*`). This prevents collisions and lets vendors add data
   without a spec bump.
2. **Unknown enum values.** New `kind`/`name`/`type` values MAY appear; consumers MUST
   degrade gracefully to the documented fallback (`chain` / `log` / ignore).
3. **`custom` kind/name.** Use `kind: "custom"` (spans) or `name: "custom"` (events)
   with a namespaced `subtype` for entirely new concepts.
4. **No private top-level keys.** Producers MUST NOT add unnamespaced top-level keys
   not defined by this spec; put them under `attributes` or `metadata`.

---

## 16. Streaming vs. Batch Profiles

OpenTrace supports two emission profiles; both validate against the same schema.

| | **Streaming profile** | **Batch profile** |
|---|---|---|
| Spans | `span.start` then later `span.end` | single `span` record |
| When emitted | live, as work happens | after each span completes |
| Partial output | `delta` events | none |
| Best for | real-time viewers, OTLP live bridge, crash capture | post-hoc export, compact files |
| Crash safety | high (start persisted before work) | low (lost if process dies) |

A producer MAY mix profiles (e.g. stream the top-level agent loop but emit compact
spans for fast tool calls). Consumers reconstruct identically from either.

---

## 17. OpenTelemetry Interoperability

OpenTrace is designed so a consumer can convert a trace to **OTLP** (OpenTelemetry
Protocol) traces + logs **losslessly and in real time**, letting any OTel backend
render agent traces live. This section is normative for the OpenTrace→OTLP bridge.

### 17.1 ID and structure mapping

| OpenTrace | OTLP |
|-----------|------|
| `trace_id` (32-hex) | `Span.trace_id` (same bytes) |
| `span_id` (16-hex) | `Span.span_id` (same bytes) |
| `parent_id` | `Span.parent_span_id` |
| `time` / `start_time` | `Span.start_time_unix_nano` |
| `span.end` `time` / `end_time` | `Span.end_time_unix_nano` |
| `name` | `Span.name` |
| header `resource` | OTLP `Resource.attributes` |
| `status` | `Span.status` (`ok`→`STATUS_CODE_OK`, `error`/`timeout`→`STATUS_CODE_ERROR`) |
| `event` records | OTLP **log records** correlated by `trace_id`/`span_id`, *or* `Span.events[]` |

### 17.2 Kind → OTel GenAI operation / OpenInference span kind

| OpenTrace `kind` | `gen_ai.operation.name` | `openinference.span.kind` | OTel span kind |
|------------------|-------------------------|---------------------------|----------------|
| `llm` | `chat` / `text_completion` | `LLM` | CLIENT |
| `tool` | `execute_tool` | `TOOL` | INTERNAL |
| `agent` | `invoke_agent` | `AGENT` | INTERNAL |
| `workflow` | `invoke_workflow` | `CHAIN` | INTERNAL |
| `chain` | — | `CHAIN` | INTERNAL |
| `retriever` | `retrieval` | `RETRIEVER` | INTERNAL |
| `embedding` | `embeddings` | `EMBEDDING` | CLIENT |
| `reranker` | — | `RERANKER` | INTERNAL |
| `classifier` | `chat` | `LLM` (+`opentrace.kind=classifier`) | INTERNAL |
| `guardrail` | — | `GUARDRAIL` | INTERNAL |
| `evaluator` | — | `EVALUATOR` | INTERNAL |
| `plan` | `plan` | `CHAIN` (+`opentrace.kind=plan`) | INTERNAL |
| `handoff` | `invoke_agent` | `AGENT` (+`opentrace.kind=handoff`) | INTERNAL |
| `turn` / `session` | — | `CHAIN` | SERVER/INTERNAL |
| `human` | — | `CHAIN` (+`opentrace.kind=human`) | INTERNAL |

Concepts OTel has no native slot for (`plan`, `handoff`, `classifier`, `human`,
`harness`) are preserved via an `opentrace.kind` attribute so no information is lost on
the round-trip; OTel-aware viewers still render them as spans.

### 17.3 Attribute mapping (selected)

| OpenTrace | OTLP span attribute |
|-----------|---------------------|
| `model` | `gen_ai.request.model` / `gen_ai.response.model` |
| `provider` | `gen_ai.provider.name` |
| `request.*` | `gen_ai.request.*` (temperature, top_p, max_tokens, …) |
| `response.id` | `gen_ai.response.id` |
| `response.finish_reasons` | `gen_ai.response.finish_reasons` |
| `usage.input_tokens` | `gen_ai.usage.input_tokens` |
| `usage.output_tokens` | `gen_ai.usage.output_tokens` |
| `usage.cache_read_tokens` | `gen_ai.usage.cache_read.input_tokens` |
| `usage.reasoning_tokens` | `gen_ai.usage.reasoning.output_tokens` |
| `input.messages` | `gen_ai.input.messages` (structured) |
| `output.messages` | `gen_ai.output.messages` (structured) |
| `tool_name` | `gen_ai.tool.name` |
| `tool_call_id` | `gen_ai.tool.call.id` |
| `arguments` | `gen_ai.tool.call.arguments` |
| `result` | `gen_ai.tool.call.result` |
| `agent.id` / `agent.name` | `gen_ai.agent.id` / `gen_ai.agent.name` |
| `error.type` | `error.type` |
| `session_id` | `session.id` |
| `user.id` | `user.id` |

### 17.4 Real-time bridge

The reference SDK ships an `otel` exporter that, instead of (or in addition to)
writing JSONL, emits spans through the standard OpenTelemetry SDK to any OTLP endpoint
(`OTEL_EXPORTER_OTLP_ENDPOINT`). Because OpenTrace IDs are already W3C-compliant, spans
arrive with correct parentage and can be viewed live in Jaeger/Tempo/Phoenix/etc.
Usage instructions are provided with the SDK.

---

## 18. Crosswalks to Other Formats

OpenTrace defines bidirectional mappings (provided as reference converters):

- **OpenTelemetry GenAI** — §17 (primary, lossless via `opentrace.*` attributes).
- **OpenInference** — span kinds and `llm.*`/`tool.*` attributes (§17.2/17.3).
- **Langfuse** ingestion — `llm`→generation, `tool`/others→observation, header→trace,
  `score` events→scores, `session_id`→session.
- **JayFarei/opentraces** — `steps[]`/`ToolCall`/`Observation`/`TokenUsage` map onto
  OpenTrace spans/events; `source_call_id`↔`tool_call_id`.
- **cursor/agent-trace** — orthogonal (attribution, not execution); an OpenTrace
  consumer MAY *emit* agent-trace records from `artifact`/edit events, but the formats
  do not overlap.

---

## 19. Versioning

- The spec uses **SemVer**. `spec_version` is carried as the `ot` field (major.minor).
- **Additive** changes (new kinds, names, optional fields) bump the **minor** version
  and remain backward-compatible; consumers ignore what they don't understand.
- **Breaking** changes bump the **major** version.
- Producers MUST set `ot` to the spec version they target. Consumers MUST accept any
  `ot` with a major version they support and degrade gracefully on unknown additions.

---

## Appendix A: Complete Field Reference

The normative machine-readable schema is published as JSON Schema at
`schema/opentrace-0.1.schema.json` (Draft 2020-12), with one sub-schema per record
type and per span kind. Producers SHOULD validate output against it in tests; the
reference SDK does so.

(See `schema/` in this repository for the authoritative field-by-field definitions,
including every kind-specific and event-specific payload.)

---

## Appendix B: Worked Examples

### B.1 Minimal single LLM call (non-agentic, batch profile)

```jsonl
{"ot":"0.1","type":"trace.start","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","seq":0,"time":"2026-06-21T17:00:00.000Z","name":"summarize","producer":{"name":"my-app","version":"1.0.0","sdk":"opentrace-py","sdk_version":"0.1.0"},"root":{"type":"pipeline"},"environment":"production"}
{"ot":"0.1","type":"span","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7","parent_id":null,"seq":1,"time":"2026-06-21T17:00:00.010Z","end_time":"2026-06-21T17:00:01.230Z","duration_ms":1220,"kind":"llm","name":"chat anthropic/claude-opus-4-8","model":"anthropic/claude-opus-4-8","provider":"anthropic","request":{"temperature":0.2,"max_tokens":1024,"stream":false},"input":{"messages":[{"role":"user","parts":[{"type":"text","text":"Summarize: ..."}]}]},"output":{"messages":[{"role":"assistant","finish_reason":"stop","parts":[{"type":"text","text":"Summary: ..."}]}]},"usage":{"input_tokens":820,"output_tokens":110,"total_tokens":930},"cost":{"total":0.0061,"currency":"USD"},"response":{"id":"msg_01ABC","finish_reasons":["stop"]},"status":{"code":"ok"}}
{"ot":"0.1","type":"trace.end","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","seq":2,"time":"2026-06-21T17:00:01.240Z","outcome":"success","metrics":{"duration_ms":1240,"llm_calls":1,"total_tokens":930,"total_cost":0.0061}}
```

### B.2 Agentic coding turn with planning, tool loop, sub-agent handoff (streaming profile)

```jsonl
{"ot":"0.1","type":"trace.start","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","seq":0,"time":"2026-06-21T18:00:00.000Z","name":"fix-failing-test","producer":{"name":"claude-code","version":"2.0","sdk":"opentrace-py","sdk_version":"0.1.0"},"root":{"type":"agent"},"agent":{"id":"main","name":"Coder","role":"orchestrator","model":"anthropic/claude-opus-4-8","framework":"claude-agent-sdk"},"session_id":"sess_42","execution_context":"runtime","environment":"development","tool_definitions":[{"name":"run_bash","type":"function"},{"name":"edit_file","type":"function"},{"name":"review_diff","type":"agent"}]}
{"ot":"0.1","type":"span.start","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"1111111111111111","parent_id":null,"seq":1,"time":"2026-06-21T18:00:00.010Z","kind":"turn","name":"turn 1","turn_id":"1111111111111111","input":{"messages":[{"role":"user","parts":[{"type":"text","text":"The auth test is failing, fix it."}]}]}}
{"ot":"0.1","type":"span.start","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"2222222222222222","parent_id":"1111111111111111","seq":2,"time":"2026-06-21T18:00:00.020Z","kind":"agent","name":"Coder loop","turn_id":"1111111111111111","agent":{"id":"main","name":"Coder","role":"orchestrator"}}
{"ot":"0.1","type":"span","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"3333333333333333","parent_id":"2222222222222222","seq":3,"time":"2026-06-21T18:00:00.030Z","end_time":"2026-06-21T18:00:01.500Z","kind":"plan","name":"plan","turn_id":"1111111111111111","plan":{"id":"plan_1","goal":"fix auth test","revision":1,"steps":[{"id":"s1","description":"reproduce failure","status":"pending"},{"id":"s2","description":"patch code","status":"pending"},{"id":"s3","description":"review diff","status":"pending"}]},"status":{"code":"ok"}}
{"ot":"0.1","type":"span","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"4444444444444444","parent_id":"2222222222222222","seq":4,"time":"2026-06-21T18:00:01.510Z","end_time":"2026-06-21T18:00:03.000Z","kind":"llm","name":"chat","model":"anthropic/claude-opus-4-8","provider":"anthropic","output":{"messages":[{"role":"assistant","finish_reason":"tool_call","parts":[{"type":"reasoning","text":"Run the test first.","signature":"__REDACTED__"},{"type":"tool_call","tool_call_id":"call_1","name":"run_bash","arguments":{"command":"pytest tests/test_auth.py"}}]}]},"usage":{"input_tokens":1500,"output_tokens":60,"reasoning_tokens":40},"status":{"code":"ok"}}
{"ot":"0.1","type":"span","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"5555555555555555","parent_id":"2222222222222222","seq":5,"time":"2026-06-21T18:00:03.010Z","end_time":"2026-06-21T18:00:05.400Z","kind":"tool","name":"run_bash","tool_call_id":"call_1","tool_name":"run_bash","tool_type":"function","arguments":{"command":"pytest tests/test_auth.py"},"result":{"exit_code":1,"stdout":"1 failed: token expiry off by 60s"},"status":{"code":"ok"}}
{"ot":"0.1","type":"event","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"2222222222222222","seq":6,"time":"2026-06-21T18:00:05.410Z","name":"plan_update","plan_id":"plan_1","step_id":"s1","status":"done"}
{"ot":"0.1","type":"span","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"6666666666666666","parent_id":"2222222222222222","seq":7,"time":"2026-06-21T18:00:05.420Z","end_time":"2026-06-21T18:00:06.900Z","kind":"tool","name":"edit_file","tool_call_id":"call_2","tool_name":"edit_file","tool_type":"function","arguments":{"path":"auth.py","patch":"- expiry=3600\n+ expiry=3660"},"result":{"ok":true},"status":{"code":"ok"}}
{"ot":"0.1","type":"span.start","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"7777777777777777","parent_id":"2222222222222222","seq":8,"time":"2026-06-21T18:00:06.910Z","kind":"handoff","name":"delegate to reviewer","from_agent":"main","to_agent":"code_reviewer","mode":"delegate","reason":"diff ready","payload":{"path":"auth.py"},"returns":true}
{"ot":"0.1","type":"span.start","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"8888888888888888","parent_id":"7777777777777777","seq":9,"time":"2026-06-21T18:00:06.920Z","kind":"agent","name":"Reviewer","agent":{"id":"code_reviewer","name":"Code Reviewer","role":"subagent","model":"anthropic/claude-sonnet-4-6"}}
{"ot":"0.1","type":"span","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"9999999999999999","parent_id":"8888888888888888","seq":10,"time":"2026-06-21T18:00:06.930Z","end_time":"2026-06-21T18:00:08.100Z","kind":"guardrail","name":"secret-scan","policy":"no-secrets","verdict":"pass","status":{"code":"ok"}}
{"ot":"0.1","type":"span","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"aaaaaaaaaaaaaaaa","parent_id":"8888888888888888","seq":11,"time":"2026-06-21T18:00:08.110Z","end_time":"2026-06-21T18:00:10.000Z","kind":"llm","name":"chat","model":"anthropic/claude-sonnet-4-6","provider":"anthropic","output":{"messages":[{"role":"assistant","finish_reason":"stop","parts":[{"type":"text","text":"LGTM, off-by-60s fix is correct."}]}]},"usage":{"input_tokens":900,"output_tokens":30},"status":{"code":"ok"}}
{"ot":"0.1","type":"event","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"8888888888888888","seq":12,"time":"2026-06-21T18:00:10.010Z","name":"score","score_name":"review_approval","value":1,"data_type":"boolean","target_span_id":"6666666666666666"}
{"ot":"0.1","type":"span.end","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"8888888888888888","seq":13,"time":"2026-06-21T18:00:10.020Z","kind":"agent","status":{"code":"ok"},"usage":{"input_tokens":900,"output_tokens":30}}
{"ot":"0.1","type":"span.end","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"7777777777777777","seq":14,"time":"2026-06-21T18:00:10.030Z","kind":"handoff","status":{"code":"ok"}}
{"ot":"0.1","type":"span.end","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"2222222222222222","seq":15,"time":"2026-06-21T18:00:10.040Z","kind":"agent","status":{"code":"ok"},"metrics":{"llm_calls":2,"tool_calls":2,"handoffs":1}}
{"ot":"0.1","type":"span.end","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","span_id":"1111111111111111","seq":16,"time":"2026-06-21T18:00:10.050Z","kind":"turn","status":{"code":"ok"},"output":{"messages":[{"role":"assistant","parts":[{"type":"text","text":"Fixed the token expiry off-by-60s bug; reviewer approved."}]}]}}
{"ot":"0.1","type":"trace.end","trace_id":"a1b2c3d4e5f60718293a4b5c6d7e8f90","seq":17,"time":"2026-06-21T18:00:10.060Z","outcome":"success","outputs":{"summary":"auth test passing"},"metrics":{"duration_ms":10060,"turns":1,"llm_calls":2,"tool_calls":2,"agent_invocations":2,"handoffs":1,"total_tokens":3520}}
```

---

*End of OpenTrace Specification v0.1.0 (Draft).*
