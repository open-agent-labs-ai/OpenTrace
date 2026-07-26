#!/usr/bin/env python3
"""Drop-in OpenTrace instrumentation for the Hermes personal assistant.

Hermes is a personal assistant harness (messaging, calendar, memory) with no
public instrumentation API, so you wrap its loop yourself. Unlike the OpenClaw
adapter's global-context style, this toolkit passes an explicit ``Turn`` handle
around, which makes it **safe for a Hermes that services several channels/turns
concurrently** (e.g. Slack + email at once).

    from instrument import HermesTracer

    tracer = HermesTracer("hermes.ot.jsonl", agent_name="Hermes")

    with tracer.session(session_id="dm:alice") as sess:
        with sess.turn("remind the team about standup") as turn:
            with turn.llm("anthropic/claude-opus-4-8", input_text="...") as call:
                call.done(output_text="I'll draft a message.",
                          input_tokens=900, output_tokens=40, cost=0.006)

            with turn.memory("read", key="contacts.team"):
                team = load_team()

            draft = "Standup in 10 min!"
            if turn.approval("Send this to #team?", detail=draft):
                with turn.tool("slack_send", arguments={"channel": "#team", "text": draft}) as t:
                    t.result(send_slack("#team", draft))

            # delegate a sub-task to a specialized skill:
            with turn.handoff("scheduler") as sub:
                with sub.tool("create_event", arguments={"title": "Standup"}) as t:
                    t.result({"event_id": "evt_1"})

Import-safe and testable without Hermes; nothing here imports the harness.
"""
from __future__ import annotations

import contextlib
import functools
from typing import Any, Callable, Optional

import opentrace as ot


class _ToolSpan:
    def __init__(self, span: ot.Span):
        self._span = span

    def result(self, value: Any, is_error: bool = False) -> None:
        self._span.set_result(_safe(value), is_error=is_error)

    @property
    def span(self) -> ot.Span:
        return self._span


class _LLMSpan:
    def __init__(self, span: ot.Span):
        self._span = span

    def done(self, output_text: Optional[str] = None, output=None,
             input_tokens: Optional[int] = None, output_tokens: Optional[int] = None,
             cost: Optional[float] = None, finish_reason: Optional[str] = None) -> None:
        msgs = None
        if output_text is not None:
            msgs = [ot.content.assistant(output_text, finish_reason=finish_reason)]
        elif output is not None:
            msgs = output if isinstance(output, list) else [ot.content.assistant(str(output))]
        usage = None
        if input_tokens is not None or output_tokens is not None:
            usage = {"input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0}
        cost_obj = {"total": cost, "currency": "USD"} if cost is not None else None
        self._span.set_output(messages=msgs, usage=usage, cost=cost_obj)


class Agent:
    """A live agent scope. Spawns spans for tools, llm calls, memory, handoffs."""

    def __init__(self, span: ot.Span, name: str):
        self._span = span
        self.name = name

    @contextlib.contextmanager
    def tool(self, tool_name: str, arguments: Any = None,
             tool_call_id: Optional[str] = None, tool_type: str = "function"):
        span = self._span.tool(tool_name, arguments=_safe(arguments),
                               tool_call_id=tool_call_id, tool_type=tool_type)
        span.__enter__()
        handle = _ToolSpan(span)
        try:
            yield handle
        except Exception as exc:
            span.record_error(exc)
            raise
        finally:
            span.end()

    def trace_tool(self, _fn: Optional[Callable] = None, *,
                   name: Optional[str] = None):
        """Decorator variant of :meth:`tool` bound to this agent."""
        def decorate(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                with self.tool(name or fn.__name__,
                               arguments={"args": _safe(list(args)),
                                          "kwargs": _safe(kwargs)}) as t:
                    result = fn(*args, **kwargs)
                    t.result(result)
                    return result
            return wrapper
        return decorate(_fn) if callable(_fn) else decorate

    @contextlib.contextmanager
    def llm(self, model: str, provider: Optional[str] = None,
            input_text: Optional[str] = None, input=None):
        msgs = [ot.content.user(input_text)] if input_text is not None else input
        span = self._span.llm(model=model, provider=provider or _provider(model),
                              input=msgs)
        span.__enter__()
        try:
            yield _LLMSpan(span)
        except Exception as exc:
            span.record_error(exc)
            raise
        finally:
            span.end()

    @contextlib.contextmanager
    def memory(self, operation: str, key: Optional[str] = None,
               store: Optional[str] = None):
        """A ``kind="memory"`` span for personal-context reads/writes/searches."""
        span = self._span.span(ot.SpanKind.MEMORY, name="memory:" + operation,
                               operation=operation, key=key, store=store)
        span.__enter__()
        try:
            yield span
        except Exception as exc:
            span.record_error(exc)
            raise
        finally:
            span.end()

    def approval(self, prompt: str, detail: Any = None,
                 approve_fn: Optional[Callable[[str], bool]] = None,
                 interaction: str = "approval") -> bool:
        """Human-in-the-loop gate (``kind="human"``). Use before Hermes sends a
        message, spends money, or changes a calendar. Returns the decision."""
        if approve_fn is None:
            def approve_fn(p: str) -> bool:  # pragma: no cover - interactive
                return input(p + " [y/N] ").strip().lower() in ("y", "yes")
        with self._span.human(interaction=interaction, prompt=prompt,
                              detail=_safe(detail)) as span:
            decision = bool(approve_fn(prompt))
            span.set_field(response="approved" if decision else "denied")
            span.score("approval", 1 if decision else 0, data_type="boolean")
            return decision

    @contextlib.contextmanager
    def handoff(self, to_agent: str, mode: str = "delegate",
                reason: Optional[str] = None):
        """Delegate to a specialized Hermes skill/sub-agent. Yields the sub-Agent."""
        h = self._span.handoff(from_agent=self.name, to_agent=to_agent,
                               mode=mode, reason=reason)
        h.__enter__()
        sub_span = h.agent(id=to_agent, name=to_agent, role="subagent")
        sub_span.__enter__()
        try:
            yield Agent(sub_span, to_agent)
        except Exception as exc:
            sub_span.record_error(exc)
            raise
        finally:
            sub_span.end()
            h.end()


class Turn(Agent):
    """One user interaction. Is itself the main Agent scope, plus owns the turn."""

    def __init__(self, turn_span: ot.Span, agent_span: ot.Span, name: str):
        super().__init__(agent_span, name)
        self._turn = turn_span

    def _close(self, error: Optional[Exception] = None) -> None:
        if error is not None:
            self._span.record_error(error)
        self._span.end()
        self._turn.end()


class Session:
    def __init__(self, trace: ot.Trace, agent_name: str, agent_role: str):
        self._trace = trace
        self._agent_name = agent_name
        self._agent_role = agent_role

    @contextlib.contextmanager
    def turn(self, user_input: Optional[str] = None, **turn_fields):
        input_msgs = [ot.content.user(user_input)] if user_input is not None else None
        turn_span = self._trace.turn(input=input_msgs, **turn_fields)
        turn_span.__enter__()
        agent_span = turn_span.agent(id="main", name=self._agent_name,
                                     role=self._agent_role)
        agent_span.__enter__()
        turn = Turn(turn_span, agent_span, self._agent_name)
        try:
            yield turn
            turn._close()
        except Exception as exc:
            turn._close(error=exc)
            raise


class HermesTracer:
    def __init__(self, output: str = "hermes.ot.jsonl", agent_name: str = "Hermes",
                 agent_role: str = "orchestrator", environment: str = "personal",
                 **tracer_kwargs):
        self._agent_name = agent_name
        self._agent_role = agent_role
        self.tracer = ot.Tracer(
            name=agent_name,
            producer={"name": "hermes", "version": tracer_kwargs.pop("version", "0")},
            output=output, environment=environment, **tracer_kwargs)

    @contextlib.contextmanager
    def session(self, session_id: Optional[str] = None, **trace_fields):
        trace = self.tracer.trace(name=self._agent_name, session_id=session_id,
                                  **trace_fields)
        trace.__enter__()
        try:
            yield Session(trace, self._agent_name, self._agent_role)
            trace.finish(outcome="success")
        except Exception:
            trace.finish(outcome="failure")
            raise

    def close(self) -> None:
        self.tracer.close()


# ---- helpers -------------------------------------------------------------- #

def _provider(model: str) -> Optional[str]:
    m = (model or "").lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "gpt" in m or "openai" in m:
        return "openai"
    if "gemini" in m or "google" in m:
        return "google"
    return None


def _safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _safe(x) for k, x in list(v.items())[:50]}
    if isinstance(v, (list, tuple)):
        return [_safe(x) for x in list(v)[:50]]
    return str(v)[:2000]
