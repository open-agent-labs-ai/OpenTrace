#!/usr/bin/env python3
"""Generic OTel GenAI OTLP/JSON -> OpenTrace converter.

This is the *reverse* of the OpenTrace SDK's ``records_to_otlp`` (see
``opentrace/mapping.py`` and ``opentrace/otel_json.py``). It reads an
OTLP/HTTP-JSON ``ExportTraceServiceRequest`` document (the shape produced by
``opentrace convert <trace> --to otlp`` and by any OTLP/JSON file exporter) and
emits a valid OpenTrace v0.1 ``.ot.jsonl`` stream.

It relies only on the standard library, so it runs anywhere Python 3.8+ runs and
needs neither the OpenTelemetry SDK nor the OpenTrace SDK. If the OpenTrace SDK
is importable it is used only to validate the output when ``--validate`` is
passed.

Mapping (reverse of §17 of the OpenTrace spec):

    OTLP span attribute / field           -> OpenTrace span field
    ------------------------------------------------------------------
    opentrace.kind                        -> kind          (authoritative)
    openinference.span.kind               -> kind          (fallback)
    gen_ai.operation.name                 -> kind          (last resort)
    gen_ai.request.model                  -> model
    gen_ai.provider.name                  -> provider
    gen_ai.request.{temperature,top_p,
      top_k,max_tokens,seed}              -> request.{...}
    gen_ai.response.id                    -> response.id
    gen_ai.response.finish_reasons        -> response.finish_reasons
    gen_ai.usage.input_tokens             -> usage.input_tokens
    gen_ai.usage.output_tokens            -> usage.output_tokens
    gen_ai.usage.total_tokens             -> usage.total_tokens
    gen_ai.usage.cache_read.input_tokens  -> usage.cache_read_tokens
    gen_ai.usage.cache_creation.input_tokens -> usage.cache_creation_tokens
    gen_ai.usage.reasoning.output_tokens  -> usage.reasoning_tokens
    gen_ai.usage.cost                     -> cost.total
    gen_ai.input.messages                 -> input.messages
    gen_ai.output.messages                -> output.messages
    gen_ai.tool.name                      -> tool_name
    gen_ai.tool.call.id                   -> tool_call_id
    gen_ai.tool.type                      -> tool_type
    gen_ai.tool.call.arguments            -> arguments
    gen_ai.tool.call.result               -> result
    gen_ai.agent.{id,name}                -> agent.{id,name}
    opentrace.handoff.{from_agent,...}    -> {from_agent,...}
    opentrace.{chosen,confidence,...}     -> {chosen,confidence,...}
    opentrace.plan                        -> plan
    error.type                            -> error.type
    span events                           -> event records

Usage:
    python otlp_to_opentrace.py in.otlp.json -o out.ot.jsonl
    python otlp_to_opentrace.py in.otlp.json --validate
    cat in.otlp.json | python otlp_to_opentrace.py - -o out.ot.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

SPEC_VERSION = "0.1"

# openinference.span.kind -> OpenTrace kind. Reverse of KIND_MAP in mapping.py.
# Where the forward map is many-to-one (llm & classifier both -> LLM), we pick the
# most common OpenTrace kind; opentrace.kind (when present) always wins.
OI_KIND_TO_OT = {
    "LLM": "llm",
    "TOOL": "tool",
    "AGENT": "agent",
    "CHAIN": "chain",
    "RETRIEVER": "retriever",
    "EMBEDDING": "embedding",
    "RERANKER": "reranker",
    "GUARDRAIL": "guardrail",
    "EVALUATOR": "evaluator",
}

# gen_ai.operation.name -> OpenTrace kind (last-resort fallback).
OP_TO_OT = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "invoke_workflow": "workflow",
    "retrieval": "retriever",
    "embeddings": "embedding",
    "plan": "plan",
}

# OTel usage attribute -> OpenTrace usage key. Reverse of USAGE_MAP.
USAGE_REVERSE = {
    "gen_ai.usage.input_tokens": "input_tokens",
    "gen_ai.usage.output_tokens": "output_tokens",
    "gen_ai.usage.total_tokens": "total_tokens",
    "gen_ai.usage.cache_read.input_tokens": "cache_read_tokens",
    "gen_ai.usage.cache_creation.input_tokens": "cache_creation_tokens",
    "gen_ai.usage.reasoning.output_tokens": "reasoning_tokens",
    # tolerate the flat forms some exporters emit
    "gen_ai.usage.prompt_tokens": "input_tokens",
    "gen_ai.usage.completion_tokens": "output_tokens",
}


# --------------------------------------------------------------------------- #
# OTLP value/attribute decoding
# --------------------------------------------------------------------------- #

def _decode_value(v: Dict[str, Any]) -> Any:
    """Decode a single OTLP AnyValue into a Python value."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "arrayValue" in v:
        return [_decode_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: _decode_value(kv.get("value", {}))
                for kv in v["kvlistValue"].get("values", [])}
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def _decode_attrs(attrs: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for kv in attrs or []:
        out[kv.get("key")] = _decode_value(kv.get("value", {}))
    return out


def _maybe_json(value: Any) -> Any:
    """gen_ai content attributes are JSON-encoded strings on the way out; decode
    them back to structured data when possible, else return the raw value."""
    if isinstance(value, str):
        s = value.strip()
        if s and s[0] in "[{" and s[-1] in "]}":
            try:
                return json.loads(s)
            except (ValueError, TypeError):
                return value
    return value


def _nanos_to_iso(nanos: Any) -> Optional[str]:
    try:
        n = int(nanos)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    dt = datetime.fromtimestamp(n / 1_000_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# --------------------------------------------------------------------------- #
# Span attribute -> OpenTrace span field mapping
# --------------------------------------------------------------------------- #

def _kind_from_attrs(attrs: Dict[str, Any]) -> str:
    if attrs.get("opentrace.kind"):
        return attrs["opentrace.kind"]
    oi = attrs.get("openinference.span.kind")
    if oi and oi in OI_KIND_TO_OT:
        return OI_KIND_TO_OT[oi]
    op = attrs.get("gen_ai.operation.name")
    if op and op in OP_TO_OT:
        return OP_TO_OT[op]
    return "chain"


def _status_from_otlp(status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    code = (status or {}).get("code")
    if code in ("STATUS_CODE_OK", "OK", 1):
        return {"code": "ok"}
    if code in ("STATUS_CODE_ERROR", "ERROR", 2):
        return {"code": "error"}
    return {"code": "ok"}


def otlp_span_to_ot(sp: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one OTLP span dict into an OpenTrace compact ``span`` record."""
    attrs = _decode_attrs(sp.get("attributes"))
    kind = _kind_from_attrs(attrs)

    rec: Dict[str, Any] = {
        "ot": SPEC_VERSION,
        "type": "span",
        "trace_id": sp.get("traceId") or sp.get("trace_id"),
        "span_id": sp.get("spanId") or sp.get("span_id"),
        "parent_id": sp.get("parentSpanId") or sp.get("parent_id"),
        "kind": kind,
        "name": sp.get("name") or kind,
        "start_time": _nanos_to_iso(sp.get("startTimeUnixNano")),
        "end_time": _nanos_to_iso(sp.get("endTimeUnixNano")),
        "status": _status_from_otlp(sp.get("status")),
    }

    # model / provider
    if attrs.get("gen_ai.request.model"):
        rec["model"] = attrs["gen_ai.request.model"]
    if attrs.get("gen_ai.provider.name"):
        rec["provider"] = attrs["gen_ai.provider.name"]

    # request params
    request: Dict[str, Any] = {}
    for k in ("temperature", "top_p", "top_k", "max_tokens", "seed"):
        ak = "gen_ai.request." + k
        if attrs.get(ak) is not None:
            request[k] = attrs[ak]
    if request:
        rec["request"] = request

    # response
    response: Dict[str, Any] = {}
    if attrs.get("gen_ai.response.id"):
        response["id"] = attrs["gen_ai.response.id"]
    if attrs.get("gen_ai.response.finish_reasons") is not None:
        response["finish_reasons"] = _maybe_json(attrs["gen_ai.response.finish_reasons"])
    if response:
        rec["response"] = response

    # usage
    usage: Dict[str, Any] = {}
    for ak, av in attrs.items():
        if ak in USAGE_REVERSE:
            usage[USAGE_REVERSE[ak]] = av
        elif ak.startswith("gen_ai.usage.") and ak != "gen_ai.usage.cost":
            # unknown usage.* -> keep the tail as the key
            usage.setdefault(ak[len("gen_ai.usage."):], av)
    if usage:
        rec["usage"] = usage

    # cost
    if attrs.get("gen_ai.usage.cost") is not None:
        rec["cost"] = {"total": attrs["gen_ai.usage.cost"], "currency": "USD"}

    # content messages
    if attrs.get("gen_ai.input.messages") is not None:
        rec["input"] = {"messages": _maybe_json(attrs["gen_ai.input.messages"])}
    if attrs.get("gen_ai.output.messages") is not None:
        rec["output"] = {"messages": _maybe_json(attrs["gen_ai.output.messages"])}

    # tool fields
    if attrs.get("gen_ai.tool.name"):
        rec["tool_name"] = attrs["gen_ai.tool.name"]
    if attrs.get("gen_ai.tool.call.id"):
        rec["tool_call_id"] = attrs["gen_ai.tool.call.id"]
    if attrs.get("gen_ai.tool.type"):
        rec["tool_type"] = attrs["gen_ai.tool.type"]
    if attrs.get("gen_ai.tool.call.arguments") is not None:
        rec["arguments"] = _maybe_json(attrs["gen_ai.tool.call.arguments"])
    if attrs.get("gen_ai.tool.call.result") is not None:
        rec["result"] = _maybe_json(attrs["gen_ai.tool.call.result"])

    # agent
    agent: Dict[str, Any] = {}
    if attrs.get("gen_ai.agent.id"):
        agent["id"] = attrs["gen_ai.agent.id"]
    if attrs.get("gen_ai.agent.name"):
        agent["name"] = attrs["gen_ai.agent.name"]
    if agent:
        rec["agent"] = agent

    # handoff
    for k in ("from_agent", "to_agent", "mode", "reason"):
        ak = "opentrace.handoff." + k
        if attrs.get(ak) is not None:
            rec[k] = attrs[ak]

    # classifier / guardrail extras
    for k in ("chosen", "confidence", "rationale", "policy", "verdict"):
        ak = "opentrace." + k
        if attrs.get(ak) is not None:
            rec[k] = _maybe_json(attrs[ak])

    # plan
    if attrs.get("opentrace.plan") is not None:
        rec["plan"] = _maybe_json(attrs["opentrace.plan"])

    # error
    if attrs.get("error.type"):
        rec["error"] = {"type": attrs["error.type"],
                        "message": attrs.get("error.message", "")}

    return _clean(rec)


def otlp_event_to_ot(ev: Dict[str, Any], trace_id: str, span_id: str,
                     seq: int) -> Dict[str, Any]:
    attrs = _decode_attrs(ev.get("attributes"))
    rec: Dict[str, Any] = {
        "ot": SPEC_VERSION,
        "type": "event",
        "trace_id": trace_id,
        "span_id": span_id,
        "seq": seq,
        "time": _nanos_to_iso(ev.get("timeUnixNano")),
        "name": ev.get("name", "event"),
    }
    for k, v in attrs.items():
        rec.setdefault(k, _maybe_json(v))
    return _clean(rec)


# --------------------------------------------------------------------------- #
# Document iteration
# --------------------------------------------------------------------------- #

def iter_otlp_spans(doc: Dict[str, Any]):
    """Yield (span_dict, resource_attrs) for every span in an OTLP document."""
    for rs in doc.get("resourceSpans", []) or []:
        res_attrs = _decode_attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])) or []:
            for sp in ss.get("spans", []) or []:
                yield sp, res_attrs


# OTLP span-level events are attached to their converted record via an identity
# index, so that otlp_span_to_ot stays a pure span->record function.
_span_events_index: Dict[int, List[Dict[str, Any]]] = {}


def otlp_to_records(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert a full OTLP document to a list of OpenTrace records, grouped by
    trace with a synthesized ``trace.start`` / ``trace.end`` per trace and any
    span-level OTLP events converted to OpenTrace ``event`` records. This is the
    entry point the CLI uses."""
    _span_events_index.clear()
    # Build (span, converted-record) pairs so events can be indexed.
    by_trace: Dict[str, List[Dict[str, Any]]] = {}
    res_by_trace: Dict[str, Dict[str, Any]] = {}
    conv_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for sp, res in iter_otlp_spans(doc):
        tid = sp.get("traceId") or "unknown"
        rec = otlp_span_to_ot(sp)
        by_trace.setdefault(tid, []).append(sp)
        conv_by_trace.setdefault(tid, []).append(rec)
        res_by_trace.setdefault(tid, res)
        if sp.get("events"):
            _span_events_index[id(rec)] = sp["events"]

    records: List[Dict[str, Any]] = []
    for tid, recs in conv_by_trace.items():
        res = res_by_trace.get(tid, {})
        service = res.get("service.name", "otel-import")
        recs = sorted(recs, key=lambda r: r.get("start_time") or "")
        starts = [r.get("start_time") for r in recs if r.get("start_time")]
        ends = [r.get("end_time") for r in recs if r.get("end_time")]
        seq = 0

        def next_seq(_box=[0]):
            _box[0] += 1
            return _box[0] - 1

        records.append(_clean({
            "ot": SPEC_VERSION, "type": "trace.start", "trace_id": tid,
            "seq": next_seq(), "time": min(starts) if starts else None,
            "name": service,
            "producer": {"name": "otlp_to_opentrace", "sdk": "opentrace-otel-ingest"},
            "resource": res or None,
        }))
        any_error = False
        for r in recs:
            r["seq"] = next_seq()
            r["time"] = r.get("start_time")
            if (r.get("status") or {}).get("code") == "error":
                any_error = True
            records.append(r)
            for ev in _span_events_index.get(id(r), []):
                records.append(otlp_event_to_ot(ev, tid, r["span_id"], next_seq()))
        records.append(_clean({
            "ot": SPEC_VERSION, "type": "trace.end", "trace_id": tid,
            "seq": next_seq(),
            "time": max(ends) if ends else (max(starts) if starts else None),
            "outcome": "failure" if any_error else "success",
        }))
    return records


def convert_file(path: str) -> List[Dict[str, Any]]:
    raw = sys.stdin.read() if path == "-" else open(path, "r", encoding="utf-8").read()
    doc = json.loads(raw)
    # Support both a single ExportTraceServiceRequest and a JSON-lines file of them.
    if isinstance(doc, dict) and "resourceSpans" in doc:
        return otlp_to_records(doc)
    # JSON-lines: merge every line's resourceSpans.
    merged = {"resourceSpans": []}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        merged["resourceSpans"].extend(d.get("resourceSpans", []))
    return otlp_to_records(merged)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Convert OTel GenAI OTLP/JSON to OpenTrace")
    ap.add_argument("input", help="OTLP/JSON file (ExportTraceServiceRequest) or - for stdin")
    ap.add_argument("-o", "--output", help="output .ot.jsonl path (default stdout)")
    ap.add_argument("--validate", action="store_true",
                    help="validate output with the OpenTrace SDK if importable")
    args = ap.parse_args(argv)

    records = convert_file(args.input)
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    text = "\n".join(lines) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("wrote %d records to %s" % (len(records), args.output), file=sys.stderr)
    else:
        sys.stdout.write(text)

    if args.validate:
        try:
            from opentrace import validate_stream
        except ImportError:
            print("opentrace SDK not importable; skipping validation", file=sys.stderr)
            return 0
        count, problems = validate_stream(iter(lines))
        errs = [p for p in problems if "warning" not in p]
        for p in problems:
            print(("ERROR " if "warning" not in p else "WARN  ") + p, file=sys.stderr)
        print("validated %d records, %d error(s)" % (count, len(errs)), file=sys.stderr)
        return 1 if errs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
