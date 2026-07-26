# OpenTrace framework adapters

Guides and runnable code for capturing traces from popular agent frameworks and
harnesses as **OpenTrace v0.1** (`.ot.jsonl`). Each adapter lives in its own
directory with a `README.md` and, where the framework exposes an integration
seam, working Python/JS.

The [OpenTrace Python SDK](../sdk/python) (`import opentrace as ot`) and its CLI
(`opentrace validate | stats | tree | convert`) are used throughout. Install it
once:

```bash
pip install -e ../sdk/python           # or: export PYTHONPATH=$PWD/../sdk/python
```

## Adapters

### Coding agents

| Adapter | Seam | Code |
|---|---|---|
| [claude-code](./claude-code/) | Hooks + JSONL transcript | `hook_capture.py` (live), `transcript_to_opentrace.py` (offline) |
| [opencode](./opencode/) | Plugin + server SSE | `opencode-opentrace-plugin.js`, `sse_capture.py` |
| [copilot](./copilot/) | OTLP GenAI export | `otlp_to_opentrace.py` (reuses the generic ingest) |
| [opentelemetry-ingest](./opentelemetry-ingest/) | **any** OTel GenAI OTLP/JSON | `otlp_to_opentrace.py` (generic, shared) |

### Enterprise agents

| Adapter | Seam | Code |
|---|---|---|
| [llamaindex](./llamaindex/) | `instrumentation` dispatcher event/span handlers | `opentrace_llamaindex.py` |
| [pydantic-ai](./pydantic-ai/) | OTLP GenAI, or `event_stream_handler` / `agent.iter()` | `opentrace_pydantic_ai.py` |
| [dify](./dify/) | Generic OTLP, or node-level trace info | `dify_ops_to_opentrace.py` |

### Personal agents (manual instrumentation)

| Adapter | Approach | Code |
|---|---|---|
| [openclaw](./openclaw/) | Wrap the loop; global "current agent" | `instrument.py` (`@trace_tool`, `traced_llm_call`, `traced_memory`, `human_approval`) |
| [hermes](./hermes/) | Wrap the loop; explicit handle (concurrency-safe) | `instrument.py` (`turn.tool/llm/memory/approval/handoff`) |

## Two integration patterns

1. **Live capture** — the framework calls into your code (Claude Code hooks,
   OpenCode plugin/SSE, LlamaIndex handlers, Pydantic AI hooks, manual wrapping).
   These write OpenTrace as the agent runs, with the richest agent/turn/handoff
   structure.
2. **OTLP ingest** — the framework already emits OpenTelemetry GenAI spans
   (Copilot, Pydantic AI, Dify, and anything using OpenLLMetry/OpenInference).
   Export OTLP/JSON and run [`opentelemetry-ingest`](./opentelemetry-ingest/),
   which reverses the OpenTrace<->OTLP mapping (spec §17). Copilot's converter is
   a thin wrapper over it.

## Design conventions shared by the adapters

- Model ids are `provider/model` (e.g. `anthropic/claude-opus-4-8`).
- Cross-process runs use the W3C `traceparent` (`tr.traceparent()` on the parent,
  `tracer.trace(parent_traceparent=...)` on the child).
- Span kinds used: `turn`, `agent`, `llm`, `tool`, `retriever`, `embedding`,
  `plan`, `handoff`, `guardrail`, `classifier`, `memory`, `human`, `chain`,
  `workflow`.
- The pure-Python converters (Claude Code, OpenCode SSE, Copilot, Dify, the
  generic ingest) and the JS plugin emit records **without importing the SDK at
  runtime**, so they run anywhere; the SDK is used only for `opentrace
  validate/stats/tree` and the `--validate` flag.

## Verification status

Every Python file passes `python3 -m py_compile`, and every converter/handler
was exercised against representative input and its output validated with
`opentrace validate` (**0 errors** in all cases). Highlights:

- **Generic OTLP ingest** round-trips: SDK trace -> `opentrace convert --to otlp`
  -> `otlp_to_opentrace.py` -> `opentrace validate` reproduces the original tree,
  tokens, and cost exactly.
- Claude Code (hook sim + transcript sample), OpenCode (plugin driven via Node +
  SSE logic), LlamaIndex, Pydantic AI, Dify, OpenClaw, and Hermes each produced a
  valid, correctly-nested trace from a synthetic run.

See each adapter's README "Verified" section for specifics and "Limitations" for
caveats.
