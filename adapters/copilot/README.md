# GitHub Copilot -> OpenTrace

GitHub Copilot (the `copilot` CLI and VS Code's Copilot Chat) can export
**OpenTelemetry GenAI** spans. Because those are standard `gen_ai.*` spans, the
OpenTrace adapter simply **ingests Copilot's OTLP output** and reuses the
generic reverse-mapping converter in
[`../opentelemetry-ingest/`](../opentelemetry-ingest/). `otlp_to_opentrace.py`
here is a thin Copilot-flavoured wrapper around it.

> Copilot does not expose a first-party hook API for live capture the way Claude
> Code or OpenCode do, so this is an **offline OTLP -> OpenTrace** path.

## What it captures

Whatever Copilot puts in its GenAI spans: model, token usage, request params,
tool calls, and — when content capture is enabled — the prompt/response
messages. These map to OpenTrace `llm`, `tool`, and `agent`/`chain` spans via
the [generic mapping table](../opentelemetry-ingest/README.md#mapping-table-reverse-of-17).

## Config: enable Copilot's OTLP export

### `copilot` CLI (file exporter — easiest, no collector needed)

```bash
export COPILOT_OTEL_ENABLED=1
export COPILOT_OTEL_EXPORTER_TYPE=file          # write OTLP/JSON-lines to disk
export COPILOT_OTEL_CAPTURE_CONTENT=1           # include prompt/response content
export OTEL_EXPORTER_OTLP_ENDPOINT=./copilot-traces.jsonl   # file path for the file exporter
copilot   # ...run your session...
```

The file exporter appends one OTLP `ExportTraceServiceRequest` JSON object per
flush (JSON-lines). Copilot also keeps session state under `~/.copilot/`
(`session-state/`, and a community-reported `events.jsonl`); the OTLP file is the
supported, structured surface and is what this adapter reads.

### To an OTLP collector instead

```bash
export COPILOT_OTEL_ENABLED=1
export COPILOT_OTEL_EXPORTER_TYPE=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export COPILOT_OTEL_CAPTURE_CONTENT=1
```

Have the collector write an OTLP/JSON file, then feed that file to this adapter.

### VS Code Copilot Chat

Set the OTLP settings in VS Code `settings.json`:

```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporter": "otlp",
  "github.copilot.chat.otel.endpoint": "http://localhost:4318",
  "github.copilot.chat.otel.captureContent": true
}
```

> The exact env var / setting names track Copilot releases; the `COPILOT_OTEL_*`
> and `github.copilot.chat.otel.*` families above are the documented/community
> knobs. Treat specific names as illustrative and confirm against your Copilot
> version — the ingest step is unaffected since it keys off the standard
> `gen_ai.*` attributes.

## Convert to OpenTrace

```bash
export PYTHONPATH=/path/to/OpenTrace/sdk/python   # for --validate + opentrace CLI

python otlp_to_opentrace.py copilot-traces.jsonl -o copilot.ot.jsonl --validate
opentrace tree copilot.ot.jsonl
opentrace stats copilot.ot.jsonl
```

The wrapper accepts either a single OTLP object or Copilot's JSON-lines file and
delegates to the generic converter (verified round-trippable — see the
opentelemetry-ingest README).

## `Agent-Logs-Url` git trailer (coding-agent runs)

When Copilot's coding agent opens a pull request, it appends an
`Agent-Logs-Url:` trailer to the commit/PR body pointing at the hosted session
log. That URL is **not** OTLP — it is a link to GitHub-hosted logs. If you want
those runs in OpenTrace you have two options:

1. Configure the coding agent's environment with the `COPILOT_OTEL_*` variables
   above so it emits OTLP alongside the hosted logs, then ingest the OTLP file.
2. Treat the `Agent-Logs-Url` as an external artifact: attach it to a trace with
   the SDK, e.g. `span.artifact({"type": "url", "uri": "<Agent-Logs-Url>",
   "name": "copilot-agent-logs"})`.

## Limitations

- Offline only — no live streaming of an in-progress Copilot session; you ingest
  after a flush/session end.
- Content is only present if `COPILOT_OTEL_CAPTURE_CONTENT=1` was set; otherwise
  spans carry metrics and tool metadata but no message bodies.
- Copilot's span granularity is whatever Copilot emits; agent/turn structure is
  inferred from `openinference.span.kind` / `gen_ai.operation.name` and parent
  span ids, and may be flatter than a natively instrumented OpenTrace run.
- Env var / setting names may drift across Copilot versions (see note above).
