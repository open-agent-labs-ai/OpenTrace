"""Live OpenTelemetry exporter.

Streams OpenTrace records as real OpenTelemetry spans to any OTLP endpoint, so a
trace can be viewed in real time in Jaeger, Grafana Tempo, Honeycomb, Arize Phoenix,
Datadog, etc. Requires the optional ``opentelemetry-sdk`` /
``opentelemetry-exporter-otlp`` packages.

Because OpenTrace ids are already W3C-compliant (32-hex trace / 16-hex span), they are
reused verbatim as OTel ids, so span parentage is preserved exactly.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import Exporter
from ..mapping import KIND_MAP, otel_status_code, span_otel_attributes
from ..otel_json import _iso_to_nanos


class OtelExporter(Exporter):
    """Convert OpenTrace records to OTel ReadableSpans and push them to a SpanExporter.

    Parameters
    ----------
    span_exporter:
        An ``opentelemetry.sdk.trace.export.SpanExporter``. If omitted, an
        ``OTLPSpanExporter`` is constructed from the standard ``OTEL_EXPORTER_OTLP_*``
        environment variables.
    resource:
        Optional dict of resource attributes (service.name, etc).
    """

    def __init__(self, span_exporter=None, resource: Optional[Dict[str, Any]] = None):
        try:
            from opentelemetry.sdk.resources import Resource
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "OtelExporter requires opentelemetry-sdk. Install with:\n"
                "  pip install opentelemetry-sdk opentelemetry-exporter-otlp"
            ) from exc

        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            span_exporter = OTLPSpanExporter()
        self._exporter = span_exporter
        self._resource = Resource.create(resource or {"service.name": "opentrace"})
        self._starts: Dict[str, Dict[str, Any]] = {}
        self._events: Dict[str, list] = {}

    def export(self, record: Dict[str, Any]) -> None:
        t = record.get("type")
        if t == "span":
            self._emit(record)
        elif t == "span.start":
            self._starts[record["span_id"]] = dict(record)
        elif t == "span.end":
            base = self._starts.pop(record["span_id"], {})
            merged = dict(base)
            merged["start_time"] = base.get("time")
            merged["end_time"] = record.get("time")
            for k, v in record.items():
                if k not in ("type", "time", "seq"):
                    merged[k] = v
            merged.setdefault("trace_id", record.get("trace_id"))
            merged.setdefault("span_id", record.get("span_id"))
            self._emit(merged)
        elif t == "event":
            self._events.setdefault(record.get("span_id"), []).append(record)

    def _emit(self, span: Dict[str, Any]) -> None:
        from opentelemetry.sdk.trace import ReadableSpan, Event
        from opentelemetry.trace import (
            SpanContext, TraceFlags, SpanKind as OtelSpanKind, Status, StatusCode,
        )
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope

        kind = span.get("kind", "chain")
        _op, _oi, otel_kind = KIND_MAP.get(kind, (None, "CHAIN", "INTERNAL"))
        trace_id = int(span["trace_id"], 16)
        span_id = int(span["span_id"], 16)
        ctx = SpanContext(trace_id, span_id, is_remote=False,
                          trace_flags=TraceFlags(TraceFlags.SAMPLED))
        parent = None
        if span.get("parent_id"):
            parent = SpanContext(trace_id, int(span["parent_id"], 16), is_remote=False,
                                 trace_flags=TraceFlags(TraceFlags.SAMPLED))

        status_code = otel_status_code(span.get("status"))
        st = Status(StatusCode.ERROR if status_code == "STATUS_CODE_ERROR"
                    else StatusCode.OK if status_code == "STATUS_CODE_OK"
                    else StatusCode.UNSET)

        events = []
        for ev in self._events.pop(span["span_id"], []):
            ev_attrs = {k: v for k, v in ev.items()
                        if k not in ("ot", "type", "trace_id", "span_id", "time",
                                     "seq", "parent_id")}
            events.append(Event(name=ev.get("name", "event"),
                                attributes=_stringify(ev_attrs),
                                timestamp=_iso_to_nanos(ev.get("time"))))

        readable = ReadableSpan(
            name=span.get("name", kind),
            context=ctx,
            parent=parent,
            resource=self._resource,
            attributes=_stringify(span_otel_attributes(span)),
            events=events,
            kind=getattr(OtelSpanKind, otel_kind, OtelSpanKind.INTERNAL),
            status=st,
            start_time=_iso_to_nanos(span.get("start_time") or span.get("time")),
            end_time=_iso_to_nanos(span.get("end_time") or span.get("time")),
            instrumentation_scope=InstrumentationScope("opentrace", "0.1.0"),
        )
        self._exporter.export([readable])

    def close(self) -> None:
        try:
            self._exporter.shutdown()
        except Exception:
            pass


def _stringify(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """OTLP attributes must be primitives or homogeneous lists; JSON-encode the rest."""
    import json
    out = {}
    for k, v in attrs.items():
        if isinstance(v, (str, bool, int, float)):
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float, bool)) for x in v):
            out[k] = v
        else:
            out[k] = json.dumps(v, ensure_ascii=False)
    return out
