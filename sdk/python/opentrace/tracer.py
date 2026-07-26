"""OpenTrace tracer: ergonomic, thread-safe production of OpenTrace records.

Usage
-----
    from opentrace import Tracer
    from opentrace import content as C

    tracer = Tracer(name="my-app", producer={"name": "my-app", "version": "1.0"},
                    output="trace.ot.jsonl")

    with tracer.trace(name="fix-test", inputs={"goal": "fix the failing test"}) as tr:
        with tr.turn(input=[C.user("Fix the auth test")]) as turn:
            with turn.agent(id="main", name="Coder", role="orchestrator") as ag:
                with ag.llm(model="anthropic/claude-opus-4-8") as call:
                    call.set_output([C.assistant("...", finish_reason="tool_call")],
                                    usage={"input_tokens": 1500, "output_tokens": 60})
                with ag.tool("run_bash", arguments={"command": "pytest"},
                             tool_call_id="call_1") as t:
                    t.set_result({"exit_code": 0})
"""
from __future__ import annotations

import contextvars
import threading
import time as _time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from . import ids as _ids
from .constants import (
    SPEC_VERSION, SDK_NAME, SDK_VERSION, RecordType, SpanKind, EventName,
    Outcome, StatusCode, Profile,
)
from .exporters.base import Exporter, MultiExporter
from .exporters.jsonl import JsonlExporter

_current_span: "contextvars.ContextVar[Optional[Span]]" = contextvars.ContextVar(
    "opentrace_current_span", default=None
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    """Drop keys whose value is None (keeps records compact)."""
    return {k: v for k, v in d.items() if v is not None}


class Tracer:
    """Factory and configuration holder for traces."""

    def __init__(
        self,
        name: str = "opentrace",
        producer: Optional[Dict[str, Any]] = None,
        exporter: Optional[Exporter] = None,
        output: Optional[str] = None,
        profile: str = Profile.STREAM,
        environment: Optional[str] = None,
        execution_context: Optional[str] = None,
        resource: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.producer = producer or {"name": name}
        self.producer.setdefault("sdk", SDK_NAME)
        self.producer.setdefault("sdk_version", SDK_VERSION)
        if exporter is not None:
            self.exporter = exporter
        else:
            self.exporter = JsonlExporter(output)
        self.profile = profile
        self.environment = environment
        self.execution_context = execution_context
        self.resource = resource

    def trace(self, name: Optional[str] = None, trace_id: Optional[str] = None,
              parent_traceparent: Optional[str] = None, **header_fields) -> "Trace":
        return Trace(self, name=name or self.name, trace_id=trace_id,
                     parent_traceparent=parent_traceparent, **header_fields)

    def close(self) -> None:
        self.exporter.close()


class Trace:
    """One top-level execution. Context manager writes the header and footer."""

    def __init__(self, tracer: Tracer, name: str, trace_id: Optional[str] = None,
                 parent_traceparent: Optional[str] = None, **header_fields):
        self._tracer = tracer
        self._exporter = tracer.exporter
        self.name = name
        self._lock = threading.Lock()
        self._seq = 0
        self._started = _time.time()
        self._outcome: Optional[str] = None
        self._outputs: Any = None
        self._error: Optional[Dict[str, Any]] = None
        self._counts = {
            "llm_calls": 0, "tool_calls": 0, "agent_invocations": 0,
            "turns": 0, "handoffs": 0, "errors": 0, "retries": 0,
        }
        self._usage_total: Dict[str, int] = {}
        self._cost_total = 0.0
        self._closed = False

        self._parent_root: Optional[str] = None
        if parent_traceparent:
            parsed = _ids.parse_traceparent(parent_traceparent)
            if parsed:
                trace_id = parsed[0]
                self._parent_root = parsed[1]
        self.trace_id = trace_id or _ids.new_trace_id()
        self.session_id = header_fields.get("session_id")
        self._header_fields = header_fields

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> "Trace":
        header = {
            "ot": SPEC_VERSION,
            "type": RecordType.TRACE_START,
            "trace_id": self.trace_id,
            "seq": self._next_seq(),
            "time": _now_iso(),
            "name": self.name,
            "producer": self._tracer.producer,
            "environment": self._tracer.environment,
            "execution_context": self._tracer.execution_context,
            "resource": self._tracer.resource,
        }
        header.update(self._header_fields)
        self._emit(_clean(header))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self._error = _error_dict(exc, tb)
            self._counts["errors"] += 1
            if self._outcome is None:
                self._outcome = Outcome.FAILURE
        self.finish()
        return False

    def finish(self, outcome: Optional[str] = None, outputs: Any = None,
               summary: Optional[str] = None) -> None:
        if self._closed:
            return
        self._closed = True
        if outcome is not None:
            self._outcome = outcome
        if outputs is not None:
            self._outputs = outputs
        metrics = {
            "duration_ms": round((_time.time() - self._started) * 1000, 3),
            "total_cost": round(self._cost_total, 8) or None,
        }
        metrics.update({k: v for k, v in self._counts.items() if v})
        if self._usage_total:
            metrics["total_tokens"] = self._usage_total.get("total_tokens") or \
                (self._usage_total.get("input_tokens", 0) +
                 self._usage_total.get("output_tokens", 0))
        footer = {
            "ot": SPEC_VERSION,
            "type": RecordType.TRACE_END,
            "trace_id": self.trace_id,
            "seq": self._next_seq(),
            "time": _now_iso(),
            "outcome": self._outcome or Outcome.SUCCESS,
            "outputs": self._outputs if outputs is None else outputs,
            "summary": summary,
            "error": self._error,
            "metrics": _clean(metrics),
        }
        self._emit(_clean(footer))

    def traceparent(self) -> str:
        return _ids.format_traceparent(self.trace_id, self._parent_root or "0" * 16)

    # -- span factories ---------------------------------------------------
    def span(self, kind: str, name: Optional[str] = None,
             parent: Optional[str] = None, profile: Optional[str] = None,
             **fields) -> "Span":
        return Span(self, kind=kind, name=name,
                    parent_id=parent if parent is not None else self._parent_root,
                    profile=profile or self._tracer.profile, **fields)

    def turn(self, name: Optional[str] = None, **fields) -> "Span":
        self._counts["turns"] += 1
        return self.span(SpanKind.TURN, name=name or "turn", **fields)

    def agent(self, id: Optional[str] = None, name: Optional[str] = None,
              **fields) -> "Span":
        return self.span(SpanKind.AGENT, name=name,
                         agent=_agent_dict(id, name, fields)).with_meta(fields)

    def workflow(self, name: Optional[str] = None, **fields) -> "Span":
        return self.span(SpanKind.WORKFLOW, name=name, **fields)

    # -- internals --------------------------------------------------------
    def _next_seq(self) -> int:
        with self._lock:
            s = self._seq
            self._seq += 1
            return s

    def _emit(self, record: Dict[str, Any]) -> None:
        self._exporter.export(record)

    def _account(self, kind: str, usage: Optional[Dict[str, Any]],
                 cost: Optional[Dict[str, Any]]) -> None:
        if kind == SpanKind.LLM:
            self._counts["llm_calls"] += 1
        elif kind == SpanKind.TOOL:
            self._counts["tool_calls"] += 1
        elif kind == SpanKind.AGENT:
            self._counts["agent_invocations"] += 1
        elif kind == SpanKind.HANDOFF:
            self._counts["handoffs"] += 1
        if usage:
            for k, v in usage.items():
                if isinstance(v, (int, float)):
                    self._usage_total[k] = self._usage_total.get(k, 0) + v
        if cost and isinstance(cost.get("total"), (int, float)):
            self._cost_total += cost["total"]


class Span:
    """A unit of work. Context manager; emits per the chosen profile."""

    def __init__(self, trace: Trace, kind: str, name: Optional[str],
                 parent_id: Optional[str], profile: str, **fields):
        self._trace = trace
        self.kind = kind
        self.name = name or kind
        self.parent_id = parent_id
        self.span_id = _ids.new_span_id()
        self.trace_id = trace.trace_id
        self._profile = profile
        self._start_time = _now_iso()
        self._start_perf = _time.time()
        self._fields: Dict[str, Any] = _clean(dict(fields))
        self._output: Any = None
        self._usage: Optional[Dict[str, Any]] = None
        self._cost: Optional[Dict[str, Any]] = None
        self._status: Optional[Dict[str, Any]] = None
        self._error: Optional[Dict[str, Any]] = None
        self._metrics: Optional[Dict[str, Any]] = None
        self._token: Optional[contextvars.Token] = None
        self._closed = False
        # turn_id denormalization
        self.turn_id = fields.get("turn_id")
        if self.turn_id is None and kind == SpanKind.TURN:
            self.turn_id = self.span_id

    # -- context manager --------------------------------------------------
    def __enter__(self) -> "Span":
        self._token = _current_span.set(self)
        if self._profile == Profile.STREAM:
            rec = {
                "ot": SPEC_VERSION,
                "type": RecordType.SPAN_START,
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "parent_id": self.parent_id,
                "seq": self._trace._next_seq(),
                "time": self._start_time,
                "kind": self.kind,
                "name": self.name,
                "turn_id": self.turn_id,
                "session_id": self._trace.session_id,
            }
            rec.update(self._fields)
            self._trace._emit(_clean(rec))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.record_error(exc, tb)
        self.end()
        if self._token is not None:
            _current_span.reset(self._token)
        return False

    def end(self, status: Optional[str] = None) -> None:
        if self._closed:
            return
        self._closed = True
        if status is not None:
            self._status = {"code": status}
        if self._status is None:
            self._status = {"code": StatusCode.ERROR if self._error else StatusCode.OK}
        end_time = _now_iso()
        duration_ms = round((_time.time() - self._start_perf) * 1000, 3)
        self._trace._account(self.kind, self._usage, self._cost)

        common = {
            "ot": SPEC_VERSION,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "kind": self.kind,
            "status": self._status,
            "output": self._output,
            "usage": self._usage,
            "cost": self._cost,
            "error": self._error,
            "turn_id": self.turn_id,
        }
        if self._profile == Profile.STREAM:
            rec = dict(common)
            rec["type"] = RecordType.SPAN_END
            rec["seq"] = self._trace._next_seq()
            rec["time"] = end_time
            if self._metrics:
                rec["metrics"] = self._metrics
            self._trace._emit(_clean(rec))
        else:
            rec = dict(common)
            rec.update(self._fields)
            rec["type"] = RecordType.SPAN
            rec["parent_id"] = self.parent_id
            rec["seq"] = self._trace._next_seq()
            rec["time"] = self._start_time
            rec["name"] = self.name
            rec["start_time"] = self._start_time
            rec["end_time"] = end_time
            rec["duration_ms"] = duration_ms
            rec["session_id"] = self._trace.session_id
            if self._metrics:
                rec["metrics"] = self._metrics
            self._trace._emit(_clean(rec))

    # -- setters ----------------------------------------------------------
    def set_field(self, **kw) -> "Span":
        self._fields.update(_clean(kw))
        return self

    def with_meta(self, fields: Dict[str, Any]) -> "Span":
        for k in ("model", "provider", "framework"):
            if k in fields:
                self._fields[k] = fields[k]
        return self

    def set_input(self, messages=None, **kw) -> "Span":
        if messages is not None:
            self._fields["input"] = {"messages": messages}
        self._fields.update(_clean(kw))
        return self

    def set_output(self, messages=None, usage: Optional[Dict[str, Any]] = None,
                   cost: Optional[Dict[str, Any]] = None, response=None,
                   output: Any = None) -> "Span":
        if messages is not None:
            self._output = {"messages": messages}
        elif output is not None:
            self._output = output
        if usage is not None:
            self.set_usage(usage)
        if cost is not None:
            self._cost = cost
        if response is not None:
            self._fields["response"] = response
        return self

    def set_usage(self, usage: Dict[str, Any]) -> "Span":
        usage = dict(usage)
        if "total_tokens" not in usage and "input_tokens" in usage and "output_tokens" in usage:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        self._usage = usage
        return self

    def set_cost(self, **kw) -> "Span":
        self._cost = _clean(kw)
        return self

    def set_result(self, result: Any, is_error: bool = False) -> "Span":
        self._output = result
        self._fields["result"] = result
        if is_error:
            self._status = {"code": StatusCode.ERROR}
        return self

    def set_status(self, code: str, message: Optional[str] = None) -> "Span":
        self._status = _clean({"code": code, "message": message})
        return self

    def set_metrics(self, **kw) -> "Span":
        self._metrics = _clean(kw)
        return self

    def record_error(self, exc, tb=None) -> "Span":
        self._error = _error_dict(exc, tb)
        self._status = {"code": StatusCode.ERROR}
        self._trace._counts["errors"] += 1
        return self

    # -- child span factories --------------------------------------------
    def span(self, kind: str, name: Optional[str] = None, **fields) -> "Span":
        f = dict(fields)
        f.setdefault("turn_id", self.turn_id)
        return Span(self._trace, kind=kind, name=name, parent_id=self.span_id,
                    profile=fields.pop("profile", self._profile), **f)

    def llm(self, model: Optional[str] = None, name: Optional[str] = None,
            provider: Optional[str] = None, input=None, request=None,
            **fields) -> "Span":
        sp = self.span(SpanKind.LLM, name=name or ("chat " + model if model else "chat"),
                       model=model, provider=provider, request=request, **fields)
        if input is not None:
            sp.set_input(messages=input)
        return sp

    def tool(self, tool_name: str, arguments: Any = None,
             tool_call_id: Optional[str] = None, tool_type: str = "function",
             name: Optional[str] = None, **fields) -> "Span":
        return self.span(SpanKind.TOOL, name=name or tool_name, tool_name=tool_name,
                         tool_call_id=tool_call_id, tool_type=tool_type,
                         arguments=arguments, **fields)

    def agent(self, id: Optional[str] = None, name: Optional[str] = None,
              role: Optional[str] = None, model: Optional[str] = None,
              **fields) -> "Span":
        return self.span(SpanKind.AGENT, name=name,
                         agent=_agent_dict(id, name, dict(fields, role=role, model=model)),
                         model=model)

    def plan(self, goal: Optional[str] = None, steps=None, revision: int = 1,
             plan_id: str = "plan_1", name: Optional[str] = None, **fields) -> "Span":
        plan_obj = _clean({"id": plan_id, "goal": goal, "revision": revision,
                           "steps": steps})
        return self.span(SpanKind.PLAN, name=name or "plan", plan=plan_obj, **fields)

    def handoff(self, from_agent: str, to_agent: str, mode: str = "delegate",
                reason: Optional[str] = None, payload: Any = None,
                returns: bool = True, name: Optional[str] = None, **fields) -> "Span":
        return self.span(SpanKind.HANDOFF,
                         name=name or ("handoff -> " + to_agent),
                         from_agent=from_agent, to_agent=to_agent, mode=mode,
                         reason=reason, payload=payload, returns=returns, **fields)

    def classifier(self, name: Optional[str] = None, candidates=None, chosen=None,
                   confidence=None, scores=None, rationale=None, model=None,
                   **fields) -> "Span":
        return self.span(SpanKind.CLASSIFIER, name=name or "classifier",
                         candidates=candidates, chosen=chosen, confidence=confidence,
                         scores=scores, rationale=rationale, model=model, **fields)

    def guardrail(self, policy: str, verdict: str = "pass", categories=None,
                  score=None, name: Optional[str] = None, **fields) -> "Span":
        return self.span(SpanKind.GUARDRAIL, name=name or policy, policy=policy,
                         verdict=verdict, categories=categories, score=score, **fields)

    def retriever(self, query: Optional[str] = None, documents=None,
                  name: Optional[str] = None, **fields) -> "Span":
        return self.span(SpanKind.RETRIEVER, name=name or "retrieval", query=query,
                         documents=documents, **fields)

    def evaluator(self, name: Optional[str] = None, target_span_id=None,
                  **fields) -> "Span":
        return self.span(SpanKind.EVALUATOR, name=name or "evaluator",
                         target_span_id=target_span_id, **fields)

    def human(self, interaction: str = "approval", name: Optional[str] = None,
              **fields) -> "Span":
        return self.span(SpanKind.HUMAN, name=name or interaction,
                         interaction=interaction, **fields)

    def harness(self, harness_name: str, role: str = "harness",
                name: Optional[str] = None, **fields) -> "Span":
        return self.span(SpanKind.HARNESS, name=name or harness_name,
                         harness_name=harness_name, role=role, **fields)

    # -- point events -----------------------------------------------------
    def event(self, name: str, **fields) -> None:
        rec = {
            "ot": SPEC_VERSION,
            "type": RecordType.EVENT,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "seq": self._trace._next_seq(),
            "time": _now_iso(),
            "name": name,
            "turn_id": self.turn_id,
        }
        rec.update(_clean(fields))
        self._trace._emit(_clean(rec))

    def reasoning(self, content, signature: Optional[str] = None,
                  redacted: bool = False, effort: Optional[str] = None) -> None:
        if isinstance(content, list):
            parts = content
        else:
            parts = [_clean({"type": "reasoning", "text": content,
                             "signature": signature,
                             "redacted": True if redacted else None})]
        self.event(EventName.REASONING, content=parts, effort=effort)

    def decision(self, chosen, options=None, rationale: Optional[str] = None,
                 branch: Optional[str] = None) -> None:
        self.event(EventName.DECISION, chosen=chosen, options=options,
                   rationale=rationale, branch=branch)

    def plan_update(self, plan_id: str, step_id: str, status: str,
                    delta=None) -> None:
        self.event(EventName.PLAN_UPDATE, plan_id=plan_id, step_id=step_id,
                   status=status, delta=delta)

    def delta(self, content: str, channel: str = "text", index: int = 0) -> None:
        self.event(EventName.DELTA, content=content, channel=channel, index=index)

    def score(self, name: str, value, data_type: str = "numeric",
              comment: Optional[str] = None, target_span_id: Optional[str] = None) -> None:
        self.event(EventName.SCORE, score_name=name, value=value, data_type=data_type,
                   comment=comment, target_span_id=target_span_id or self.span_id)

    def log(self, message: str, level: str = "info", **data) -> None:
        self.event(EventName.LOG, message=message, level=level,
                   data=data or None)

    def artifact(self, artifact: Dict[str, Any]) -> None:
        self.event(EventName.ARTIFACT, artifact=artifact)


# -- helpers ---------------------------------------------------------------

def _agent_dict(id, name, fields) -> Dict[str, Any]:
    return _clean({
        "id": id,
        "name": name,
        "role": fields.get("role"),
        "model": fields.get("model"),
        "version": fields.get("version"),
        "description": fields.get("description"),
        "framework": fields.get("framework"),
    })


def _error_dict(exc, tb=None) -> Dict[str, Any]:
    d = {"type": type(exc).__name__, "message": str(exc)}
    if tb is not None:
        d["stack"] = "".join(traceback.format_tb(tb))[:4000]
    return d


def current_span() -> Optional[Span]:
    """Return the innermost active span in this context, if any."""
    return _current_span.get()
