<div align="center">

# OpenTrace

**An open, vendor-neutral standard for tracing everything an AI agent does.**

Multi-turn conversations · agentic loops · tool calls · planning · reasoning ·
sub-agent handoffs · classifiers & routers · guardrails · evaluators ·
human-in-the-loop · harness & meta-harness orchestration.

By [Open Agent Labs](https://openagentlabs.ai) · [openagentlabs.ai/opentrace](https://openagentlabs.ai/opentrace)

</div>

---

OpenTrace is a **JSON Lines** format (one record per line, append-only, streamable) plus
SDKs, a viewer, and framework adapters. It records the *complete runtime behavior* of an
agent system — agentic **and** non-agentic workflows — for coding agents, general
assistants, personal agents, and enterprise agents alike. It is
**OpenTelemetry-compatible on consumption**, so traces render live in Jaeger, Tempo,
Phoenix, Datadog, and friends.

```jsonl
{"ot":"0.1","type":"trace.start","trace_id":"a1b2…8f90","seq":0,"time":"…","name":"fix-failing-test","producer":{"name":"claude-code","version":"2.0"},"agent":{"id":"main","name":"Coder","role":"orchestrator"}}
{"ot":"0.1","type":"span","trace_id":"a1b2…8f90","span_id":"4444…","kind":"llm","name":"chat","model":"anthropic/claude-opus-4-8","output":{"messages":[{"role":"assistant","finish_reason":"tool_call","parts":[{"type":"reasoning","text":"Run the test first."},{"type":"tool_call","tool_call_id":"call_1","name":"run_bash","arguments":{"command":"pytest"}}]}]},"usage":{"input_tokens":1500,"output_tokens":60,"reasoning_tokens":40}}
{"ot":"0.1","type":"span","trace_id":"a1b2…8f90","span_id":"5555…","kind":"tool","tool_call_id":"call_1","tool_name":"run_bash","arguments":{"command":"pytest"},"result":{"exit_code":1}}
{"ot":"0.1","type":"span","trace_id":"a1b2…8f90","span_id":"7777…","kind":"handoff","from_agent":"main","to_agent":"code_reviewer","mode":"delegate"}
{"ot":"0.1","type":"trace.end","trace_id":"a1b2…8f90","seq":17,"outcome":"success","metrics":{"turns":1,"llm_calls":2,"tool_calls":2,"handoffs":1,"total_tokens":3520}}
```

## Why another format?

| | Cursor `agent-trace` | JayFarei `opentraces` | OTel GenAI / OpenInference | Langfuse | **OpenTrace** |
|---|:---:|:---:|:---:|:---:|:---:|
| Purpose | code attribution | execution | execution | execution | **execution** |
| Physical format | JSON | JSONL | OTLP spans | ingestion API | **JSONL** |
| W3C/OTel ids | ✗ | ✗ | ✓ | ✗ | **✓** |
| Structured messages (not flattened) | n/a | partial | ✗ *(index-flattened, stringified)* | blobs | **✓ native** |
| Typed **tool-call correlation** | ✗ | ✓ | ✓ | ✗ | **✓** |
| First-class **agent handoffs** | ✗ | ref only | ✗ | ✗ | **✓ typed** |
| Structured **plans** | ✗ | ✗ | (op only) | ✗ | **✓** |
| Structured **reasoning** | ✗ | text | token count | ✗ | **✓** |
| **Classifier / router / guardrail / evaluator / human** | ✗ | ✗ | partial | labels | **✓** |
| Streaming + crash-safe | ✗ | ✗ | ✓ | partial | **✓** |
| OTel-compatible on read | ✗ | crosswalk | native | OTLP-in | **✓ lossless** |

OpenTrace takes the best ideas from each and fills the gaps that no existing format
ships. See [SPECIFICATION.md](SPECIFICATION.md) for the full rationale.

## Repository layout

```
SPECIFICATION.md            the normative spec (start here)
schema/                     JSON Schema (Draft 2020-12) for validation
sdk/python/                 opentrace-sdk — Python SDK + `opentrace` CLI
sdk/typescript/             @openagentlabs/opentrace — TypeScript SDK
viewer/                     single-file, offline trace viewer (index.html)
demo/                       runnable demo that emits a rich sample trace
adapters/                   capture guides + code for real agent frameworks
docs/                       OpenTelemetry usage, guides
```

## 60-second tour

```bash
# 1. Generate a rich sample trace (no API keys, no network)
python3 demo/research_agent_demo.py

# 2. Inspect it
sdk/python/... $ pip install -e sdk/python          # installs the `opentrace` CLI
opentrace tree  demo/sample_trace.ot.jsonl          # span tree
opentrace stats demo/sample_trace.ot.jsonl          # tokens / cost / rollups
opentrace validate demo/sample_trace.ot.jsonl       # schema check

# 3. View it visually — just open the file, no server needed
open viewer/index.html        # click "Load demo trace"

# 4. Send it to OpenTelemetry (e.g. Jaeger on :4318)
opentrace convert demo/sample_trace.ot.jsonl --to otlp --endpoint http://localhost:4318
```

## Instrument your own agent (Python)

```python
import opentrace as ot
from opentrace import content as C

tracer = ot.Tracer(name="my-agent", producer={"name": "my-app", "version": "1.0"},
                   output="trace.ot.jsonl", environment="production")

with tracer.trace(name="answer", session_id="s1") as tr:
    with tr.turn(input=[C.user("What's 2+2 and the weather in Paris?")]) as turn:
        with turn.agent(id="main", name="Assistant", role="orchestrator",
                        model="anthropic/claude-opus-4-8") as ag:
            with ag.plan(goal="answer both parts",
                         steps=[{"id":"s1","description":"call tools","status":"pending"}]):
                pass
            with ag.llm(model="anthropic/claude-opus-4-8", input=[C.user("…")]) as call:
                call.set_output([C.assistant([C.tool_call("c1","calc",{"e":"2+2"})],
                                             finish_reason="tool_call")],
                                usage={"input_tokens":1200,"output_tokens":40})
            with ag.tool("calc", arguments={"e":"2+2"}, tool_call_id="c1") as t:
                t.set_result({"value":4})
    tr.finish(outcome="success")
tracer.close()
```

TypeScript: see [`sdk/typescript/`](sdk/typescript/). Both SDKs emit byte-identical
records.

## Capture from existing frameworks

Drop-in guides + code in [`adapters/`](adapters/):

| Category | Frameworks |
|----------|-----------|
| **Coding agents** | [Claude Code](adapters/claude-code/) · [GitHub Copilot](adapters/copilot/) · [OpenCode](adapters/opencode/) |
| **Enterprise agents** | [Dify](adapters/dify/) · [LlamaIndex](adapters/llamaindex/) · [Pydantic AI](adapters/pydantic-ai/) |
| **Personal agents** | [OpenClaw](adapters/openclaw/) · [Hermes Agent](adapters/hermes/) |
| **Anything on OTel** | [OpenTelemetry ingest](adapters/opentelemetry-ingest/) |

Each captures via the framework's real seams (Claude Code hooks, LlamaIndex
instrumentation dispatcher, OpenCode plugin/SSE, Pydantic AI event handlers, Dify OTLP,
Copilot OTLP, …) and writes OpenTrace.

## OpenTelemetry

OpenTrace ids are W3C-compliant, so every trace is losslessly convertible to OTLP and
viewable live in any OTel backend. Full guide: [docs/opentelemetry.md](docs/opentelemetry.md).

## Spec at a glance

- **3 layers**: Trace (`trace.start`…`trace.end`) → Spans (tree via `parent_id`) →
  Events (point-in-time).
- **6 record types**: `trace.start`, `trace.end`, `span.start`, `span.end`, `span`,
  `event`.
- **19 span kinds**: `session, turn, agent, workflow, chain, llm, tool, retriever,
  embedding, reranker, classifier, guardrail, evaluator, plan, handoff, harness, human,
  memory, custom`.
- **16 event names**: `message, reasoning, tool_call, tool_result, decision,
  plan_update, delta, state, artifact, score, metric, interrupt, approval, log, error,
  custom`.
- **2 emission profiles**: streaming (crash-safe, realtime) and batch (compact).
- **Messages** use the OTel GenAI structured part model natively (no flattening).
- **Redaction** is first-class (`__REDACTED__` sentinel, capture levels).

Read the whole thing: **[SPECIFICATION.md](SPECIFICATION.md)**.

## License

Specification: CC BY 4.0. Code (SDKs, viewer, adapters): Apache-2.0. © Open Agent Labs.
