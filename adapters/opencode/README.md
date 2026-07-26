# OpenCode (SST) -> OpenTrace

Two ways to capture [OpenCode](https://github.com/sst/opencode) runs as
OpenTrace v0.1:

| File | Mode | Language | When |
|---|---|---|---|
| `opencode-opentrace-plugin.js` | **in-process** | JS (ESM) | best fidelity; runs inside OpenCode |
| `sse_capture.py` | **out-of-process** | Python | no plugin install; subscribes to the server |

Both write one `.ot.jsonl` file per session and validate against the schema.
Neither needs the OpenTrace SDK at runtime.

---

## A. Plugin (`opencode-opentrace-plugin.js`)

OpenCode loads plugins from `.opencode/plugins/` (project) or
`~/.config/opencode/plugins/` (global). Copy the file there:

```bash
mkdir -p .opencode/plugins
cp opencode-opentrace-plugin.js .opencode/plugins/
# optional: choose where traces land (default: .opencode/opentrace/)
export OPENTRACE_DIR=./traces
```

Run OpenCode as usual. Traces appear at `$OPENTRACE_DIR/<sessionID>.ot.jsonl`.

The plugin exports the OpenCode plugin hooks and writes records with a tiny
inline JSON-lines writer (zero dependencies beyond Node stdlib):

| Hook | OpenTrace effect |
|---|---|
| `event({event})` `session.created` | `trace.start` + root `turn` + main `agent` |
| `tool.execute.before(input, output)` | `tool` span.start (`output.args` -> `arguments`) |
| `tool.execute.after(input, output)` | `tool` span.end (`output.output`, error from `metadata.error`) |
| `event` `message.part.updated` (`reasoning`) | `reasoning` event |
| `event` `message.part.updated` (`step-finish`) | `llm` span with token usage + cost |
| `event` `message.part.updated` (`subtask`) | `handoff` + nested sub-`agent` |
| `event` `session.idle` | close turn + `trace.end` |

`input` for the tool hooks is `{tool, sessionID, callID}`; `output` is `{args}`
(before) / `{title, output, metadata}` (after) per the OpenCode plugin API.

---

## B. SSE capture (`sse_capture.py`)

OpenCode's server publishes a Server-Sent-Events stream at
`GET http://127.0.0.1:4096/event`. This subscribes and converts live, without
touching the OpenCode install.

```bash
opencode serve --port 4096            # terminal 1
python sse_capture.py --port 4096 --out-dir ./traces   # terminal 2
# ...work in OpenCode... Ctrl-C to stop; open sessions are closed cleanly.

export PYTHONPATH=/path/to/OpenTrace/sdk/python
opentrace tree ./traces/<sessionID>.ot.jsonl
```

Consumed events: `session.created`, `session.idle`, `tool.execute.before`,
`tool.execute.after`, and `message.part.updated` (parts `reasoning`,
`step-finish`, `subtask`). Same mapping as the plugin. Pass `--validate` to
validate every written file on exit (needs the SDK on `PYTHONPATH`).

Message parts have a state progression `pending -> running -> completed`
(`toolCallId`, `toolName`, `input`, `output`); this adapter keys tool spans on
`callID`/`toolCallId` and closes them on `tool.execute.after` /
`message.part.updated` completion.

---

## Verified

- `node --check` passes on the plugin (ESM); `python3 -m py_compile` passes on
  `sse_capture.py`.
- Both were driven through a representative event sequence (session.created,
  tool before/after, reasoning part, step-finish with tokens+cost, a subtask,
  session.idle) and each produced **15 records, 0 validation errors** with the
  tree `turn > agent > {reasoning, tool(bash), llm, handoff > reviewer}`.

## Limitations

- Sub-agent (`subtask`) parts don't expose the child's own tool calls on the
  event bus in a linkable way, so sub-agents are rendered as a
  `handoff`+`agent` marker; the child's tools appear under the main agent. If a
  future OpenCode release scopes tool events by sub-session id, key `get()` on
  that id to nest them.
- `step-finish` field names for tokens/cost (`tokens.input`, `tokens.cache.read`,
  `cost`, `provider`/`model`) track OpenCode's message-part schema; unknown
  shapes are dropped rather than guessed. Treat those exact keys as
  best-effort against your OpenCode version.
- The SSE path starts a trace on the first event it sees for a session; if you
  attach mid-session you miss earlier tool calls. The plugin does not have this
  gap.
