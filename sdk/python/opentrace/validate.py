"""Lightweight, dependency-free OpenTrace validation.

If ``jsonschema`` is installed and the schema file is available, full JSON-Schema
validation is used; otherwise a structural validator enforces the core invariants
(envelope, id formats, per-type required fields, controlled vocabularies).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import ids as _ids
from .constants import RecordType, SpanKind, EventName

_SPAN_KINDS = {v for k, v in vars(SpanKind).items() if not k.startswith("_") and isinstance(v, str)}
_EVENT_NAMES = {v for k, v in vars(EventName).items() if not k.startswith("_") and isinstance(v, str)}
_TYPES = {"trace.start", "trace.end", "span.start", "span.end", "span", "event"}


class ValidationError(Exception):
    def __init__(self, message: str, line: Optional[int] = None):
        self.line = line
        super().__init__(("line %d: " % line if line else "") + message)


def validate_record(record: Dict[str, Any], line: Optional[int] = None,
                    strict: bool = False) -> List[str]:
    """Validate one record. Returns a list of problem strings (empty == valid).

    ``strict`` treats unknown enum values as errors (default: warnings only, since the
    spec requires consumers to tolerate unknown kinds/names).
    """
    problems: List[str] = []

    def err(msg: str):
        problems.append(msg)

    for field in ("ot", "type", "trace_id", "seq", "time"):
        if field not in record:
            err("missing required envelope field '%s'" % field)
    t = record.get("type")
    if t is not None and t not in _TYPES:
        err("unknown record type '%s'" % t)
    if "trace_id" in record and not _ids.is_valid_trace_id(record["trace_id"]):
        err("invalid trace_id '%s' (must be 32 lowercase hex, non-zero)" % record["trace_id"])
    if "seq" in record and not isinstance(record["seq"], int):
        err("seq must be an integer")

    if t in ("span", "span.start", "span.end", "event"):
        sid = record.get("span_id")
        if sid is None:
            err("%s requires span_id" % t)
        elif not _ids.is_valid_span_id(sid):
            err("invalid span_id '%s' (must be 16 lowercase hex, non-zero)" % sid)
    if "parent_id" in record and record["parent_id"] is not None:
        if not _ids.is_valid_span_id(record["parent_id"]):
            err("invalid parent_id '%s'" % record["parent_id"])

    if t == "trace.start":
        if "name" not in record:
            err("trace.start requires 'name'")
        if "producer" not in record or not isinstance(record["producer"], dict):
            err("trace.start requires 'producer' object")
    elif t == "trace.end":
        if record.get("outcome") not in ("success", "failure", "partial", "cancelled", "timeout"):
            err("trace.end requires valid 'outcome'")
    elif t in ("span", "span.start", "span.end"):
        kind = record.get("kind")
        if kind is None:
            err("%s requires 'kind'" % t)
        elif kind not in _SPAN_KINDS:
            if strict:
                err("unknown span kind '%s'" % kind)
            else:
                problems.append("warning: unknown span kind '%s'" % kind)
    elif t == "event":
        name = record.get("name")
        if name is None:
            err("event requires 'name'")
        elif name not in _EVENT_NAMES and strict:
            err("unknown event name '%s'" % name)

    return problems


def validate_stream(lines: Iterable[str], strict: bool = False) -> Tuple[int, List[str]]:
    """Validate an iterable of JSONL lines. Returns (record_count, problems)."""
    schema_validator = _try_jsonschema()
    problems: List[str] = []
    count = 0
    seen_header = False
    for i, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError as e:
            problems.append("line %d: invalid JSON: %s" % (i, e))
            continue
        count += 1
        if i == 1 and rec.get("type") != "trace.start":
            problems.append("line 1: first record should be trace.start")
        if rec.get("type") == "trace.start":
            seen_header = True
        if schema_validator is not None:
            for e in schema_validator(rec):
                problems.append("line %d: %s" % (i, e))
        else:
            for p in validate_record(rec, line=i, strict=strict):
                problems.append("line %d: %s" % (i, p))
    if not seen_header:
        problems.append("stream has no trace.start header")
    return count, problems


def _try_jsonschema():
    try:
        import jsonschema  # noqa
    except ImportError:
        return None
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    schema_path = os.path.join(here, "schema", "opentrace-0.1.schema.json")
    if not os.path.exists(schema_path):
        # try repo root layout
        schema_path = os.path.join(os.path.dirname(here), "schema", "opentrace-0.1.schema.json")
    if not os.path.exists(schema_path):
        return None
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(schema)

    def _validate(rec):
        return [e.message for e in validator.iter_errors(rec)]

    return _validate
