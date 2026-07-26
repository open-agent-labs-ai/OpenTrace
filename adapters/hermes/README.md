# Hermes -> OpenTrace (manual instrumentation)

Hermes is a personal assistant harness (messaging, calendar, memory) with **no
public instrumentation API**, so you add tracing by wrapping its loop with the
OpenTrace SDK. `instrument.py` is a self-contained toolkit.

Unlike the [OpenClaw adapter](../openclaw/), which uses a module-global "current
agent", this one passes an explicit `Turn`/`Agent` handle around. That makes it
**safe when Hermes services several channels or turns at once** (Slack + email +
calendar concurrently) — each turn gets its own handle, no shared global.

## What it captures

| Helper | OpenTrace span/event |
|---|---|
| `tracer.session(...)` | `trace.start` / `trace.end` |
| `sess.turn(user_input)` | `turn` + main `agent` |
| `turn.llm(model, ...)` + `.done(...)` | `llm` span (model, input/output, tokens, cost) |
| `turn.tool(name, arguments=...)` + `.result(...)` | `tool` span |
| `turn.trace_tool` (decorator) | `tool` span per decorated call |
| `turn.memory(op, key=...)` | `kind="memory"` span (personal context) |
| `turn.approval(prompt, detail=...)` | `kind="human"` span + boolean `score` |
| `turn.handoff(to_agent)` | `handoff` + nested sub-`agent`; yields a sub-`Agent` |

## Install

```bash
pip install -e /path/to/OpenTrace/sdk/python     # or set PYTHONPATH
cp instrument.py /path/to/your/hermes/
```

## Wrapping the Hermes loop

```python
from instrument import HermesTracer

tracer = HermesTracer("hermes.ot.jsonl", agent_name="Hermes")

def handle_message(channel_id, user_text):
    with tracer.session(session_id=channel_id) as sess:      # one per conversation
        with sess.turn(user_text) as turn:
            # 1. model call
            with turn.llm("anthropic/claude-opus-4-8", input_text=user_text) as call:
                reply = hermes_model(user_text)              # your model call
                call.done(output_text=reply.text,
                          input_tokens=reply.usage.input,
                          output_tokens=reply.usage.output,
                          cost=reply.cost)

            # 2. personal-context access -> kind="memory"
            with turn.memory("read", key="contacts.team", store="local-sqlite"):
                team = hermes_memory.get("contacts.team")

            # 3. human gate BEFORE any outbound side effect -> kind="human"
            draft = "Standup in 10 min!"
            if turn.approval("Send this to #team?", detail=draft):
                with turn.tool("slack_send",
                               arguments={"channel": "#team", "text": draft}) as t:
                    t.result(hermes_send_slack("#team", draft))

            # 4. delegate to a specialized skill -> handoff + sub-agent
            with turn.handoff("scheduler", reason="create calendar event") as sub:
                with sub.tool("create_event", arguments={"title": "Standup"}) as t:
                    t.result(hermes_calendar.create("Standup"))

tracer.close()
```

### Human approval before side effects

A personal assistant sends messages, spends money, and edits your calendar.
Gate every such action with `turn.approval(prompt, detail=...)`. It records a
`kind="human"` span (with the exact `detail` shown to you) and a boolean
`approval` score, so every outbound action has an auditable "human said yes"
record. Pass `approve_fn=` to wire Hermes's own confirmation UI; the default
uses `input()`.

### Capturing memory / personal context

Wrap every read/write/search of personal data (contacts, notes, preferences,
history) in `turn.memory(operation, key=..., store=...)`. These become
`kind="memory"` spans so you can audit precisely what personal data each turn
touched and where it came from.

### Sub-skills via handoff

`turn.handoff("scheduler")` yields a fresh sub-`Agent`; anything you do on it
(tools, llm, memory) nests under a `handoff` -> sub-`agent` in the trace,
matching how Hermes delegates to specialized skills.

## Verified

- `python3 -m py_compile` passes.
- A standalone run (session > turn > llm(940 tok, $0.006) > `memory:read` >
  `human` approval(+score) > `slack_send` tool > handoff > scheduler >
  `create_event`) produced **21 records, 0 validation errors** with exactly that
  tree.

## Limitations / notes

- Explicit-handle design: you must thread the `turn`/`sub` handle to where you
  create spans (that is what makes it concurrency-safe). Don't cache a handle
  past the `with` block that owns it.
- Token/cost are whatever you pass to `.done(...)`; the toolkit does not price
  calls.
- Tool arguments/results are coerced to JSON-safe values and truncated; wrap
  large payloads yourself if you need full fidelity.
