"""Dependency-free conversion of an OpenTrace stream to OTLP/JSON.

Produces an ``ExportTraceServiceRequest`` JSON object (the OTLP-HTTP/JSON body),
which can be POSTed to any OTLP/HTTP traces endpoint (``/v1/traces``) or inspected
directly. This path needs no third-party packages, so it is fully testable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .mapping import KIND_MAP, otel_status_code, span_otel_attributes


def _iso_to_nanos(ts: Optional[str]) -> int:
    if not ts:
        return 0
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return int((dt - epoch).total_seconds() * 1_000_000_000)


def _kv(key: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    elif isinstance(value, (list, dict)):
        import json
        v = {"stringValue": json.dumps(value, ensure_ascii=False)}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def _attrs(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [_kv(k, v) for k, v in d.items()]


def records_to_otlp(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert OpenTrace records into a single OTLP ExportTraceServiceRequest dict."""
    records = list(records)

    # Reconstruct spans from compact `span` and streaming `span.start`/`span.end`.
    starts: Dict[str, Dict[str, Any]] = {}
    spans: Dict[str, Dict[str, Any]] = {}
    events_by_span: Dict[str, List[Dict[str, Any]]] = {}
    resource: Dict[str, Any] = {"service.name": "opentrace"}
    producer_name = "opentrace"

    for r in records:
        t = r.get("type")
        if t == "trace.start":
            res = r.get("resource") or {}
            if res:
                resource = dict(res)
            producer_name = (r.get("producer") or {}).get("name", producer_name)
            resource.setdefault("service.name", producer_name)
        elif t == "span":
            spans[r["span_id"]] = r
        elif t == "span.start":
            starts[r["span_id"]] = dict(r)
        elif t == "span.end":
            base = starts.pop(r["span_id"], {"span_id": r["span_id"],
                                             "trace_id": r["trace_id"],
                                             "kind": r.get("kind", "chain")})
            merged = dict(base)
            merged["start_time"] = base.get("time") or base.get("start_time")
            merged["end_time"] = r.get("time")
            for k, v in r.items():
                if k not in ("type", "time", "seq"):
                    merged[k] = v
            spans[r["span_id"]] = merged
        elif t == "event":
            events_by_span.setdefault(r.get("span_id"), []).append(r)

    # Any spans still open (no end seen) -> treat as unfinished, emit with start==end.
    for sid, s in starts.items():
        s.setdefault("start_time", s.get("time"))
        s.setdefault("end_time", s.get("time"))
        spans[sid] = s

    otlp_spans: List[Dict[str, Any]] = []
    for sid, s in spans.items():
        kind = s.get("kind", "chain")
        _op, _oi, otel_kind = KIND_MAP.get(kind, (None, "CHAIN", "INTERNAL"))
        start = _iso_to_nanos(s.get("start_time") or s.get("time"))
        end = _iso_to_nanos(s.get("end_time") or s.get("start_time") or s.get("time"))
        span_events = []
        for ev in events_by_span.get(sid, []):
            ev_attrs = {k: v for k, v in ev.items()
                        if k not in ("ot", "type", "trace_id", "span_id", "time",
                                     "seq", "name", "parent_id")}
            span_events.append({
                "timeUnixNano": str(_iso_to_nanos(ev.get("time"))),
                "name": ev.get("name", "event"),
                "attributes": _attrs({k: v for k, v in ev_attrs.items()}),
            })
        otlp_span: Dict[str, Any] = {
            "traceId": s["trace_id"],
            "spanId": sid,
            "name": s.get("name", kind),
            "kind": "SPAN_KIND_" + otel_kind,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": _attrs(span_otel_attributes(s)),
            "status": {"code": otel_status_code(s.get("status"))},
        }
        if s.get("parent_id"):
            otlp_span["parentSpanId"] = s["parent_id"]
        if span_events:
            otlp_span["events"] = span_events
        otlp_spans.append(otlp_span)

    return {
        "resourceSpans": [{
            "resource": {"attributes": _attrs(resource)},
            "scopeSpans": [{
                "scope": {"name": "opentrace", "version": "0.1.0"},
                "spans": otlp_spans,
            }],
        }]
    }
