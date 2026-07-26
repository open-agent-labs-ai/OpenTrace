# Pydantic AI -> OpenTrace

Pydantic AI emits **standard OpenTelemetry GenAI spans**, so there are two ways
to get OpenTrace out of it.

## Path 1 (recommended for prod): direct OTLP

Pydantic AI's built-in instrumentation produces `gen_ai.*` spans. Export them
and run the generic ingest adapter.

```python
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings

# Pin the GenAI semantic-convention version so attribute names are stable.
settings = InstrumentationSettings(version=3)   # or the version you validated against
agent = Agent("anthropic:claude-opus-4-8", instrument=settings)

# configure an OTLP exporter (collector or file) via the OpenTelemetry SDK, e.g.
#   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# have the collector write an OTLP/JSON file, then:
```

```bash
python ../opentelemetry-ingest/otlp_to_opentrace.py pydantic-spans.otlp.json \
    -o pydantic.ot.jsonl --validate
```

> **Pin the version.** `InstrumentationSettings(version=...)` fixes which GenAI
> convention Pydantic AI emits. The ingest adapter keys off `gen_ai.*` /
> `openinference.span.kind`; pinning avoids attribute drift between releases.

## Path 2 (no collector): in-process hook

`opentrace_pydantic_ai.py` writes OpenTrace directly using the SDK. Two
entry points, both feeding a shared recorder.

### 2a. `event_stream_handler`

```python
import opentrace as ot
from opentrace_pydantic_ai import OpenTracePydanticAI, make_event_stream_handler

tracer = ot.Tracer(name="assistant", output="trace.ot.jsonl")
session = OpenTracePydanticAI(tracer, agent_name="Assistant",
                              model="claude-opus-4-8")

result = await agent.run(
    "What's the weather in NYC?",
    event_stream_handler=make_event_stream_handler(session),
)
session.close()
tracer.close()
```

### 2b. `agent.iter()` runner

```python
import opentrace as ot
from opentrace_pydantic_ai import run_traced

tracer = ot.Tracer(name="assistant", output="trace.ot.jsonl")
result = await run_traced(agent, "What's the weather in NYC?", tracer,
                          agent_name="Assistant", model="claude-opus-4-8")
tracer.close()
```

`run_traced` walks the node loop (`UserPromptNode`, `ModelRequestNode`,
`CallToolsNode`, `End`) using `Agent.is_*_node(...)` predicates and streams each
model-request node's events through the recorder.

### Event / node mapping

| Pydantic AI | OpenTrace |
|---|---|
| `ModelRequestNode` (a model call) | `llm` span (model, provider, usage) |
| `PartStartEvent(part=ThinkingPart)` | `reasoning` event |
| `PartStartEvent(part=TextPart)` | assistant `output` message |
| `FunctionToolCallEvent(part=ToolCallPart)` | opens `tool` span (`tool_name`, `args`, `tool_call_id`) |
| `FunctionToolResultEvent(result=ToolReturnPart)` | closes `tool` span (`content` -> result) |
| `UserPromptNode` | `message` event (the prompt) |
| `End` | finalize trace |

Usage is read from the run context (`ctx.usage()` -> `input_tokens`/
`request_tokens`, `output_tokens`/`response_tokens`, `total_tokens`).

## Install

```bash
pip install pydantic-ai
pip install -e /path/to/OpenTrace/sdk/python    # or PYTHONPATH
```

The hook module is **import-safe without pydantic-ai** (types are referenced by
class name). `run_traced` requires pydantic-ai (it uses the node predicates);
the `event_stream_handler` path works purely on duck-typed events.

## Verified

- `python3 -m py_compile` passes.
- With pydantic-ai **absent**, a synthetic event stream (ThinkingPart,
  ToolCallPart with JSON args, ToolReturnPart, TextPart) driven through
  `make_event_stream_handler` produced **11 records, 0 validation errors**, tree
  `turn > agent > {tool(get_weather), llm(250 tok) > reasoning}`.

## Limitations

- The two hook paths are alternatives; don't run both for one call or you get
  duplicate spans.
- `run_traced` depends on the `Agent.is_*_node` predicate API; if a pydantic-ai
  release renames those, the runner falls through (no spans) while the
  `event_stream_handler` path keeps working.
- Cost is not computed here; prefer Path 1 if you want Pydantic AI's own
  `gen_ai.usage.cost` when present.
- Tool-call `args` that arrive as a JSON string are parsed best-effort; invalid
  JSON is stored as the raw string.
