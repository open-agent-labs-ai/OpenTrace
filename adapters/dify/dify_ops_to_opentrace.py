#!/usr/bin/env python3
"""Dify ops-trace JSON -> OpenTrace (best-effort).

Dify's ops-tracing layer builds ``*TraceInfo`` objects (see Dify's
``core/ops/entities`` / ``TraceTaskName``: ``workflow``, ``message``, ``tool``,
``node_execution``, ``dataset_retrieval``, ``moderation``). Those are internal
Python objects, not a stable public export; this converter accepts a **JSON
dump of a workflow trace** in the documented shape below and emits OpenTrace
v0.1.

For most deployments the simpler path is Dify's generic OTLP exporter
(``ENABLE_OTEL=true``) piped through ``../opentelemetry-ingest/`` — see
README.md. Use this converter when you have Dify's node-level trace info (e.g.
captured from a custom ops trace provider) and want the Dify-native node
structure.

Expected input (either a single object or a list of them):

    {
      "trace_task": "workflow",              # TraceTaskName
      "workflow_run_id": "run-123",
      "workflow_id": "wf-1",
      "message_id": "msg-1",                 # optional
      "conversation_id": "conv-1",           # optional -> session_id
      "workflow_name": "My Flow",
      "start_time": "2026-07-25T10:00:00Z",
      "end_time":   "2026-07-25T10:00:05Z",
      "error": null,
      "node_executions": [
        {
          "node_execution_id": "ne-1",
          "node_id": "llm-1",
          "node_type": "llm",               # llm|tool|knowledge-retrieval|agent|code|...
          "title": "Answer",
          "status": "succeeded",            # succeeded|failed|running
          "inputs": {...}, "outputs": {...},
          "created_at": "2026-07-25T10:00:01Z",
          "finished_at": "2026-07-25T10:00:03Z",
          "execution_metadata": {           # LLM nodes carry token counts here
            "total_tokens": 320, "prompt_tokens": 250, "completion_tokens": 70,
            "total_price": 0.0021, "currency": "USD"
          }
        }
      ]
    }

Mapping:
    trace_task=workflow           -> workflow span (root, under a turn)
    node_type=llm                 -> llm span (+ usage/cost from execution_metadata)
    node_type in {tool}           -> tool span
    node_type in {knowledge-retrieval, dataset-retrieval}
                                  -> retriever span
    node_type=agent               -> agent span
    everything else               -> chain span
    dataset_retrieval trace_task  -> retriever span

Usage:
    python dify_ops_to_opentrace.py dify_trace.json -o dify.ot.jsonl [--validate]
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid as _uuidlib
from typing import Any, Dict, List, Optional

SPEC_VERSION = "0.1"

# Dify node_type -> OpenTrace span kind.
NODE_KIND = {
    "llm": "llm",
    "agent": "agent",
    "tool": "tool",
    "knowledge-retrieval": "retriever",
    "dataset-retrieval": "retriever",
    "dataset_retrieval": "retriever",
    "question-classifier": "classifier",
    "parameter-extractor": "llm",
    "code": "chain",
    "template-transform": "chain",
    "http-request": "tool",
    "if-else": "chain",
    "iteration": "chain",
    "answer": "chain",
    "start": "chain",
    "end": "chain",
    "variable-aggregator": "chain",
    "moderation": "guardrail",
}


def _sid() -> str:
    return _uuidlib.uuid4().hex[:16]


def _tid() -> str:
    return _uuidlib.uuid4().hex


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class Emitter:
    def __init__(self, trace_id: str, session_id: Optional[str]):
        self.trace_id = trace_id
        self.session_id = session_id
        self.seq = 0
        self.records: List[Dict[str, Any]] = []

    def _next(self) -> int:
        s = self.seq
        self.seq += 1
        return s

    def span(self, kind: str, name: str, parent: Optional[str],
             start: Optional[str], end: Optional[str], status: str = "ok",
             **fields) -> str:
        span_id = _sid()
        rec = _clean({
            "ot": SPEC_VERSION, "type": "span", "trace_id": self.trace_id,
            "span_id": span_id, "parent_id": parent, "seq": self._next(),
            "time": start, "kind": kind, "name": name, "start_time": start,
            "end_time": end or start, "session_id": self.session_id,
            "status": {"code": status},
        })
        rec.update(_clean(fields))
        self.records.append(rec)
        return span_id

    def header(self, name: str, time: Optional[str]) -> None:
        self.records.append(_clean({
            "ot": SPEC_VERSION, "type": "trace.start", "trace_id": self.trace_id,
            "seq": self._next(), "time": time, "name": name,
            "session_id": self.session_id,
            "producer": {"name": "dify", "sdk": "opentrace-dify-adapter"},
        }))

    def footer(self, time: Optional[str], outcome: str) -> None:
        self.records.append(_clean({
            "ot": SPEC_VERSION, "type": "trace.end", "trace_id": self.trace_id,
            "seq": self._next(), "time": time, "outcome": outcome,
        }))


def _usage_and_cost(meta: Dict[str, Any]):
    if not isinstance(meta, dict):
        return None, None
    usage = _clean({
        "input_tokens": meta.get("prompt_tokens"),
        "output_tokens": meta.get("completion_tokens"),
        "total_tokens": meta.get("total_tokens"),
    }) or None
    cost = None
    total_price = meta.get("total_price") or meta.get("price")
    if total_price is not None:
        cost = {"total": float(total_price), "currency": meta.get("currency", "USD")}
    return usage, cost


def _node_status(status: Optional[str]) -> str:
    if status in ("failed", "error", "exception"):
        return "error"
    return "ok"


def convert_workflow(trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    session_id = trace.get("conversation_id") or trace.get("message_id")
    em = Emitter(_tid(), session_id)
    start = trace.get("start_time")
    end = trace.get("end_time")
    wf_name = trace.get("workflow_name") or trace.get("workflow_id") or "workflow"
    em.header(name="dify:" + str(wf_name), time=start)

    turn = em.span("turn", "turn", None, start, end)
    outcome = "failure" if trace.get("error") else "success"

    task = trace.get("trace_task", "workflow")
    if task in ("dataset_retrieval", "dataset-retrieval"):
        # a standalone retrieval trace
        em.span("retriever", "dataset_retrieval", turn, start, end,
                query=_stringify(trace.get("query") or trace.get("inputs")),
                documents=_docs(trace.get("documents") or trace.get("outputs")))
        em.footer(end, outcome)
        return em.records

    # workflow span holding node executions
    wf = em.span("workflow", str(wf_name), turn, start, end,
                 status=_node_status("failed" if trace.get("error") else "succeeded"))

    for node in trace.get("node_executions", []) or []:
        _emit_node(em, node, wf)

    em.footer(end, outcome)
    return em.records


def _emit_node(em: Emitter, node: Dict[str, Any], parent: str) -> None:
    node_type = (node.get("node_type") or "").lower()
    kind = NODE_KIND.get(node_type, "chain")
    name = node.get("title") or node.get("node_id") or node_type or "node"
    start = node.get("created_at") or node.get("start_time")
    end = node.get("finished_at") or node.get("end_time")
    status = _node_status(node.get("status"))
    meta = node.get("execution_metadata") or {}
    usage, cost = _usage_and_cost(meta)

    fields: Dict[str, Any] = {}
    if kind == "llm":
        fields["model"] = meta.get("model") or (node.get("process_data") or {}).get("model_name")
        fields["provider"] = meta.get("model_provider") or (node.get("process_data") or {}).get("model_provider")
        fields["usage"] = usage
        fields["cost"] = cost
        inp = node.get("inputs") or (node.get("process_data") or {}).get("prompts")
        if inp is not None:
            fields["input"] = {"messages": _to_messages(inp)}
        out = node.get("outputs")
        if out is not None:
            fields["output"] = {"messages": _to_messages(out, role="assistant")}
    elif kind == "tool":
        fields["tool_name"] = node.get("title") or node_type
        fields["arguments"] = node.get("inputs")
        fields["result"] = node.get("outputs")
    elif kind == "retriever":
        fields["query"] = _stringify(node.get("inputs"))
        fields["documents"] = _docs(node.get("outputs"))
    elif kind == "classifier":
        outputs = node.get("outputs") or {}
        fields["chosen"] = outputs.get("class_name") or outputs.get("class")
    else:
        # generic chain node: keep inputs/outputs as fields for reference
        if node.get("outputs") is not None:
            fields["output"] = node.get("outputs")

    em.span(kind, name, parent, start, end, status=status, **fields)


# ---- helpers -------------------------------------------------------------- #

def _to_messages(val: Any, role: str = "user") -> List[Dict[str, Any]]:
    if isinstance(val, list) and val and isinstance(val[0], dict) and "role" in val[0]:
        # already message-shaped (e.g. prompt list)
        out = []
        for m in val:
            content = m.get("text") or m.get("content") or ""
            out.append({"role": m.get("role", role),
                        "parts": [{"type": "text", "text": str(content)}]})
        return out
    return [{"role": role, "parts": [{"type": "text", "text": _stringify(val) or ""}]}]


def _docs(val: Any) -> Optional[List[Dict[str, Any]]]:
    if val is None:
        return None
    items = val
    if isinstance(val, dict):
        items = val.get("result") or val.get("documents") or val.get("retrieval") or []
    if not isinstance(items, list):
        return None
    docs = []
    for it in items:
        if isinstance(it, dict):
            docs.append(_clean({
                "id": it.get("id") or it.get("document_id") or it.get("segment_id"),
                "score": it.get("score"),
                "text": (it.get("content") or it.get("text") or "")[:500] or None,
            }))
    return docs or None


def _stringify(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("query", "text", "input", "question"):
            if k in v:
                return str(v[k])
    return json.dumps(v, ensure_ascii=False)[:1000]


def convert(doc: Any) -> List[Dict[str, Any]]:
    if isinstance(doc, list):
        records: List[Dict[str, Any]] = []
        for t in doc:
            records.extend(convert_workflow(t))
        return records
    return convert_workflow(doc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Convert Dify ops-trace JSON to OpenTrace")
    ap.add_argument("input", help="Dify trace JSON file, or - for stdin")
    ap.add_argument("-o", "--output")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args(argv)

    raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    doc = json.loads(raw)
    records = convert(doc)
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
