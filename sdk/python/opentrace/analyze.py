"""In-memory analysis of an OpenTrace stream: reconstruct spans and compute rollups."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


def load(path: str) -> List[Dict[str, Any]]:
    """Load all records from a .ot.jsonl(.gz) file."""
    opener = gzip.open if path.endswith(".gz") else open
    records = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _nanos(ts: Optional[str]) -> int:
    if not ts:
        return 0
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


class TraceModel:
    """Reconstructed trace: header, footer, span tree, events."""

    def __init__(self, records: List[Dict[str, Any]]):
        self.header: Optional[Dict[str, Any]] = None
        self.footer: Optional[Dict[str, Any]] = None
        self.spans: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.trace_id: Optional[str] = None
        starts: Dict[str, Dict[str, Any]] = {}
        for r in records:
            t = r.get("type")
            self.trace_id = self.trace_id or r.get("trace_id")
            if t == "trace.start":
                self.header = r
            elif t == "trace.end":
                self.footer = r
            elif t == "span":
                self.spans[r["span_id"]] = _norm_span(r)
            elif t == "span.start":
                starts[r["span_id"]] = dict(r)
            elif t == "span.end":
                base = starts.pop(r["span_id"], {"span_id": r["span_id"], "kind": r.get("kind")})
                merged = dict(base)
                merged["start_time"] = base.get("time") or base.get("start_time")
                merged["end_time"] = r.get("time")
                for k, v in r.items():
                    if k not in ("type", "time", "seq"):
                        merged[k] = v
                self.spans[r["span_id"]] = _norm_span(merged)
            elif t == "event":
                self.events.append(r)
        for sid, s in starts.items():  # unfinished spans
            s["start_time"] = s.get("time")
            s["open"] = True
            self.spans[sid] = _norm_span(s)
        # attach events + children
        for s in self.spans.values():
            s["children"] = []
        for sid, s in self.spans.items():
            p = s.get("parent_id")
            if p and p in self.spans:
                self.spans[p]["children"].append(sid)
        self.events_by_span: Dict[str, List[Dict[str, Any]]] = {}
        for ev in self.events:
            self.events_by_span.setdefault(ev.get("span_id"), []).append(ev)

    def roots(self) -> List[str]:
        return [sid for sid, s in self.spans.items()
                if not s.get("parent_id") or s["parent_id"] not in self.spans]

    def rollup(self) -> Dict[str, Any]:
        by_kind: Dict[str, int] = {}
        tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        cost = 0.0
        errors = 0
        for s in self.spans.values():
            by_kind[s.get("kind", "?")] = by_kind.get(s.get("kind", "?"), 0) + 1
            u = s.get("usage") or {}
            for k in tokens:
                if isinstance(u.get(k), (int, float)):
                    tokens[k] += u[k]
            c = s.get("cost") or {}
            if isinstance(c.get("total"), (int, float)):
                cost += c["total"]
            if (s.get("status") or {}).get("code") == "error":
                errors += 1
        if not tokens["total_tokens"]:
            tokens["total_tokens"] = tokens["input_tokens"] + tokens["output_tokens"]
        start = _nanos(self.header.get("time")) if self.header else 0
        end = _nanos(self.footer.get("time")) if self.footer else 0
        return {
            "trace_id": self.trace_id,
            "name": self.header.get("name") if self.header else None,
            "outcome": self.footer.get("outcome") if self.footer else "incomplete",
            "spans": len(self.spans),
            "events": len(self.events),
            "by_kind": by_kind,
            "tokens": tokens,
            "cost": round(cost, 6),
            "errors": errors,
            "duration_ms": round((end - start) / 1e6, 3) if (start and end) else None,
        }


def _norm_span(s: Dict[str, Any]) -> Dict[str, Any]:
    s.setdefault("kind", "chain")
    if "start_time" not in s:
        s["start_time"] = s.get("time")
    return s


def split_traces(records: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Partition a multi-trace file into per-trace record lists."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for r in records:
        tid = r.get("trace_id")
        if tid not in buckets:
            buckets[tid] = []
            order.append(tid)
        buckets[tid].append(r)
    return [buckets[t] for t in order]
