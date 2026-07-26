"""OpenTrace Python SDK — capture and write OpenTrace v0.1 agent traces.

See https://openagentlabs.ai/opentrace and the SPECIFICATION.md in this repo.
"""
from __future__ import annotations

from .constants import (
    SPEC_VERSION, SDK_NAME, SDK_VERSION, REDACTED,
    RecordType, SpanKind, EventName, Outcome, StatusCode, Profile,
)
from .tracer import Tracer, Trace, Span, current_span
from .exporters.base import Exporter, MultiExporter, MemoryExporter
from .exporters.jsonl import JsonlExporter
from . import content
from . import ids
from .otel_json import records_to_otlp
from .validate import validate_record, validate_stream, ValidationError

__all__ = [
    "SPEC_VERSION", "SDK_NAME", "SDK_VERSION", "REDACTED",
    "RecordType", "SpanKind", "EventName", "Outcome", "StatusCode", "Profile",
    "Tracer", "Trace", "Span", "current_span",
    "Exporter", "MultiExporter", "MemoryExporter", "JsonlExporter",
    "content", "ids", "records_to_otlp",
    "validate_record", "validate_stream", "ValidationError",
]


def OtelExporter(*args, **kwargs):
    """Lazily-imported OTel exporter (needs opentelemetry-sdk)."""
    from .exporters.otel import OtelExporter as _OtelExporter
    return _OtelExporter(*args, **kwargs)
