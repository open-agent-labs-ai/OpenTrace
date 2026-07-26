# Using OpenTrace with OpenTelemetry

OpenTrace is designed so a trace can be viewed **live** in any OpenTelemetry backend —
Jaeger, Grafana Tempo, Honeycomb, Datadog, Arize Phoenix, Langfuse (OTLP), etc. — with
correct span parentage and the standard `gen_ai.*` semantic conventions.

This works because OpenTrace already uses **W3C Trace Context IDs** (32-hex trace ids,
16-hex span ids), so its ids are valid OTel ids as-is — no remapping, no id collisions.

There are **two ways** to get OpenTrace into OpenTelemetry:

1. **Live export** — the SDK streams spans to an OTLP endpoint as your agent runs.
2. **Offline conversion** — convert an existing `.ot.jsonl` file to OTLP/JSON and POST
   it (no OTel packages required).

---

## 1. Live export from the SDK

Emit to a JSONL file *and* an OTLP collector at the same time:

```python
import opentrace as ot

tracer = ot.Tracer(
    name="my-agent",
    producer={"name": "my-app", "version": "1.0.0"},
    exporter=ot.MultiExporter([
        ot.JsonlExporter("trace.ot.jsonl"),   # durable local record
        ot.OtelExporter(),                     # live spans -> OTLP
    ]),
    resource={"service.name": "my-agent", "service.version": "1.0.0"},
)
```

`ot.OtelExporter()` uses the standard OpenTelemetry environment variables:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # OTLP/HTTP
# export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer <token>"   # for SaaS backends
pip install "opentrace-sdk[otel]"     # installs opentelemetry-sdk + otlp exporter
```

Or pass a specific exporter:

```python
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
tracer = ot.Tracer(..., exporter=ot.OtelExporter(span_exporter=OTLPSpanExporter(endpoint="localhost:4317")))
```

Each OpenTrace span becomes one OTel span; `event` records become span events. Because
the streaming profile writes `span.start` before the work runs and `span.end` after, the
OTel exporter emits the span when it closes, preserving real start/end timestamps.

---

## 2. Offline conversion (no dependencies)

Convert a finished trace file to an OTLP/JSON request body and POST it to any OTLP/HTTP
`/v1/traces` endpoint:

```bash
# print OTLP/JSON
opentrace convert trace.ot.jsonl --to otlp --pretty

# write it to a file
opentrace convert trace.ot.jsonl --to otlp -o trace.otlp.json

# POST straight to a collector
opentrace convert trace.ot.jsonl --to otlp --endpoint http://localhost:4318
```

This path uses only the Python standard library, so it works even without
`opentelemetry-sdk` installed. It's ideal for backfilling historical traces or CI.

---

## 3. Quick start: view live in Jaeger

```bash
# 1. Run Jaeger with the OTLP receiver enabled (UI on :16686, OTLP/HTTP on :4318)
docker run --rm --name jaeger \
  -e COLLECTOR_OTLP_ENABLED=true \
  -p 16686:16686 -p 4318:4318 -p 4317:4317 \
  jaegertracing/all-in-one:latest

# 2a. Live: run your agent with the OTel exporter
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 python3 your_agent.py

# 2b. Or offline: push the demo trace
opentrace convert demo/sample_trace.ot.jsonl --to otlp --endpoint http://localhost:4318

# 3. Open the UI
open http://localhost:16686     # search service "research-assistant" / "my-agent"
```

You'll see the full agent tree — turns, the agent loop, LLM calls, the tool loop, the
sub-agent handoff — as a normal distributed trace.

### Arize Phoenix (LLM-native UI)

```bash
pip install arize-phoenix
python3 -m phoenix.server.main serve        # OTLP on :6006, UI on :6006
opentrace convert demo/sample_trace.ot.jsonl --to otlp --endpoint http://localhost:6006
```

Phoenix understands `openinference.span.kind` (which OpenTrace emits: `LLM`, `TOOL`,
`AGENT`, `RETRIEVER`, `GUARDRAIL`, `EVALUATOR`, …) and renders messages, tokens, and
tool calls natively.

### Grafana Tempo / Honeycomb / Datadog

Point `OTEL_EXPORTER_OTLP_ENDPOINT` (and `OTEL_EXPORTER_OTLP_HEADERS` for auth) at the
vendor's OTLP ingest and use either path above.

---

## 4. What maps to what

OpenTrace preserves everything on the round-trip. Concepts OTel has a slot for use the
standard names; concepts it lacks are preserved under `opentrace.*` so nothing is lost.

| OpenTrace | OpenTelemetry |
|-----------|---------------|
| `trace_id` / `span_id` / `parent_id` | `trace_id` / `span_id` / `parent_span_id` (same bytes) |
| span `kind` | `gen_ai.operation.name` + `openinference.span.kind` + `opentrace.kind` |
| `model` / `provider` | `gen_ai.request.model` / `gen_ai.provider.name` |
| `usage.input_tokens` / `output_tokens` | `gen_ai.usage.input_tokens` / `output_tokens` |
| `usage.cache_read_tokens` / `reasoning_tokens` | `gen_ai.usage.cache_read.input_tokens` / `reasoning.output_tokens` |
| `input.messages` / `output.messages` | `gen_ai.input.messages` / `gen_ai.output.messages` |
| `tool_name` / `tool_call_id` / `arguments` / `result` | `gen_ai.tool.name` / `.call.id` / `.call.arguments` / `.call.result` |
| `agent.id` / `agent.name` | `gen_ai.agent.id` / `gen_ai.agent.name` |
| `handoff` (`from_agent`/`to_agent`/`mode`) | `opentrace.handoff.*` (no native OTel slot) |
| `plan` / `classifier.chosen` / `guardrail.verdict` | `opentrace.plan` / `opentrace.chosen` / `opentrace.verdict` |
| `status` | span `status` (`ok`→OK, `error`/`timeout`→ERROR) |
| `event` records | span `events[]` (or correlated OTLP log records) |
| header `resource` | OTLP `Resource.attributes` |
| `session_id` / `user.id` | `session.id` / `user.id` |

Full mapping tables are in [SPECIFICATION.md §17](../SPECIFICATION.md#17-opentelemetry-interoperability).

---

## 5. Going the other way (OTLP → OpenTrace)

Agents that already emit OTel GenAI spans (Pydantic AI, Dify's OTLP exporter, the
GitHub Copilot CLI, Claude Code's telemetry) can be captured **into** OpenTrace using
the ingest adapter — see [../adapters/opentelemetry-ingest/](../adapters/opentelemetry-ingest/).
This reverses the mapping above so you get the richer OpenTrace structure (typed
handoffs, plans, reasoning) back out of a plain OTLP dump.
