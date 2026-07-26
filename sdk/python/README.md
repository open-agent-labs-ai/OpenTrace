# OpenTrace Python SDK (`opentrace-sdk`)

Capture and write [OpenTrace](../../SPECIFICATION.md) v0.1 agent traces as JSONL, with
lossless OpenTelemetry export.

## Install

```bash
pip install opentrace-sdk               # core (zero dependencies)
pip install "opentrace-sdk[otel]"       # + live OpenTelemetry/OTLP export
pip install "opentrace-sdk[validate]"   # + jsonschema-backed validation
```

(From this repo: `pip install -e sdk/python`.)

## Quick start

```python
import opentrace as ot
from opentrace import content as C

tracer = ot.Tracer(
    name="my-agent",
    producer={"name": "my-app", "version": "1.0.0"},
    output="trace.ot.jsonl",     # or exporter=ot.OtelExporter(...) for live OTLP
    environment="production",
)

with tracer.trace(name="answer-question", inputs={"q": "..."}, session_id="sess_1") as tr:
    with tr.turn(input=[C.user("What's the weather in Paris?")]) as turn:
        with turn.agent(id="main", name="Assistant", role="orchestrator",
                        model="anthropic/claude-opus-4-8") as ag:

            # planning
            with ag.plan(goal="answer weather query",
                         steps=[{"id": "s1", "description": "call weather tool",
                                 "status": "pending"}]):
                pass

            # an LLM call that decides to call a tool
            with ag.llm(model="anthropic/claude-opus-4-8", provider="anthropic",
                        input=[C.user("What's the weather in Paris?")]) as call:
                call.set_output(
                    [C.assistant([C.tool_call("call_1", "get_weather", {"city": "Paris"})],
                                 finish_reason="tool_call")],
                    usage={"input_tokens": 1200, "output_tokens": 40},
                    cost={"total": 0.008, "currency": "USD"},
                )

            # the tool execution
            with ag.tool("get_weather", arguments={"city": "Paris"},
                         tool_call_id="call_1") as t:
                t.set_result({"temp_c": 21, "sky": "clear"})

            ag.plan_update("plan_1", "s1", "done")

    tr.finish(outcome="success", outputs={"answer": "21°C and clear in Paris."})

tracer.close()
```

This produces a valid `trace.ot.jsonl`. Inspect it:

```bash
opentrace validate trace.ot.jsonl
opentrace stats    trace.ot.jsonl
opentrace tree     trace.ot.jsonl
```

## Core concepts

| Object | Role |
|--------|------|
| `Tracer` | Config + exporter factory. Create once per process. |
| `Trace` | One top-level run. Context manager writes the `trace.start`/`trace.end`. |
| `Span` | A unit of work (`agent`, `llm`, `tool`, `plan`, `handoff`, …). Context manager; emits `span.start`/`span.end` (stream) or a compact `span` (batch). |
| `content` (`C`) | Helpers to build messages and content parts (`C.user`, `C.assistant`, `C.tool_call`, `C.reasoning`, …). |

### Span factories

Every span exposes typed child factories so you rarely touch raw fields:

```python
ag.llm(model=..., provider=..., input=[...])         # kind=llm
ag.tool(name, arguments=..., tool_call_id=...)        # kind=tool
ag.agent(id=..., name=..., role="subagent")           # kind=agent (sub-agent)
ag.plan(goal=..., steps=[...])                        # kind=plan
ag.handoff(from_agent=..., to_agent=..., mode=...)    # kind=handoff
ag.classifier(candidates=..., chosen=..., ...)        # kind=classifier / router
ag.guardrail(policy=..., verdict="pass")              # kind=guardrail
ag.retriever(query=..., documents=[...])              # kind=retriever
ag.evaluator(name=..., target_span_id=...)            # kind=evaluator
ag.human(interaction="approval")                      # kind=human
ag.harness(harness_name=..., role="meta_harness")     # kind=harness / meta-harness
ag.span("custom", subtype="dev.myco.thing")           # any custom kind
```

Point events on any span:

```python
sp.reasoning("let me think...", signature="...")
sp.decision(chosen="branch_a", options=["a", "b"], rationale="...")
sp.plan_update("plan_1", "s2", "in_progress")
sp.delta("partial token", channel="text")   # streaming output chunk
sp.score("helpfulness", 0.9)
sp.artifact({"id": "a1", "mime_type": "text/x-diff", "name": "patch.diff"})
sp.log("retrying", level="warning")
```

Errors are captured automatically when a `with` block raises, or manually via
`sp.record_error(exc)`. The trace footer aggregates counts, tokens, and cost.

## Emission profiles

- **Streaming** (default): `span.start` is written immediately, `span.end` on close.
  Best for live viewing and crash-safety. Emits `delta` events for streamed output.
- **Batch**: one compact `span` record per span, written on close. Smaller files.

```python
ot.Tracer(..., profile=ot.Profile.BATCH)    # or per-span: ag.tool(..., profile="batch")
```

## OpenTelemetry export

See [../../docs/opentelemetry.md](../../docs/opentelemetry.md). Two paths:

**Live** — stream spans to any OTLP endpoint as the agent runs:

```python
tracer = ot.Tracer(
    name="my-agent",
    exporter=ot.MultiExporter([
        ot.JsonlExporter("trace.ot.jsonl"),
        ot.OtelExporter(),   # reads OTEL_EXPORTER_OTLP_ENDPOINT
    ]),
)
```

**Offline** — convert an existing trace file to OTLP/JSON (no dependencies) and POST it:

```bash
opentrace convert trace.ot.jsonl --to otlp --endpoint http://localhost:4318
```

Because OpenTrace IDs are W3C-compliant, spans arrive in Jaeger/Tempo/Phoenix/etc.
with correct parentage.

## Cross-process / sub-agent stitching

```python
# parent process
tp = tr.traceparent()                       # "00-<trace_id>-<span_id>-01"
subprocess.run([...], env={**os.environ, "TRACEPARENT": tp})

# child process
tracer.trace(parent_traceparent=os.environ["TRACEPARENT"])   # reuses trace_id + parent
```

## Programmatic analysis

```python
from opentrace.analyze import load, TraceModel
model = TraceModel(load("trace.ot.jsonl"))
print(model.rollup())          # tokens, cost, span counts by kind, duration
```
