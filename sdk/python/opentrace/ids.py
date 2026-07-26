"""W3C Trace Context compatible identifier generation for OpenTrace.

trace_id : 16 bytes -> 32 lowercase hex chars
span_id  :  8 bytes -> 16 lowercase hex chars

These are byte-for-byte compatible with OpenTelemetry, so OpenTrace IDs can be
handed straight to an OTLP exporter.
"""
from __future__ import annotations

import os
import re

_TRACE_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_RE = re.compile(r"^[0-9a-f]{16}$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


def new_trace_id() -> str:
    """Return a random, non-zero 32-hex trace id."""
    while True:
        tid = os.urandom(16).hex()
        if tid != _ZERO_TRACE:
            return tid


def new_span_id() -> str:
    """Return a random, non-zero 16-hex span id."""
    while True:
        sid = os.urandom(8).hex()
        if sid != _ZERO_SPAN:
            return sid


def is_valid_trace_id(value: str) -> bool:
    return bool(_TRACE_RE.match(value)) and value != _ZERO_TRACE


def is_valid_span_id(value: str) -> bool:
    return bool(_SPAN_RE.match(value)) and value != _ZERO_SPAN


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """Build a W3C ``traceparent`` header for cross-process propagation."""
    flags = "01" if sampled else "00"
    return "00-{}-{}-{}".format(trace_id, span_id, flags)


def parse_traceparent(header: str):
    """Parse a ``traceparent`` header. Returns (trace_id, parent_span_id, sampled) or None."""
    parts = header.strip().split("-")
    if len(parts) != 4:
        return None
    _version, trace_id, parent_id, flags = parts
    if not is_valid_trace_id(trace_id) or not is_valid_span_id(parent_id):
        return None
    sampled = bool(int(flags, 16) & 0x01)
    return trace_id, parent_id, sampled
