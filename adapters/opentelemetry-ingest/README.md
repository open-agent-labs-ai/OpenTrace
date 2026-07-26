# OpenTelemetry GenAI -> OpenTrace ingest

A generic, dependency-free converter that turns any **OTel GenAI OTLP/JSON**
span dump into an OpenTrace v0.1 `.ot.jsonl` stream. It is the exact reverse of
the OpenTrace SDK's `records_to_otlp` (`opentrace/otel_json.py` +
`opentrace/mapping.py`, spec §17).

Use this as the shared back end for any framework that already speaks OTLP GenAI
(GitHub Copilot CLI, Pydantic AI, Dify, LangChain/LlamaIndex via OpenLLMetry,
OpenInference, ...). Those adapters point their OTLP exporter at a file or
collector, then run this converter on the resulting JSON.

## What it captures

Every span in the OTLP document becomes an OpenTrace `span` record, with:

- span kind, name, parent/child hierarchy, start/end time, status
- model, provider, request params (temperature/top_p/top_k/max_tokens/seed)
- token usage and cost
- structured input/output messages
- tool name / call id / type / arguments / result
- agent id/name, handoff fields, classifier & guardrail extras, plan
- span-level OTLP events -> OpenTrace `event` records

A synthetic `trace.start` / `trace.end` pair is generated per distinct `traceId`
(outcome is `failure` if any span carries an error status, else `success`).

## Install / requirements

None. Python 3.8+, standard library only. The OpenTrace SDK is optional and used
only by `--validate`.

```bash
export PYTHONPATH=/path/to/OpenTrace/sdk/python   # only needed for --validate
```

## Usage

```bash
# file in, file out
python otlp_to_opentrace.py spans.otlp.json -o trace.ot.jsonl

# validate the result against the OpenTrace schema (needs the SDK on PYTHONPATH)
python otlp_to_opentrace.py spans.otlp.json --validate

# stdin -> stdout
cat spans.otlp.json | python otlp_to_opentrace.py - > trace.ot.jsonl
```

It accepts either a single `ExportTraceServiceRequest` object (`{"resourceSpans":
[...]}`) or a JSON-lines file where each line is one such object (the shape a
file OTLP exporter appends).

## Round-trip verification

This was verified end to end:

```bash
# 1. build a trace with the SDK (llm/tool/agent/plan/handoff/guardrail/retriever)
python make_trace.py                                     # -> trace.ot.jsonl
opentrace validate trace.ot.jsonl                        # 23 records, 0 errors
# 2. forward: OpenTrace -> OTLP
opentrace convert trace.ot.jsonl --to otlp -o trace.otlp.json
# 3. reverse: OTLP -> OpenTrace, and validate
python otlp_to_opentrace.py trace.otlp.json --validate   # 14 records, 0 errors
```

`opentrace tree` on the reversed file reproduces the original hierarchy exactly
(turn > agent > plan/llm/tool/handoff>agent>guardrail/retriever), with the
`reasoning`, `plan_update`, and `score` events and the 1560-token / $0.01
rollups intact.

## Mapping table (reverse of §17)

| OTLP span field / attribute | OpenTrace span field |
|---|---|
| `opentrace.kind` (authoritative) | `kind` |
| `openinference.span.kind` (fallback) | `kind` |
| `gen_ai.operation.name` (last resort) | `kind` |
| `gen_ai.request.model` | `model` |
| `gen_ai.provider.name` | `provider` |
| `gen_ai.request.{temperature,top_p,top_k,max_tokens,seed}` | `request.{...}` |
| `gen_ai.response.id` | `response.id` |
| `gen_ai.response.finish_reasons` | `response.finish_reasons` |
| `gen_ai.usage.input_tokens` | `usage.input_tokens` |
| `gen_ai.usage.output_tokens` | `usage.output_tokens` |
| `gen_ai.usage.total_tokens` | `usage.total_tokens` |
| `gen_ai.usage.cache_read.input_tokens` | `usage.cache_read_tokens` |
| `gen_ai.usage.cache_creation.input_tokens` | `usage.cache_creation_tokens` |
| `gen_ai.usage.reasoning.output_tokens` | `usage.reasoning_tokens` |
| `gen_ai.usage.cost` | `cost.total` |
| `gen_ai.input.messages` | `input.messages` |
| `gen_ai.output.messages` | `output.messages` |
| `gen_ai.tool.name` | `tool_name` |
| `gen_ai.tool.call.id` | `tool_call_id` |
| `gen_ai.tool.type` | `tool_type` |
| `gen_ai.tool.call.arguments` | `arguments` |
| `gen_ai.tool.call.result` | `result` |
| `gen_ai.agent.{id,name}` | `agent.{id,name}` |
| `opentrace.handoff.{from_agent,to_agent,mode,reason}` | `{from_agent,...}` |
| `opentrace.{chosen,confidence,rationale,policy,verdict}` | same keys |
| `opentrace.plan` | `plan` |
| `error.type` | `error.type` |
| OTLP span `events[]` | `event` records |
| OTLP status `STATUS_CODE_OK/ERROR` | `status.code` `ok`/`error` |

### Kind reverse-mapping notes

The forward map is many-to-one (`llm` and `classifier` both emit
`openinference.span.kind=LLM`; `agent` and `handoff` both emit `AGENT`). To make
the reverse deterministic, `opentrace.kind` is always emitted by the SDK and is
authoritative here. When ingesting spans from a **non-OpenTrace** producer that
lacks `opentrace.kind`, we fall back to `openinference.span.kind`, then to
`gen_ai.operation.name`:

- `LLM`->`llm`, `TOOL`->`tool`, `AGENT`->`agent`, `RETRIEVER`->`retriever`,
  `EMBEDDING`->`embedding`, `RERANKER`->`reranker`, `GUARDRAIL`->`guardrail`,
  `EVALUATOR`->`evaluator`, `CHAIN`->`chain`.
- op fallbacks: `chat`/`text_completion`/`generate_content`->`llm`,
  `execute_tool`->`tool`, `invoke_agent`->`agent`,
  `invoke_workflow`->`workflow`, `retrieval`->`retriever`,
  `embeddings`->`embedding`, `plan`->`plan`.

The flat `gen_ai.usage.prompt_tokens` / `completion_tokens` forms some exporters
emit are also accepted.

## Limitations

- **Not a 1:1 inverse of every OpenTrace field.** Fields the forward map drops
  (session_id, turn_id denormalization, per-span metrics, redaction markers)
  are not reconstructable from OTLP and are omitted. The span tree, timing,
  content, usage, cost, tool/agent/handoff/plan data, and events do round-trip.
- Streaming vs compact profile: output is always compact `span` records
  (start/end folded into one), which is lossless for analysis.
- Trace `name` comes from `resource.service.name`; the original trace-level
  `name`/`outputs`/`summary` are not present in OTLP and are synthesized.
- Non-GenAI spans (plain OTel HTTP/db spans) map to `kind=chain` with their raw
  attributes preserved only where they match the table above.
