# Claude Code -> OpenTrace

Two complementary ways to get OpenTrace traces out of [Claude
Code](https://docs.claude.com/en/docs/claude-code):

| File | Mode | When |
|---|---|---|
| `hook_capture.py` | **live** | capture as the session runs, via Claude Code hooks |
| `transcript_to_opentrace.py` | **offline** | convert a finished session `.jsonl` transcript |

Both emit OpenTrace v0.1 `.ot.jsonl` and validate against the schema. Neither
requires the OpenTrace SDK at runtime (standard library only); the SDK is used
only for `opentrace validate/tree/stats`.

There is also Claude Code's built-in OpenTelemetry (`CLAUDE_CODE_ENABLE_TELEMETRY=1`);
if you prefer that path, point its OTLP exporter at a file and run the
[`../opentelemetry-ingest/`](../opentelemetry-ingest/) converter instead. The
two scripts here give richer agent/turn/handoff structure than the raw OTel
metrics.

---

## A. Live capture with hooks (`hook_capture.py`)

Claude Code runs a shell command for each [hook
event](https://docs.claude.com/en/docs/claude-code/hooks), passing a JSON
payload on stdin. This script is stateless between invocations: it appends
records to `~/.claude/opentrace/<session_id>.ot.jsonl` (override with
`OPENTRACE_DIR`) and keeps a `<session_id>.state.json` sidecar so a `PostToolUse`
can close the span its `PreToolUse` opened.

### settings.json

Add to `~/.claude/settings.json` (global) or `.claude/settings.json` (project).
Replace `/ABS/PATH` with the absolute path to this file.

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/hook_capture.py" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/hook_capture.py" }] }],
    "PreToolUse":       [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/hook_capture.py" }] }],
    "PostToolUse":      [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/hook_capture.py" }] }],
    "SubagentStart":    [{ "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/hook_capture.py" }] }],
    "SubagentStop":     [{ "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/hook_capture.py" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/hook_capture.py" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/hook_capture.py" }] }]
  }
}
```

Then just use Claude Code. When a session ends you get:

```bash
export PYTHONPATH=/path/to/OpenTrace/sdk/python
opentrace tree  ~/.claude/opentrace/<session_id>.ot.jsonl
opentrace stats ~/.claude/opentrace/<session_id>.ot.jsonl
```

### Hook stdin fields consumed

`session_id`, `cwd`, `hook_event_name`, `tool_name`, `tool_input`,
`tool_use_id`, `tool_response`, `agent_id`, `agent_type`, `prompt`.

### Event -> OpenTrace mapping

| Hook event | OpenTrace effect |
|---|---|
| `SessionStart` | `trace.start` |
| `UserPromptSubmit` | new `turn` span + `message` event (the prompt) |
| `PreToolUse` | `tool` `span.start` (args from `tool_input`) |
| `PostToolUse` | `tool` `span.end` (result from `tool_response`, error if `is_error`) |
| `SubagentStart` | `handoff` span.start + nested sub-`agent` span.start |
| `SubagentStop` | sub-`agent` span.end + `handoff` span.end |
| `Stop` | close the open agent + turn |
| `SessionEnd` | `trace.end` |

Tool spans that fire while a sub-agent is active nest under that sub-agent
(keyed by `agent_type`).

> The hook never raises into the host session: any error is swallowed and logged
> to stderr, and it always exits 0.

---

## B. Offline transcript conversion (`transcript_to_opentrace.py`)

Claude Code writes a full transcript per session to
`~/.claude/projects/<project-slug>/<session-id>.jsonl`. This converter reads one
and emits OpenTrace with original timestamps preserved.

```bash
export PYTHONPATH=/path/to/OpenTrace/sdk/python
python3 transcript_to_opentrace.py \
    ~/.claude/projects/-repo-slug/<session>.jsonl \
    -o session.ot.jsonl --validate
opentrace tree session.ot.jsonl
```

### Transcript fields consumed

Per line: `type` (`user`/`assistant`/`system`), `uuid`, `parentUuid`,
`timestamp`, `isSidechain`, `parentToolUseId`, `cwd`, and `message.content`
blocks (`text`, `thinking`, `tool_use`, `tool_result`) plus `message.usage`.

### Mapping

| Transcript element | OpenTrace |
|---|---|
| top-level user text | `turn` + `message` event |
| assistant message | `llm` span (model + `usage`), under the main `agent` |
| `thinking` block | `reasoning` event |
| `tool_use` block | `tool` span |
| `tool_result` (later user msg, matched by `tool_use_id`) | tool span `result` / error |
| `Task` tool_use + `isSidechain` entries (`parentToolUseId`) | `handoff` + sub-`agent` |
| `usage.cache_read_input_tokens` etc. | `usage.cache_read_tokens` etc. |

---

## Verified

- `python3 -m py_compile` passes for both scripts.
- Hook path: a simulated 10-event sequence (SessionStart -> UserPromptSubmit ->
  Pre/PostToolUse -> Subagent Start/tool/Stop -> Stop -> SessionEnd) produced 15
  records, **0 validation errors**, and the expected tree (turn > agent >
  Bash tool, handoff > reviewer agent > Read tool).
- Transcript path: an 8-entry sample transcript (thinking, tool_use/result with
  an error, a Task sub-agent sidechain, cache tokens) produced 14 records, **0
  validation errors**, tree `turn > agent > {reasoning, llm, tool(Bash, error),
  handoff > reviewer > {llm, Read tool}}`, 3650 tokens rolled up.

## Limitations

- **Hook granularity:** hooks do not expose per-assistant-message LLM token
  usage, so the live path has `tool`/`agent`/`handoff` spans but no `llm` spans.
  Use the transcript converter (which reads `message.usage`) for token/cost
  rollups, or enable `CLAUDE_CODE_ENABLE_TELEMETRY=1` for OTel usage metrics.
- The hook state sidecar assumes one live session per `session_id`; concurrent
  identical session ids would interleave (not a normal Claude Code condition).
- Sub-agent matching in hooks keys on `agent_type`; if two sub-agents of the
  same type run concurrently they share a bucket. The transcript converter keys
  on `parentToolUseId` and does not have this limitation.
- Cost is not emitted (Claude Code reports tokens, not dollar cost); add a
  price table downstream if needed.
