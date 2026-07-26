# Dify -> OpenTrace

Two ways to get [Dify](https://github.com/langgenius/dify) runs into OpenTrace.

## Path 1 (recommended): Dify OTLP -> generic ingest

Dify ships a generic OpenTelemetry exporter. Enable it and pipe the OTLP through
[`../opentelemetry-ingest/`](../opentelemetry-ingest/).

In Dify's `.env` (API + worker):

```bash
ENABLE_OTEL=true
OTLP_BASE_ENDPOINT=http://otel-collector:4318      # your OTLP/HTTP endpoint
# (Dify also honors the standard OTEL_EXPORTER_OTLP_* variables)
```

Have your collector write an OTLP/JSON file, then:

```bash
export PYTHONPATH=/path/to/OpenTrace/sdk/python
python ../opentelemetry-ingest/otlp_to_opentrace.py dify-spans.otlp.json \
    -o dify.ot.jsonl --validate
opentrace tree dify.ot.jsonl
```

### Node -> span note

Dify's OTLP spans carry `gen_ai.*` attributes for LLM/tool activity, which the
generic ingest maps automatically. Dify workflow **node** boundaries appear as
`CHAIN`/`INTERNAL` spans and become OpenTrace `chain` spans unless they carry
GenAI operation attributes (LLM nodes -> `llm`, tool nodes -> `tool`). For
richer node typing (retriever/classifier/agent), use Path 2.

## Path 2: node-level trace info -> `dify_ops_to_opentrace.py`

Dify's internal ops-tracing layer builds `*TraceInfo` objects
(`TraceTaskName`: `workflow`, `message`, `tool`, `node_execution`,
`dataset_retrieval`, `moderation`; `WorkflowNodeTraceInfo` with
`node_execution_id` / `node_id` / `node_type`, and token counts on LLM nodes).
If you capture that node-level info as JSON (e.g. via a custom ops trace
provider), this converter renders it with Dify-native structure.

```bash
python dify_ops_to_opentrace.py dify_trace.json -o dify.ot.jsonl --validate
```

### Input shape

A single object (or a list of them). See the module docstring for the full
schema; the essentials:

```json
{
  "trace_task": "workflow",
  "workflow_name": "Support Flow",
  "conversation_id": "conv-1",
  "start_time": "2026-07-25T10:00:00Z",
  "end_time": "2026-07-25T10:00:05Z",
  "node_executions": [
    {"node_type":"knowledge-retrieval","title":"Docs","status":"succeeded",
     "inputs":{"query":"refund policy"},
     "outputs":{"result":[{"document_id":"d1","score":0.91,"content":"..."}]},
     "created_at":"...","finished_at":"..."},
    {"node_type":"llm","title":"Answer","status":"succeeded",
     "execution_metadata":{"model":"gpt-4o","prompt_tokens":250,
       "completion_tokens":70,"total_tokens":320,"total_price":0.0021},
     "created_at":"...","finished_at":"..."}
  ]
}
```

### Mapping

| Dify `trace_task` / `node_type` | OpenTrace |
|---|---|
| `trace_task=workflow` | `workflow` span (root, under a `turn`) |
| `trace_task=dataset_retrieval` | `retriever` span |
| node `llm`, `parameter-extractor` | `llm` span (usage + cost from `execution_metadata`) |
| node `tool`, `http-request` | `tool` span |
| node `knowledge-retrieval` / `dataset-retrieval` | `retriever` span (docs from outputs) |
| node `agent` | `agent` span |
| node `question-classifier` | `classifier` span (`chosen` = class) |
| node `moderation` | `guardrail` span |
| node `code`/`template-transform`/`if-else`/`iteration`/`answer`/... | `chain` span |

Token usage comes from `execution_metadata` (`prompt_tokens`/`completion_tokens`/
`total_tokens`); cost from `total_price`/`currency`.

## Verified

- `python3 -m py_compile` passes.
- A sample workflow trace (start node + knowledge-retrieval + LLM with tokens &
  price + tool) converted to **8 records, 0 validation errors**, tree
  `turn > workflow > {chain(Start), retriever(Docs), llm(320 tok), tool}`, with
  the 320-token / $0.0021 rollup.

## Limitations

- **Best-effort, documented shape.** Dify does not publish a stable JSON export
  for `*TraceInfo`; the input schema here is a reasonable rendering of the
  node-execution fields. Field names (`execution_metadata`, `node_type`,
  `created_at`/`finished_at`) follow Dify's workflow node conventions but may
  need light adjustment for your Dify version. Prefer Path 1 (OTLP) when you
  just need it working.
- Nodes are placed flat under the `workflow` span (Dify's DAG edges are not
  reconstructed into nesting); iteration/parallel structure is not rebuilt.
- `message`-task and `moderation`-task standalone traces are only partially
  handled (workflow and dataset_retrieval tasks are the primary paths).
