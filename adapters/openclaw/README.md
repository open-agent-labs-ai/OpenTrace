# OpenClaw -> OpenTrace (manual instrumentation)

OpenClaw is a personal-agent harness with **no public instrumentation API**, so
you add tracing by wrapping its main loop with the OpenTrace SDK. `instrument.py`
is a self-contained, drop-in toolkit that makes that a few lines: a session
context manager, a `@trace_tool` decorator, a `traced_llm_call()` context
manager, and helpers for memory and human-approval spans.

Nothing in `instrument.py` imports OpenClaw, so you can test it standalone and
then splice the calls into your real loop.

## What it captures

| Helper | OpenTrace span/event |
|---|---|
| `tracer.session(...)` | `trace.start` / `trace.end` |
| `sess.turn(user_input=...)` | `turn` + main `agent` (records the user message) |
| `@trace_tool` | `tool` span per function call (args in, result out, errors) |
| `traced_llm_call(...)` + `.done(...)` | `llm` span (model, provider, input/output, tokens, cost) |
| `traced_memory(op, key=...)` | `kind="memory"` span for personal-context ops |
| `human_approval(prompt)` | `kind="human"` span + boolean `score` |

## Install

```bash
pip install -e /path/to/OpenTrace/sdk/python     # or set PYTHONPATH
cp instrument.py  /path/to/your/openclaw/         # drop it next to your loop
```

## Wrapping the OpenClaw loop

Find OpenClaw's turn loop (where it takes a user instruction, calls the model,
and dispatches tools) and wrap it:

```python
from instrument import (OpenClawTracer, trace_tool, traced_llm_call,
                        traced_memory, human_approval)

tracer = OpenClawTracer("openclaw.ot.jsonl", agent_name="OpenClaw")

# 1. Decorate the harness's tool functions once, where they are defined.
@trace_tool
def list_downloads(): ...

@trace_tool(name="rm")
def delete_installers(paths): ...

# 2. Wrap the loop.
with tracer.session(session_id=conversation_id) as sess:
    for user_message in openclaw_inputs():                 # your input source
        with sess.turn(user_input=user_message):
            # 3. Wrap each model call.
            with traced_llm_call(model="anthropic/claude-opus-4-8",
                                 input_text=user_message) as call:
                reply = openclaw_call_model(user_message)  # your model call
                call.done(output_text=reply.text,
                          input_tokens=reply.usage.input,
                          output_tokens=reply.usage.output,
                          cost=reply.cost)

            # 4. Tools traced automatically once decorated.
            files = list_downloads()

            # 5. Memory / personal-context ops -> kind="memory".
            with traced_memory("read", key="user.preferences") as mem:
                prefs = openclaw_memory.get("user.preferences")
                mem.done(result=prefs, hit=prefs is not None)

            # 6. Human-in-the-loop gates -> kind="human".
            if human_approval("Delete 12 old installers?"):
                delete_installers(files)

tracer.close()
```

`human_approval` uses `input()` by default; pass `approve_fn=` to hook
OpenClaw's own confirmation UI (return `True`/`False`).

### Capturing memory / personal context

Personal agents constantly read and write user context (preferences, notes,
contacts, history). Wrap each access in `traced_memory(operation, key=..., store=...)`
so you can audit exactly what personal data the agent touched:

```python
with traced_memory("search", key="contacts", store="local-sqlite") as mem:
    hits = openclaw_memory.search("plumber")
    mem.done(result=[h.name for h in hits], hit=bool(hits))
```

`operation` is a free string (`read`/`write`/`search`/`forget`/...). These
become `kind="memory"` spans, a first-class OpenTrace span kind.

### Human approvals

Any irreversible or sensitive action (deleting files, sending money, posting)
should go through `human_approval`, which records a `kind="human"` span plus a
boolean `approval` score, giving you a reviewable record of what the human OK'd.

## Verified

- `python3 -m py_compile` passes.
- A standalone run (session > turn > llm(1280 tok, $0.01) > `list_downloads`
  tool > `memory:read` > `human` approval(+score) > `rm` tool) produced **17
  records, 0 validation errors** with exactly that tree.

## Limitations / notes

- Uses a module-global "current agent" set inside `session()`/`turn()`. It is
  not safe across threads or concurrent async turns; for a concurrent harness,
  hold the `agent` span the context managers yield and pass it explicitly
  instead of relying on the global.
- Token/cost values are whatever you pass to `.done(...)`; the toolkit does not
  compute cost.
- Tool arguments are captured by binding the function signature and coerced to
  JSON-safe values (truncated); large blobs are stringified and clipped.
