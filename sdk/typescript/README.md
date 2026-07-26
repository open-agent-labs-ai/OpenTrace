# OpenTrace TypeScript SDK (`@openagentlabs/opentrace`)

Capture and write [OpenTrace](../../SPECIFICATION.md) v0.1 agent traces as JSONL, with
lossless OpenTelemetry export. This is the TypeScript port of the Python SDK and emits
byte-identical record shapes, so the two can be mixed across a distributed system.

## Install

```bash
npm install @openagentlabs/opentrace     # core (zero runtime dependencies)
```

The core SDK has **no runtime dependencies**. Live OTLP streaming can optionally use
`@opentelemetry/sdk-trace-base` (see [OpenTelemetry export](#opentelemetry-export)); the
dependency-free `recordsToOtlp()` converter needs nothing extra.

From this repo:

```bash
cd sdk/typescript
npm install      # dev deps: typescript, @types/node
npm run build    # tsc -> dist/
npm test         # node test/smoke.mjs
```

## Quick start

Because TypeScript has no `with` statement, the primary ergonomic pattern is the
**callback form** — pass an `async (span) => {}` and the span is ended (and any thrown
error recorded) automatically. A manual `start`/`end` form is always available too.

```ts
import { Tracer, content as C } from "@openagentlabs/opentrace";

const tracer = new Tracer({
  name: "my-agent",
  producer: { name: "my-app", version: "1.0.0" },
  output: "trace.ot.jsonl",       // or exporter: new OtelExporter(...) for live OTLP
  environment: "production",
});

const tr = tracer.trace("answer-question", { inputs: { q: "..." }, sessionId: "sess_1" });

await tr.turn({ input: [C.user("What's the weather in Paris?")] }, async (turn) => {
  await turn.agent(
    { id: "main", name: "Assistant", role: "orchestrator", model: "anthropic/claude-opus-4-8" },
    async (ag) => {
      // planning
      const plan = ag.plan({
        goal: "answer weather query",
        steps: [{ id: "s1", description: "call weather tool", status: "pending" }],
      });
      plan.end();

      // an LLM call that decides to call a tool
      await ag.llm(
        { model: "anthropic/claude-opus-4-8", provider: "anthropic",
          input: [C.user("What's the weather in Paris?")] },
        async (call) => {
          call.setOutput({
            messages: [
              C.assistant([C.toolCall("call_1", "get_weather", { city: "Paris" })],
                          { finishReason: "tool_call" }),
            ],
            usage: { input_tokens: 1200, output_tokens: 40 },
            cost: { total: 0.008, currency: "USD" },
          });
        }
      );

      // the tool execution
      await ag.tool("get_weather", { arguments: { city: "Paris" }, toolCallId: "call_1" },
        async (t) => {
          t.setResult({ temp_c: 21, sky: "clear" });
        });

      ag.planUpdate("plan_1", "s1", "done");
    }
  );
});

tr.finish({ outcome: "success", outputs: { answer: "21C and clear in Paris." } });
tracer.close();
```

This produces a valid `trace.ot.jsonl`. Inspect it with the bundled CLI:

```bash
npx opentrace validate trace.ot.jsonl
npx opentrace stats    trace.ot.jsonl
npx opentrace tree     trace.ot.jsonl
```

### snake_case in the JSON

The TS API surface is camelCase (`toolCallId`, `fromAgent`, `finishReason`), but the
emitted JSON uses the OpenTrace/OpenTelemetry snake_case field names (`tool_call_id`,
`from_agent`, `finish_reason`). This is what keeps the records byte-compatible with the
Python SDK. Passthrough `fields` bags accept either casing.

## Core concepts

| Object | Role |
|--------|------|
| `Tracer` | Config + exporter factory. Create once per process. |
| `Trace` | One top-level run. Writes the `trace.start` header on creation and the `trace.end` footer on `finish()`. |
| `Span` | A unit of work (`agent`, `llm`, `tool`, `plan`, `handoff`, …). Emits `span.start`/`span.end` (stream profile) or a compact `span` (batch profile). |
| `content` (`C`) | Helpers to build messages and content parts (`C.user`, `C.assistant`, `C.toolCall`, `C.reasoning`, …). |

### Span factories

Every span exposes typed child factories so you rarely touch raw fields. Each factory
returns a started `Span` for the manual form, or — when you pass a callback as the last
argument — runs the callback and ends the span for you:

```ts
ag.llm({ model, provider, input });                       // kind=llm
ag.tool(name, { arguments, toolCallId });                 // kind=tool
ag.agent({ id, name, role: "subagent" });                 // kind=agent (sub-agent)
ag.plan({ goal, steps });                                 // kind=plan
ag.handoff({ fromAgent, toAgent, mode });                 // kind=handoff
ag.classifier({ candidates, chosen });                    // kind=classifier / router
ag.guardrail({ policy, verdict: "pass" });                // kind=guardrail
ag.retriever({ query, documents });                       // kind=retriever
ag.evaluator({ name, targetSpanId });                     // kind=evaluator
ag.human({ interaction: "approval" });                    // kind=human
ag.harness({ harnessName, role: "meta_harness" });        // kind=harness / meta-harness
ag.span("custom", undefined, { subtype: "dev.myco.thing" }); // any custom kind
```

Point events on any span:

```ts
sp.reasoning("let me think...", { signature: "..." });
sp.decision("branch_a", { options: ["a", "b"], rationale: "..." });
sp.planUpdate("plan_1", "s2", "in_progress");
sp.delta("partial token", "text");            // streaming output chunk
sp.score("helpfulness", 0.9);
sp.artifact({ id: "a1", mime_type: "text/x-diff", name: "patch.diff" });
sp.log("retrying", "warning");
```

Errors are captured automatically when a callback throws, or manually via
`sp.recordError(err)`. The trace footer aggregates counts, tokens, and cost.

## Emission profiles

- **Streaming** (default): `span.start` is written immediately, `span.end` on close.
  Best for live viewing and crash-safety. Emits `delta` events for streamed output.
- **Batch**: one compact `span` record per span, written on close. Smaller files.

```ts
new Tracer({ /* ... */, profile: Profile.BATCH });   // or per-span: ag.tool(name, { profile: "batch" })
```

## OpenTelemetry export

Two paths, both preserving parentage because OpenTrace IDs are already W3C-compliant
(32-hex trace / 16-hex span) and are reused verbatim as OTel IDs.

**Live** — stream spans to any OTLP endpoint as the agent runs:

```ts
import { Tracer, MultiExporter, JsonlExporter, OtelExporter } from "@openagentlabs/opentrace";

const tracer = new Tracer({
  name: "my-agent",
  exporter: new MultiExporter([
    new JsonlExporter("trace.ot.jsonl"),
    new OtelExporter(),   // buffers, or drives a supplied @opentelemetry SpanExporter
  ]),
});
```

`OtelExporter` works with zero dependencies by buffering records and converting them via
the same mapping as `recordsToOtlp()` (`exporter.toOtlp()`). To stream live to a
collector, pass an `@opentelemetry/sdk-trace-base` `SpanExporter` as `spanExporter`.

**Offline** — convert an existing trace file to OTLP/JSON (no dependencies) and POST it:

```bash
npx opentrace convert trace.ot.jsonl --to otlp --endpoint http://localhost:4318
```

Or programmatically:

```ts
import { recordsToOtlp, load } from "@openagentlabs/opentrace";
const otlp = recordsToOtlp(load("trace.ot.jsonl"));   // ExportTraceServiceRequest JSON
```

Spans arrive in Jaeger / Tempo / Phoenix / Honeycomb / etc. with correct parentage.

## Cross-process / sub-agent stitching

```ts
// parent process
const tp = tr.traceparent();                          // "00-<trace_id>-<span_id>-01"
spawn(cmd, args, { env: { ...process.env, TRACEPARENT: tp } });

// child process
tracer.trace("child", { parentTraceparent: process.env.TRACEPARENT });  // reuses trace_id + parent
```

## Programmatic analysis

```ts
import { load, TraceModel } from "@openagentlabs/opentrace";
const model = new TraceModel(load("trace.ot.jsonl"));
console.log(model.rollup());   // tokens, cost, span counts by kind, duration
```

## Validation

```ts
import { validate, validateStream } from "@openagentlabs/opentrace";

const { problems } = validate(records);            // already-parsed record array
const res = validateStream(lines);                 // iterable of raw JSONL lines
```

Structural validation is dependency-free: it enforces the envelope, ID formats,
per-type required fields, and controlled vocabularies (unknown kinds/event names are
warnings unless `strict`).
