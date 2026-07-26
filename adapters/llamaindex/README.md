# LlamaIndex -> OpenTrace

`opentrace_llamaindex.py` bridges LlamaIndex's
`llama_index.core.instrumentation` dispatcher into the OpenTrace SDK. Register
it once and every LLM chat, agent tool call, retrieval, and embedding becomes an
OpenTrace span under a live agent.

## What it captures

| LlamaIndex event (new instrumentation API) | OpenTrace span |
|---|---|
| `LLMChatStartEvent` / `LLMPredictStartEvent` / `LLMCompletionStartEvent` | opens `llm` span (model, input messages) |
| `LLMChatEndEvent` / `LLMPredictEndEvent` / `LLMCompletionEndEvent` | closes it with token `usage` |
| `AgentToolCallEvent` | `tool` span (name, arguments, tool id) |
| `RetrievalStartEvent` / `RetrievalEndEvent` | `retriever` span (query -> retrieved nodes as documents) |
| `EmbeddingStartEvent` / `EmbeddingEndEvent` | `embedding` span (chunk count) |
| `AgentRunStepStartEvent` / `AgentRunStepEndEvent` | `chain` span (a step) |

Start/end events are correlated by their shared `span_id`. Token usage is read
from the LLM response's `raw.usage` (`prompt_tokens`/`completion_tokens` and
`input_tokens`/`output_tokens` are both handled).

## Install

```bash
pip install llama-index-core
pip install -e /path/to/OpenTrace/sdk/python   # or set PYTHONPATH
```

The adapter file is **import-safe without LlamaIndex** (the base classes are
guarded); `instrument()` raises a clear error if LlamaIndex is missing, but the
mapping logic and `OpenTraceEventHandler` remain usable for testing.

## Usage

```python
import opentrace as ot
from opentrace_llamaindex import instrument

tracer = ot.Tracer(name="rag-agent", producer={"name": "app", "version": "1.0"},
                   output="trace.ot.jsonl", environment="production")

session = instrument(tracer, agent_name="RAG")     # registers on root dispatcher

# ...run LlamaIndex as usual...
# index.as_query_engine().query("What is our auth policy?")
# or an AgentRunner / FunctionCallingAgent

session.close()          # closes agent/turn/trace
tracer.close()
```

`instrument()` returns an `OpenTraceSession` that owns the trace -> turn -> agent
scope; child spans from events attach under that agent. Pass
`with_span_handler=True` to also register the lightweight
`OpenTraceSpanHandler` (raw LlamaIndex span boundaries as `log` events).

### Under the hood

```python
from llama_index.core.instrumentation import get_dispatcher
dispatcher = get_dispatcher()                 # root dispatcher
dispatcher.add_event_handler(OpenTraceEventHandler(session))
dispatcher.add_span_handler(OpenTraceSpanHandler(session))   # optional
```

## Legacy `CallbackManager` path

For older LlamaIndex (pre-instrumentation) you can wire a
`BaseCallbackHandler` instead. Map `CBEventType` -> OpenTrace kind on
`on_event_start` / `on_event_end`:

| `CBEventType` | OpenTrace |
|---|---|
| `LLM` | `llm` |
| `FUNCTION_CALL` | `tool` |
| `RETRIEVE` | `retriever` |
| `EMBEDDING` | `embedding` |
| `AGENT_STEP` | `chain` |
| `RERANKING` | `reranker` |

```python
from llama_index.core.callbacks import CallbackManager
Settings.callback_manager = CallbackManager([your_handler])
```

The event-handler path above is preferred on current LlamaIndex; the callback
mapping is provided as guidance and is not the shipped code.

## Verified

- `python3 -m py_compile` passes.
- With LlamaIndex **absent** (`_HAS_LI == False`), a synthetic event sequence
  (Retrieval start/end, Embedding start/end, LLMChat start/end with usage,
  AgentToolCall) drove `OpenTraceEventHandler.handle` and produced **14
  records, 0 validation errors**, tree `turn > agent > {retriever, embedding,
  llm(130 tok), tool}`.

## Limitations

- `AgentToolCallEvent` is a single event (no paired end), so tool spans capture
  arguments but not the tool's return value; results surface later as
  `FunctionCallingAgent` / tool-output events which this version does not
  correlate back. Extend `_dispatch` if you need tool results.
- Event field names (`model_dict`, `messages`, `nodes`, `str_or_query_bundle`,
  `chunks`, `tool_name`/`arguments`) track LlamaIndex's event classes; unknown
  shapes are read defensively and dropped rather than guessed.
- All semantic spans are placed flat under one agent; nested workflow structure
  from `AgentRunStep*` is emitted as sibling `chain` spans, not deep nesting.
- Cost is not computed (LlamaIndex reports tokens); add a price table
  downstream.
