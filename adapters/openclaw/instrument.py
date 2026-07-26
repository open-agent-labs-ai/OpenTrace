#!/usr/bin/env python3
"""Drop-in OpenTrace instrumentation for the OpenClaw personal agent harness.

OpenClaw has no public instrumentation API, so you wrap its main loop yourself.
This module gives you a small, self-contained toolkit built on the OpenTrace
SDK:

    from instrument import OpenClawTracer, trace_tool, traced_llm_call, \
        traced_memory, human_approval

    tracer = OpenClawTracer("trace.ot.jsonl", agent_name="OpenClaw")

    with tracer.session(session_id="local-1") as sess:
        with sess.turn(user_input="tidy my downloads folder") as turn:
            with traced_llm_call(model="anthropic/claude-opus-4-8",
                                 input_text="tidy my downloads folder") as call:
                # ...call your model...
                call.done(output_text="I'll list and sort the files.",
                          input_tokens=1200, output_tokens=80, cost=0.01)

            files = list_downloads()          # a @trace_tool-decorated function

            with traced_memory("read", key="user.preferences") as mem:
                prefs = load_prefs()
                mem.done(result=prefs)

            if human_approval("Delete 12 old installers?"):
                delete_installers()

The whole thing is import-safe and testable without OpenClaw: nothing here
imports the harness. Wrap your real loop as shown in README.md.
"""
from __future__ import annotations

import contextlib
import functools
from typing import Any, Callable, Dict, Optional

import opentrace as ot

# Module-global "current agent span" so decorators/helpers can find their parent
# without threading it through every call. Set inside `session()`/`turn()`.
_CURRENT_AGENT: Optional[ot.Span] = None


def _agent() -> ot.Span:
    if _CURRENT_AGENT is None:
        raise RuntimeError(
            "no active OpenClaw trace; wrap your loop in `with tracer.session(): "
            "with sess.turn(...):` before using trace helpers")
    return _CURRENT_AGENT


class _Session:
    def __init__(self, trace: ot.Trace, agent_name: str, agent_role: str):
        self._trace = trace
        self._agent_name = agent_name
        self._agent_role = agent_role

    @contextlib.contextmanager
    def turn(self, user_input: Optional[str] = None, **turn_fields):
        """Open a turn + the main agent for one user interaction."""
        global _CURRENT_AGENT
        input_msgs = ([ot.content.user(user_input)] if user_input is not None else None)
        turn = self._trace.turn(input=input_msgs, **turn_fields)
        turn.__enter__()
        agent = turn.agent(id="main", name=self._agent_name, role=self._agent_role)
        agent.__enter__()
        prev = _CURRENT_AGENT
        _CURRENT_AGENT = agent
        try:
            yield agent
        except Exception as exc:
            agent.record_error(exc)
            raise
        finally:
            _CURRENT_AGENT = prev
            agent.end()
            turn.end()


class OpenClawTracer:
    """Thin wrapper over ``opentrace.Tracer`` with a session context manager."""

    def __init__(self, output: str = "openclaw.ot.jsonl",
                 agent_name: str = "OpenClaw", agent_role: str = "orchestrator",
                 environment: str = "personal", **tracer_kwargs):
        self._agent_name = agent_name
        self._agent_role = agent_role
        self.tracer = ot.Tracer(
            name=agent_name,
            producer={"name": "openclaw", "version": tracer_kwargs.pop("version", "0")},
            output=output, environment=environment, **tracer_kwargs)

    @contextlib.contextmanager
    def session(self, session_id: Optional[str] = None, goal: Optional[str] = None,
                **trace_fields):
        trace = self.tracer.trace(name=self._agent_name, session_id=session_id,
                                  **trace_fields)
        trace.__enter__()
        try:
            yield _Session(trace, self._agent_name, self._agent_role)
            trace.finish(outcome="success")
        except Exception:
            trace.finish(outcome="failure")
            raise

    def close(self) -> None:
        self.tracer.close()


# --------------------------------------------------------------------------- #
# Decorator + context managers
# --------------------------------------------------------------------------- #

def trace_tool(_fn: Optional[Callable] = None, *, name: Optional[str] = None,
               tool_type: str = "function"):
    """Decorator: record each call of a tool function as an OpenTrace tool span.

        @trace_tool
        def list_downloads(): ...

        @trace_tool(name="rm", tool_type="function")
        def delete_installers(): ...
    """
    def decorate(fn: Callable) -> Callable:
        tool_name = name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            arguments = _describe_args(fn, args, kwargs)
            with _agent().tool(tool_name, arguments=arguments,
                               tool_type=tool_type) as span:
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    span.record_error(exc)
                    raise
                span.set_result(_safe(result))
                return result
        return wrapper

    return decorate(_fn) if callable(_fn) else decorate


class _LLMCall:
    def __init__(self, span: ot.Span):
        self.span = span

    def done(self, output_text: Optional[str] = None, output=None,
             input_tokens: Optional[int] = None, output_tokens: Optional[int] = None,
             cost: Optional[float] = None, finish_reason: Optional[str] = None,
             **fields) -> None:
        msgs = None
        if output_text is not None:
            msgs = [ot.content.assistant(output_text, finish_reason=finish_reason)]
        elif output is not None:
            msgs = output if isinstance(output, list) else [ot.content.assistant(str(output))]
        usage = None
        if input_tokens is not None or output_tokens is not None:
            usage = {"input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0}
        cost_obj = {"total": cost, "currency": "USD"} if cost is not None else None
        self.span.set_output(messages=msgs, usage=usage, cost=cost_obj)
        for k, v in fields.items():
            self.span.set_field(**{k: v})


@contextlib.contextmanager
def traced_llm_call(model: str, provider: Optional[str] = None,
                    input_text: Optional[str] = None, input=None, **fields):
    """Context manager around a model call. Call ``.done(...)`` on the yielded
    handle to record output/usage/cost."""
    msgs = None
    if input_text is not None:
        msgs = [ot.content.user(input_text)]
    elif input is not None:
        msgs = input
    span = _agent().llm(model=model, provider=provider or _provider(model),
                        input=msgs, **fields)
    span.__enter__()
    handle = _LLMCall(span)
    try:
        yield handle
    except Exception as exc:
        span.record_error(exc)
        raise
    finally:
        span.end()


class _MemoryOp:
    def __init__(self, span: ot.Span):
        self.span = span

    def done(self, result: Any = None, hit: Optional[bool] = None) -> None:
        if result is not None:
            self.span.set_result(_safe(result))
        if hit is not None:
            self.span.set_field(hit=hit)


@contextlib.contextmanager
def traced_memory(operation: str, key: Optional[str] = None,
                  store: Optional[str] = None, **fields):
    """A ``kind="memory"`` span for personal-context / memory operations.

    ``operation`` is ``read`` | ``write`` | ``search`` | ``forget`` etc.
    Use this for the agent reading your preferences, notes, contacts, history.
    """
    span = _agent().span(ot.SpanKind.MEMORY, name="memory:" + operation,
                         operation=operation, key=key, store=store, **fields)
    span.__enter__()
    op = _MemoryOp(span)
    try:
        yield op
    except Exception as exc:
        span.record_error(exc)
        raise
    finally:
        span.end()


def human_approval(prompt: str, approve_fn: Optional[Callable[[str], bool]] = None,
                   interaction: str = "approval") -> bool:
    """Record a ``kind="human"`` span for a human-in-the-loop approval.

    ``approve_fn`` is your harness's blocking prompt (defaults to ``input()``).
    Returns the boolean decision and records it on the span.
    """
    if approve_fn is None:
        def approve_fn(p: str) -> bool:  # pragma: no cover - interactive default
            return input(p + " [y/N] ").strip().lower() in ("y", "yes")
    with _agent().human(interaction=interaction, prompt=prompt) as span:
        decision = bool(approve_fn(prompt))
        span.set_field(response="approved" if decision else "denied")
        span.score("approval", 1 if decision else 0, data_type="boolean")
        return decision


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _provider(model: str) -> Optional[str]:
    m = (model or "").lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "gpt" in m or "openai" in m:
        return "openai"
    if "gemini" in m or "google" in m:
        return "google"
    return None


def _describe_args(fn: Callable, args, kwargs) -> Dict[str, Any]:
    try:
        import inspect
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return {k: _safe(v) for k, v in bound.arguments.items()}
    except Exception:
        return {"args": _safe(list(args)), "kwargs": _safe(kwargs)}


def _safe(v: Any) -> Any:
    """Coerce arbitrary values into JSON-serializable form for the trace."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _safe(x) for k, x in list(v.items())[:50]}
    if isinstance(v, (list, tuple)):
        return [_safe(x) for x in list(v)[:50]]
    return str(v)[:2000]
