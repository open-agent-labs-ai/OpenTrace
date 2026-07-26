"""Tests for the OpenTrace Python SDK. Run: pytest sdk/python/tests"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import opentrace as ot
from opentrace import content as C, ids
from opentrace.analyze import TraceModel
from opentrace.otel_json import records_to_otlp


def build(profile=ot.Profile.STREAM):
    exp = ot.MemoryExporter()
    tr = ot.Tracer(name="t", producer={"name": "t", "version": "1"}, exporter=exp, profile=profile)
    with tr.trace(name="run", session_id="s1") as t:
        with t.turn(input=[C.user("hi")]) as turn:
            with turn.agent(id="main", name="A", role="orchestrator") as ag:
                with ag.plan(goal="g", steps=[{"id": "s1", "description": "d", "status": "pending"}]):
                    pass
                with ag.llm(model="anthropic/claude-opus-4-8", provider="anthropic",
                            input=[C.user("hi")]) as call:
                    call.reasoning("thinking", signature="x")
                    call.set_output([C.assistant([C.tool_call("c1", "f", {"a": 1})],
                                                 finish_reason="tool_call")],
                                    usage={"input_tokens": 10, "output_tokens": 5},
                                    cost={"total": 0.01, "currency": "USD"})
                with ag.tool("f", arguments={"a": 1}, tool_call_id="c1") as tool:
                    tool.set_result({"ok": True})
                with ag.handoff(from_agent="main", to_agent="sub", mode="delegate") as h:
                    with h.agent(id="sub", name="Sub", role="subagent") as sub:
                        with sub.guardrail(policy="p", verdict="pass"):
                            pass
                        sub.score("q", 1, data_type="boolean")
    return exp.records


def test_ids_are_w3c():
    assert ids.is_valid_trace_id(ids.new_trace_id())
    assert ids.is_valid_span_id(ids.new_span_id())
    assert not ids.is_valid_trace_id("0" * 32)
    assert not ids.is_valid_span_id("xyz")
    tp = ids.format_traceparent("a" * 32, "b" * 16)
    parsed = ids.parse_traceparent(tp)
    assert parsed == ("a" * 32, "b" * 16, True)


def test_stream_profile_valid():
    recs = build(ot.Profile.STREAM)
    count, problems = ot.validate_stream([json.dumps(r) for r in recs], strict=False)
    errors = [p for p in problems if "warning" not in p]
    assert not errors, errors
    assert recs[0]["type"] == "trace.start"
    assert recs[-1]["type"] == "trace.end"
    # every span.start has a matching span.end
    starts = {r["span_id"] for r in recs if r["type"] == "span.start"}
    ends = {r["span_id"] for r in recs if r["type"] == "span.end"}
    assert starts == ends


def test_batch_profile_valid():
    recs = build(ot.Profile.BATCH)
    count, problems = ot.validate_stream([json.dumps(r) for r in recs], strict=False)
    errors = [p for p in problems if "warning" not in p]
    assert not errors, errors
    assert not any(r["type"] in ("span.start", "span.end") for r in recs)
    assert any(r["type"] == "span" for r in recs)


def test_footer_accounting():
    recs = build()
    footer = recs[-1]
    m = footer["metrics"]
    assert m["llm_calls"] == 1
    assert m["tool_calls"] == 1
    assert m["agent_invocations"] == 2   # main + sub
    assert m["handoffs"] == 1
    assert m["turns"] == 1
    assert footer["outcome"] == "success"


def test_ids_unique_and_parented():
    recs = build(ot.Profile.BATCH)
    model = TraceModel(recs)
    # tool call correlation preserved
    tool = [s for s in model.spans.values() if s["kind"] == "tool"][0]
    assert tool["tool_call_id"] == "c1"
    # handoff typed
    handoff = [s for s in model.spans.values() if s["kind"] == "handoff"][0]
    assert handoff["from_agent"] == "main" and handoff["to_agent"] == "sub"
    roll = model.rollup()
    assert roll["tokens"]["total_tokens"] == 15


def test_error_capture():
    exp = ot.MemoryExporter()
    tr = ot.Tracer(name="t", producer={"name": "t"}, exporter=exp)
    try:
        with tr.trace(name="boom") as t:
            with t.span("tool", name="x") as s:
                raise ValueError("kaboom")
    except ValueError:
        pass
    span_end = [r for r in exp.records if r["type"] == "span.end"][0]
    assert span_end["status"]["code"] == "error"
    assert span_end["error"]["type"] == "ValueError"
    footer = exp.records[-1]
    assert footer["outcome"] == "failure"


def test_otel_conversion():
    recs = build(ot.Profile.BATCH)
    otlp = records_to_otlp(recs)
    spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
    # turn, agent(main), plan, llm, tool, handoff, agent(sub), guardrail
    assert len(spans) == 8, len(spans)
    # find llm span, check gen_ai attrs
    llm = [s for s in spans if s["name"].startswith("chat")][0]
    keys = {a["key"] for a in llm["attributes"]}
    assert "gen_ai.request.model" in keys
    assert "gen_ai.usage.input_tokens" in keys
    assert "openinference.span.kind" in keys
    # ids preserved
    assert all(len(s["traceId"]) == 32 and len(s["spanId"]) == 16 for s in spans)


def test_redaction_sentinel():
    part = C.reasoning("secret", redacted=True)
    assert part["text"] == ot.REDACTED and part["redacted"] is True


def test_live_otel_exporter():
    """Exercise the real OTel SDK path if opentelemetry-sdk is installed (else skip)."""
    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
    except ImportError:
        print("SKIP test_live_otel_exporter (opentelemetry-sdk not installed)")
        return
    sink = InMemorySpanExporter()
    tracer = ot.Tracer(name="live", producer={"name": "t"},
                       exporter=ot.OtelExporter(span_exporter=sink))
    with tracer.trace(name="run") as t:
        with t.span("llm", model="anthropic/claude-opus-4-8") as s:
            s.reasoning("thinking")
            s.set_output([C.assistant("hi")], usage={"input_tokens": 10, "output_tokens": 5})
    tracer.close()
    spans = sink.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert format(span.context.span_id, "016x")  # valid hex id
    assert span.attributes.get("gen_ai.request.model") == "anthropic/claude-opus-4-8"
    assert span.attributes.get("gen_ai.usage.input_tokens") == 10
    assert [e.name for e in span.events] == ["reasoning"]


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL", fn.__name__, "->", e)
            traceback.print_exc()
    print("\n%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
