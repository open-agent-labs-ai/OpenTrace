#!/usr/bin/env python3
"""LlamaIndex -> OpenTrace instrumentation.

Bridges LlamaIndex's ``llama_index.core.instrumentation`` dispatcher into the
OpenTrace SDK. Register it once on the root dispatcher and every LLM chat, agent
tool call, retrieval, and embedding becomes an OpenTrace span under a live
agent.

    import opentrace as ot
    from opentrace_llamaindex import instrument

    tracer = ot.Tracer(name="rag-agent", output="trace.ot.jsonl")
    session = instrument(tracer, agent_name="RAG")   # registers handlers
    # ... run your LlamaIndex query / agent ...
    session.close()                                  # closes trace + flushes
    tracer.close()

The file is import-safe even when ``llama_index`` is not installed: the
LlamaIndex base classes are imported lazily and guarded, and the mapping logic
(keyed on event class names) is fully defined regardless.

Event mapping (new instrumentation API, ``BaseEventHandler.handle``):
    LLMChatStartEvent / LLMPredictStartEvent   -> open  llm span
    LLMChatEndEvent   / LLMPredictEndEvent      -> close llm span (+ usage)
    AgentToolCallEvent                          -> tool span (open+close)
    RetrievalStartEvent / RetrievalEndEvent     -> retriever span
    EmbeddingStartEvent / EmbeddingEndEvent     -> embedding span
    AgentRunStepStartEvent / ...EndEvent        -> chain span (a step)

Legacy ``CallbackManager`` path is documented in README.md; this module targets
the current dispatcher/event-handler API.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import opentrace as ot

# ---- Guarded LlamaIndex imports ------------------------------------------- #
try:  # pragma: no cover - depends on optional dependency
    from llama_index.core.instrumentation import get_dispatcher
    from llama_index.core.instrumentation.event_handlers.base import BaseEventHandler
    _HAS_LI = True
except Exception:  # ImportError or partial installs
    get_dispatcher = None  # type: ignore
    BaseEventHandler = object  # type: ignore
    _HAS_LI = False


def _event_class(event: Any) -> str:
    """Return the LlamaIndex event's class name (works via class_name() or type)."""
    try:
        return event.class_name()
    except Exception:
        return type(event).__name__


def _get(event: Any, *names, default=None):
    for n in names:
        v = getattr(event, n, None)
        if v is not None:
            return v
    return default


def _usage_from_response(resp: Any) -> Optional[Dict[str, Any]]:
    """Pull token usage out of a LlamaIndex ChatResponse/CompletionResponse."""
    if resp is None:
        return None
    raw = getattr(resp, "raw", None)
    usage = None
    if isinstance(raw, dict):
        usage = raw.get("usage")
    elif raw is not None:
        usage = getattr(raw, "usage", None)
    if usage is None:
        return None

    def g(*keys):
        for k in keys:
            if isinstance(usage, dict) and usage.get(k) is not None:
                return usage[k]
            v = getattr(usage, k, None)
            if v is not None:
                return v
        return None

    out = {
        "input_tokens": g("prompt_tokens", "input_tokens"),
        "output_tokens": g("completion_tokens", "output_tokens"),
        "total_tokens": g("total_tokens"),
    }
    return {k: v for k, v in out.items() if v is not None} or None


class OpenTraceEventHandler(BaseEventHandler):
    """Maps LlamaIndex instrumentation events to OpenTrace spans.

    Subclasses ``BaseEventHandler`` when LlamaIndex is available; otherwise it is
    a plain object exposing the same ``handle`` method for offline testing.
    """

    # pydantic (LlamaIndex base) requires declared/allowed extra fields; when the
    # base is a plain object this is ignored.
    if _HAS_LI:
        model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    @classmethod
    def class_name(cls) -> str:
        return "OpenTraceEventHandler"

    def __init__(self, session: "OpenTraceSession", **kw):
        if _HAS_LI:
            super().__init__(**kw)
            object.__setattr__(self, "_ot_session", session)
        else:
            self._ot_session = session

    @property
    def session(self) -> "OpenTraceSession":
        return getattr(self, "_ot_session")

    # BaseEventHandler API -------------------------------------------------
    def handle(self, event: Any, **kwargs) -> None:
        try:
            self._dispatch(event)
        except Exception as exc:  # never break the host run
            self.session._log_error(exc)

    def _dispatch(self, event: Any) -> None:
        name = _event_class(event)
        s = self.session
        span_key = _get(event, "span_id", "id_", "id", default=id(event))

        if name in ("LLMChatStartEvent", "LLMPredictStartEvent",
                    "LLMCompletionStartEvent"):
            model = _model_name(event)
            sp = s.agent.llm(model=model, provider=_provider_name(model))
            msgs = _get(event, "messages")
            if msgs is not None:
                sp.set_field(input={"messages": _messages_to_ot(msgs)})
            sp.__enter__()
            s.open_spans[span_key] = sp

        elif name in ("LLMChatEndEvent", "LLMPredictEndEvent",
                      "LLMCompletionEndEvent"):
            sp = s.open_spans.pop(span_key, None)
            if sp is not None:
                resp = _get(event, "response", "messages")
                sp.set_output(usage=_usage_from_response(_get(event, "response")))
                if resp is not None:
                    sp.set_field(response={"text": str(resp)[:2000]})
                sp.end()

        elif name == "AgentToolCallEvent":
            tool_name = _get(event, "tool_name", default="tool")
            args = _get(event, "arguments", "tool_kwargs")
            with s.agent.tool(tool_name, arguments=args,
                              tool_call_id=_get(event, "tool_id")) as t:
                pass  # single-shot event; result arrives via tool output events

        elif name in ("RetrievalStartEvent",):
            sp = s.agent.retriever(query=_stringify(_get(event, "str_or_query_bundle",
                                                          "query")))
            sp.__enter__()
            s.open_spans[span_key] = sp

        elif name in ("RetrievalEndEvent",):
            sp = s.open_spans.pop(span_key, None)
            if sp is not None:
                nodes = _get(event, "nodes", default=[])
                sp.set_output(output={"documents": _nodes_to_docs(nodes)})
                sp.end()

        elif name in ("EmbeddingStartEvent",):
            sp = s.agent.span(ot.SpanKind.EMBEDDING, name="embedding",
                              model=_model_name(event))
            sp.__enter__()
            s.open_spans[span_key] = sp

        elif name in ("EmbeddingEndEvent",):
            sp = s.open_spans.pop(span_key, None)
            if sp is not None:
                chunks = _get(event, "chunks", default=[])
                sp.set_metrics(embeddings=len(chunks) if chunks else 0)
                sp.end()

        elif name in ("AgentRunStepStartEvent", "AgentChatWithStepStartEvent"):
            sp = s.agent.span(ot.SpanKind.CHAIN, name="step")
            sp.__enter__()
            s.open_spans[span_key] = sp

        elif name in ("AgentRunStepEndEvent", "AgentChatWithStepEndEvent"):
            sp = s.open_spans.pop(span_key, None)
            if sp is not None:
                sp.end()
        # other events are ignored


class OpenTraceSpanHandler:
    """Minimal LlamaIndex span handler.

    The event handler above already produces a complete span tree. This span
    handler is offered for parity with LlamaIndex's ``BaseSpanHandler`` API and
    simply records enter/exit as OpenTrace ``log`` events on the active agent.
    Register it via ``dispatcher.add_span_handler(...)`` if you want raw
    LlamaIndex span boundaries in addition to the semantic spans.
    """

    def __init__(self, session: "OpenTraceSession"):
        self.session = session

    def span_enter(self, id_: str, bound_args=None, instance=None, **kw) -> None:
        try:
            self.session.agent.log("span_enter", level="debug", span=str(id_))
        except Exception:
            pass

    def span_exit(self, id_: str, bound_args=None, instance=None, result=None,
                  **kw) -> None:
        try:
            self.session.agent.log("span_exit", level="debug", span=str(id_))
        except Exception:
            pass

    def span_drop(self, id_: str, err=None, **kw) -> None:
        try:
            self.session.agent.log("span_drop", level="warning", span=str(id_))
        except Exception:
            pass


class OpenTraceSession:
    """Owns the live trace/turn/agent that events attach to."""

    def __init__(self, tracer: ot.Tracer, agent_name: str = "LlamaIndex",
                 agent_role: str = "orchestrator", session_id: Optional[str] = None,
                 goal: Optional[str] = None):
        self.tracer = tracer
        self.trace = tracer.trace(name=agent_name, session_id=session_id)
        self.trace.__enter__()
        self.turn = self.trace.turn()
        self.turn.__enter__()
        self.agent = self.turn.agent(id="main", name=agent_name, role=agent_role)
        self.agent.__enter__()
        self.open_spans: Dict[Any, ot.Span] = {}
        self._errors = 0

    def _log_error(self, exc: Exception) -> None:
        self._errors += 1
        try:
            self.agent.log("opentrace_llamaindex error: %s" % exc, level="error")
        except Exception:
            pass

    def close(self, outcome: str = "success") -> None:
        # close any still-open spans
        for sp in list(self.open_spans.values()):
            try:
                sp.end()
            except Exception:
                pass
        self.open_spans.clear()
        for ctx in (self.agent, self.turn):
            try:
                ctx.end()
            except Exception:
                pass
        try:
            self.trace.finish(outcome=outcome)
        except Exception:
            pass


def instrument(tracer: ot.Tracer, agent_name: str = "LlamaIndex",
               agent_role: str = "orchestrator", session_id: Optional[str] = None,
               with_span_handler: bool = False) -> OpenTraceSession:
    """Register OpenTrace handlers on the LlamaIndex root dispatcher.

    Returns an :class:`OpenTraceSession`; call ``.close()`` when the run ends.
    Raises ``RuntimeError`` if LlamaIndex is not importable.
    """
    if not _HAS_LI:
        raise RuntimeError(
            "llama_index is not installed; `pip install llama-index-core` to use "
            "instrument(). The mapping logic is still importable for testing.")
    session = OpenTraceSession(tracer, agent_name=agent_name,
                               agent_role=agent_role, session_id=session_id)
    dispatcher = get_dispatcher()  # root dispatcher
    dispatcher.add_event_handler(OpenTraceEventHandler(session))
    if with_span_handler:
        dispatcher.add_span_handler(OpenTraceSpanHandler(session))
    return session


# ---- content helpers ------------------------------------------------------ #

def _model_name(event: Any) -> Optional[str]:
    m = _get(event, "model_dict", "model")
    if isinstance(m, dict):
        return m.get("model") or m.get("model_name")
    return _get(event, "model_name") or (str(m) if m else None)


def _provider_name(model: Optional[str]) -> Optional[str]:
    if not model:
        return None
    m = model.lower()
    if "gpt" in m or "o1" in m or "o3" in m:
        return "openai"
    if "claude" in m:
        return "anthropic"
    if "gemini" in m:
        return "google"
    return None


def _messages_to_ot(messages: Any) -> list:
    out = []
    for m in messages or []:
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else "user")
        content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else str(m))
        out.append({"role": str(role), "parts": [{"type": "text", "text": str(content)}]})
    return out


def _nodes_to_docs(nodes: Any) -> list:
    docs = []
    for n in nodes or []:
        node = getattr(n, "node", n)
        docs.append({
            "id": getattr(node, "node_id", None) or getattr(node, "id_", None),
            "score": getattr(n, "score", None),
            "text": (getattr(node, "text", "") or "")[:500] or None,
        })
    return [{k: v for k, v in d.items() if v is not None} for d in docs]


def _stringify(v: Any) -> Optional[str]:
    if v is None:
        return None
    return getattr(v, "query_str", None) or str(v)
