# OpenTrace Viewer

A single-file, zero-dependency, offline trace viewer for OpenTrace `.ot.jsonl` files.
It renders a Jaeger-style **waterfall + span tree** on the left and a full **span
inspector** on the right, with token/cost/duration rollups across the top.

![kinds are color-coded; failed spans are outlined in red]

## Use it

Just open `viewer/index.html` in any browser — no build, no server, no network.

- **Load demo trace** — renders the bundled sample (from `demo/`).
- **Open .ot.jsonl** — pick a file (supports `.jsonl`, `.ot`, `.json`, and `.gz` via
  the browser's `DecompressionStream`).
- **Drag & drop** a trace file anywhere onto the window.
- `?trace=<url>` — auto-load a trace from a URL (same-origin), e.g. serve the repo with
  `python3 -m http.server` and open
  `http://localhost:8000/viewer/index.html?trace=/demo/sample_trace.ot.jsonl`.

## What it shows

- **Waterfall / tree**: every span, indented by parentage, with a time-positioned bar
  colored by `kind`. Collapsible. Sub-agent handoffs and their child spans nest
  naturally. Event markers (reasoning, deltas, plan updates, scores) appear as dots on
  the owning span's row. Spans still open (no `span.end`) render as a hatched bar.
- **Rollup chips**: outcome, span/event counts, total tokens, total cost, wall-clock
  duration, error count.
- **Inspector** (click any span): status, model/provider, tool + `tool_call_id`,
  agent/handoff details, guardrail verdict, classifier choice + confidence, token
  breakdown (input/output/reasoning/cache), cost, structured plan with per-step status,
  input/output **messages** (text, reasoning, `tool_call`, `tool_call_response` parts),
  raw arguments/results/documents/errors, the event list, and the raw span JSON.

It reconstructs spans from **both** emission profiles — streaming (`span.start` +
`span.end`) and batch (compact `span`) — and partitions multi-trace files (a dropdown
appears when a file contains more than one `trace_id`).

## Prefer a real observability backend?

Convert to OTLP and view live in Jaeger / Tempo / Grafana / Arize Phoenix — see
[../docs/opentelemetry.md](../docs/opentelemetry.md). This viewer is for quick, local,
offline inspection with zero setup.
