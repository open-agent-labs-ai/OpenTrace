#!/usr/bin/env python3
"""Pydantic AI -> OpenTrace.

Two integration paths (see README.md):

1. **Direct OTLP** — Pydantic AI already emits standard OTel GenAI spans. Point
   its OTLP exporter at a collector/file and run
   ``adapters/opentelemetry-ingest/otlp_to_opentrace.py``. Nothing here needed.

2. **In-process hook** (this module) — richer, no collector. Use either:
   - ``make_event_stream_handler(session)`` as the agent's
     ``event_stream_handler``, or
   - ``run_traced(agent, prompt, tracer)`` which drives ``agent.iter()`` and
     records the node loop.

Both feed a shared ``_Recorder`` that writes OpenTrace spans via the SDK.

Events handled (``event_stream_handler`` stream):
    PartStartEvent(part=ThinkingPart)  -> reasoning event
    PartStartEvent(part=TextPart)      -> assistant text (buffered)
    FunctionToolCallEvent(part=ToolCallPart(tool_name, args, tool_call_id))
                                       -> open tool span
    FunctionToolResultEvent(result=ToolReturnPart(content, tool_call_id))
                                       -> close tool span

Nodes handled (``agent.iter()``):
    UserPromptNode   -> message event (the prompt)
    ModelRequestNode -> llm span (streamed events routed through the recorder)
    CallToolsNode    -> tool spans (via the same events)
    End              -> finalize

Import-safe without ``pydantic_ai`` installed: all pydantic-ai types are
referenced by class name, never imported at module top level.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import opentrace as ot

try:  # optional; only used for node-type predicates in run_traced
    from pydantic_ai import Agent as _PydAgent
    _HAS_PYD = True
except Exception:
    _PydAgent = None  # type: ignore
    _HAS_PYD = False


def _cn(obj: Any) -> str:
    return type(obj).__name__


def _get(obj: Any, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


class _Recorder:
    """Holds the live trace/turn/agent and turns pydantic-ai events into spans."""

    def __init__(self, tracer: ot.Tracer, agent_name: str = "PydanticAI",
                 model: Optional[str] = None, session_id: Optional[str] = None):
        self.tracer = tracer
        self.agent_name = agent_name
        self.default_model = model
        self.trace = tracer.trace(name=agent_name, session_id=session_id)
        self.trace.__enter__()
        self.turn = self.trace.turn()
        self.turn.__enter__()
        self.agent = self.turn.agent(id="main", name=agent_name,
                                     role="orchestrator", model=model)
        self.agent.__enter__()
        self.open_tools: Dict[str, ot.Span] = {}
        self.llm: Optional[ot.Span] = None

    # -- LLM span lifecycle ----------------------------------------------
    def open_llm(self, model: Optional[str] = None) -> None:
        if self.llm is not None:
            return
        m = model or self.default_model
        self.llm = self.agent.llm(model=m, provider=_provider(m))
        self.llm.__enter__()

    def close_llm(self, usage: Any = None) -> None:
        if self.llm is None:
            return
        u = _usage_to_ot(usage)
        if u:
            self.llm.set_usage(u)
        self.llm.end()
        self.llm = None

    # -- event handling ---------------------------------------------------
    def on_event(self, event: Any) -> None:
        name = _cn(event)
        if name == "PartStartEvent":
            part = _get(event, "part")
            self._on_part(part)
        elif name == "PartDeltaEvent":
            return  # streaming deltas: ignored for span structure
        elif name == "FunctionToolCallEvent":
            self._on_tool_call(_get(event, "part"))
        elif name == "FunctionToolResultEvent":
            self._on_tool_result(_get(event, "result", "tool_return", "part"))
        elif name in ("FinalResultEvent",):
            return

    def _on_part(self, part: Any) -> None:
        if part is None:
            return
        pname = _cn(part)
        parent = self.llm or self.agent
        if pname == "ThinkingPart":
            parent.reasoning(_get(part, "content", default=""))
        elif pname == "TextPart":
            text = _get(part, "content", default="")
            if text and self.llm is not None:
                self.llm.set_output(messages=[{"role": "assistant",
                                               "parts": [{"type": "text", "text": str(text)}]}])
        elif pname == "ToolCallPart":
            self._on_tool_call(part)

    def _on_tool_call(self, part: Any) -> None:
        if part is None:
            return
        tool_name = _get(part, "tool_name", default="tool")
        call_id = _get(part, "tool_call_id", "tool_id")
        args = _get(part, "args", "arguments")
        if isinstance(args, str):
            args = _maybe_json(args)
        sp = self.agent.tool(tool_name, arguments=args, tool_call_id=call_id)
        sp.__enter__()
        if call_id:
            self.open_tools[call_id] = sp
        else:
            sp.end()

    def _on_tool_result(self, part: Any) -> None:
        if part is None:
            return
        call_id = _get(part, "tool_call_id", "tool_id")
        content = _get(part, "content", "result")
        sp = self.open_tools.pop(call_id, None) if call_id else None
        if sp is not None:
            sp.set_result(content)
            sp.end()

    def message(self, role: str, text: str) -> None:
        self.turn.event("message",
                        message={"role": role,
                                 "parts": [{"type": "text", "text": str(text)}]})

    def close(self, outcome: str = "success") -> None:
        self.close_llm()
        for sp in list(self.open_tools.values()):
            try:
                sp.end()
            except Exception:
                pass
        self.open_tools.clear()
        for ctx in (self.agent, self.turn):
            try:
                ctx.end()
            except Exception:
                pass
        self.trace.finish(outcome=outcome)


class OpenTracePydanticAI:
    """Public session handle. Create one per agent run (or per app)."""

    def __init__(self, tracer: ot.Tracer, agent_name: str = "PydanticAI",
                 model: Optional[str] = None, session_id: Optional[str] = None):
        self.recorder = _Recorder(tracer, agent_name=agent_name, model=model,
                                  session_id=session_id)

    def close(self, outcome: str = "success") -> None:
        self.recorder.close(outcome=outcome)


def make_event_stream_handler(session: OpenTracePydanticAI):
    """Return an ``event_stream_handler`` coroutine to pass to ``Agent`` / run.

        agent = Agent(model, event_stream_handler=make_event_stream_handler(sess))
    or
        await agent.run(prompt, event_stream_handler=make_event_stream_handler(sess))
    """
    rec = session.recorder

    async def handler(ctx: Any, event_stream: Any) -> None:
        # ctx is a RunContext; a model request is starting -> open an llm span.
        rec.open_llm(model=_ctx_model(ctx))
        async for event in event_stream:
            try:
                rec.on_event(event)
            except Exception as exc:  # never break the agent run
                try:
                    rec.agent.log("opentrace pydantic-ai error: %s" % exc, level="error")
                except Exception:
                    pass
        # end of this model-request stream
        rec.close_llm(usage=_ctx_usage(ctx))

    return handler


async def run_traced(agent: Any, user_prompt: str, tracer: ot.Tracer,
                     agent_name: str = "PydanticAI", model: Optional[str] = None,
                     **iter_kwargs) -> Any:
    """Drive ``agent.iter()`` and record the node loop into OpenTrace.

    Returns the pydantic-ai run result. Requires ``pydantic_ai`` installed.
    """
    if not _HAS_PYD:
        raise RuntimeError("pydantic_ai is not installed; use the direct-OTLP path "
                           "or install pydantic-ai to use run_traced().")
    session = OpenTracePydanticAI(tracer, agent_name=agent_name, model=model)
    rec = session.recorder
    outcome = "success"
    try:
        async with agent.iter(user_prompt, **iter_kwargs) as run:
            async for node in run:
                await _on_node(rec, run, node)
        return run.result
    except Exception:
        outcome = "failure"
        raise
    finally:
        session.close(outcome=outcome)


async def _on_node(rec: _Recorder, run: Any, node: Any) -> None:
    Agent = _PydAgent
    if Agent is not None and Agent.is_user_prompt_node(node):
        rec.message("user", _get(node, "user_prompt", default=""))
    elif Agent is not None and Agent.is_model_request_node(node):
        rec.open_llm(model=rec.default_model)
        # stream the request's events through the recorder
        async with node.stream(run.ctx) as request_stream:
            async for event in request_stream:
                rec.on_event(event)
        rec.close_llm(usage=_ctx_usage(run.ctx))
    elif Agent is not None and Agent.is_call_tools_node(node):
        # tool call/return events also arrive via node.stream
        async with node.stream(run.ctx) as tool_stream:
            async for event in tool_stream:
                rec.on_event(event)
    elif Agent is not None and Agent.is_end_node(node):
        return


# ---- helpers -------------------------------------------------------------- #

def _provider(model: Optional[str]) -> Optional[str]:
    if not model:
        return None
    m = str(model).lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "gpt" in m or "openai" in m or m.startswith("o1") or m.startswith("o3"):
        return "openai"
    if "gemini" in m or "google" in m:
        return "google"
    return None


def _usage_to_ot(usage: Any) -> Optional[Dict[str, Any]]:
    if usage is None:
        return None
    def g(*keys):
        for k in keys:
            v = getattr(usage, k, None)
            if v is None and isinstance(usage, dict):
                v = usage.get(k)
            if v is not None:
                return v
        return None
    out = {
        "input_tokens": g("input_tokens", "request_tokens", "prompt_tokens"),
        "output_tokens": g("output_tokens", "response_tokens", "completion_tokens"),
        "total_tokens": g("total_tokens"),
    }
    return {k: v for k, v in out.items() if v is not None} or None


def _ctx_model(ctx: Any) -> Optional[str]:
    model = _get(ctx, "model")
    if model is None:
        return None
    return _get(model, "model_name", "name") or str(model)


def _ctx_usage(ctx: Any) -> Any:
    u = _get(ctx, "usage")
    if callable(u):
        try:
            return u()
        except Exception:
            return None
    return u


def _maybe_json(s: str) -> Any:
    import json
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s
