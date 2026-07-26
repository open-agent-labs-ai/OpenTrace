#!/usr/bin/env python3
"""Convert a Claude Code session transcript (.jsonl) to an OpenTrace .ot.jsonl.

Claude Code stores a transcript per session at
``~/.claude/projects/<project-slug>/<session-id>.jsonl``. Each line is one entry:

    {"type":"user|assistant|system", "uuid":..., "parentUuid":...,
     "timestamp":"2026-...Z", "isSidechain":false, "parentToolUseId":null,
     "message":{"role":..., "content":[ ...blocks... ], "usage":{...}, "model":...}}

Content blocks:
    {"type":"text","text":...}
    {"type":"thinking","thinking":...}                  -> reasoning event
    {"type":"tool_use","id":"toolu_..","name":..,"input":{..}}  -> tool span
    {"type":"tool_result","tool_use_id":"toolu_..","content":..,"is_error":bool}
                                                        -> tool span result

Sub-agents (the Task tool) run on a *sidechain*: their entries have
``isSidechain=true`` and ``parentToolUseId`` set to the ``tool_use`` id of the
spawning ``Task`` call. We render each such group as a ``handoff`` span
containing a nested sub-``agent``.

Output is written as compact OpenTrace ``span`` records with original transcript
timestamps preserved. Validate with ``opentrace validate``.

Usage:
    python transcript_to_opentrace.py <session>.jsonl -o session.ot.jsonl [--validate]
    python transcript_to_opentrace.py ~/.claude/projects/<slug>/<session>.jsonl -o out.ot.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid as _uuidlib
from typing import Any, Dict, List, Optional

SPEC_VERSION = "0.1"


def _sid() -> str:
    return _uuidlib.uuid4().hex[:16]


def _tid() -> str:
    return _uuidlib.uuid4().hex


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _content_to_text(content: Any) -> str:
    """tool_result content may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


class Emitter:
    """Accumulates compact OpenTrace records with a monotonic seq counter."""

    def __init__(self, trace_id: str, session_id: Optional[str]):
        self.trace_id = trace_id
        self.session_id = session_id
        self._seq = 0
        self.records: List[Dict[str, Any]] = []

    def next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def span(self, kind: str, name: str, parent_id: Optional[str],
             start_time: Optional[str], end_time: Optional[str],
             **fields) -> str:
        span_id = _sid()
        rec = _clean({
            "ot": SPEC_VERSION, "type": "span", "trace_id": self.trace_id,
            "span_id": span_id, "parent_id": parent_id, "seq": self.next_seq(),
            "time": start_time, "kind": kind, "name": name,
            "start_time": start_time, "end_time": end_time or start_time,
            "session_id": self.session_id,
            "status": {"code": fields.pop("_status", "ok")},
        })
        rec.update(_clean(fields))
        self.records.append(rec)
        return span_id

    def event(self, name: str, span_id: str, time: Optional[str], **fields) -> None:
        rec = _clean({
            "ot": SPEC_VERSION, "type": "event", "trace_id": self.trace_id,
            "span_id": span_id, "seq": self.next_seq(), "time": time, "name": name,
        })
        rec.update(_clean(fields))
        self.records.append(rec)

    def header(self, name: str, time: Optional[str], cwd: Optional[str]) -> None:
        self.records.append(_clean({
            "ot": SPEC_VERSION, "type": "trace.start", "trace_id": self.trace_id,
            "seq": self.next_seq(), "time": time, "name": name,
            "session_id": self.session_id,
            "producer": {"name": "claude-code", "sdk": "opentrace-claude-code-adapter"},
            "resource": {"working_directory": cwd} if cwd else None,
        }))

    def footer(self, time: Optional[str], outcome: str = "success") -> None:
        self.records.append(_clean({
            "ot": SPEC_VERSION, "type": "trace.end", "trace_id": self.trace_id,
            "seq": self.next_seq(), "time": time, "outcome": outcome,
        }))


def load_transcript(path: str) -> List[Dict[str, Any]]:
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def _blocks(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content or []


def _is_tool_result_only(entry: Dict[str, Any]) -> bool:
    blocks = _blocks(entry)
    return bool(blocks) and all(b.get("type") == "tool_result" for b in blocks)


def convert(entries: List[Dict[str, Any]], session_id: Optional[str]) -> List[Dict[str, Any]]:
    trace_id = _tid()
    em = Emitter(trace_id, session_id)

    first_ts = entries[0].get("timestamp") if entries else None
    last_ts = entries[-1].get("timestamp") if entries else None
    cwd = next((e.get("cwd") for e in entries if e.get("cwd")), None)
    em.header(name="claude-code session", time=first_ts, cwd=cwd)

    # Index tool_result blocks by tool_use_id across the whole transcript.
    tool_results: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        for b in _blocks(e):
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                tool_results[b["tool_use_id"]] = {
                    "content": b.get("content"),
                    "is_error": bool(b.get("is_error")),
                    "time": e.get("timestamp"),
                }

    main = [e for e in entries if not e.get("isSidechain")]
    side = [e for e in entries if e.get("isSidechain")]
    # group sidechain entries by the Task tool_use id that spawned them
    side_by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for e in side:
        side_by_parent.setdefault(e.get("parentToolUseId") or "_orphan", []).append(e)

    # One turn spanning the whole main conversation; a single main agent under it.
    turn_id = em.span("turn", "turn", parent_id=None,
                      start_time=first_ts, end_time=last_ts)
    agent_id = em.span("agent", "Claude Code", parent_id=turn_id,
                       start_time=first_ts, end_time=last_ts,
                       agent={"id": "main", "name": "Claude Code",
                              "role": "orchestrator"})

    def emit_tool_use(b: Dict[str, Any], parent: str, ts: Optional[str]) -> None:
        tuid = b.get("id")
        res = tool_results.get(tuid or "")
        end_ts = res["time"] if res else ts
        status = "error" if (res and res["is_error"]) else "ok"
        result_val = _content_to_text(res["content"]) if res else None
        # If this tool_use spawned a sidechain (Task), render a handoff instead.
        if tuid in side_by_parent:
            emit_sidechain(tuid, b, parent, ts, end_ts)
            return
        em.span("tool", b.get("name", "tool"), parent_id=parent,
                start_time=ts, end_time=end_ts,
                tool_name=b.get("name"), tool_call_id=tuid,
                arguments=b.get("input"),
                result=result_val, _status=status)

    def emit_assistant_entry(entry: Dict[str, Any], parent: str) -> None:
        ts = entry.get("timestamp")
        msg = entry.get("message") or {}
        text_parts, tool_blocks = [], []
        for b in _blocks(entry):
            bt = b.get("type")
            if bt == "thinking":
                em.event("reasoning", parent, ts,
                         content=[{"type": "reasoning", "text": b.get("thinking", "")}])
            elif bt == "text" and b.get("text"):
                text_parts.append({"type": "text", "text": b["text"]})
            elif bt == "tool_use":
                tool_blocks.append(b)
        usage = msg.get("usage") or {}
        ot_usage = _clean({
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        }) or None
        # An llm span records this assistant turn's model output + usage.
        out_msgs = None
        if text_parts or tool_blocks:
            out_blocks = list(text_parts) + [
                {"type": "tool_call", "tool_call_id": b.get("id"),
                 "name": b.get("name"), "arguments": b.get("input")}
                for b in tool_blocks]
            out_msgs = {"messages": [{"role": "assistant", "parts": out_blocks}]}
        model = msg.get("model")
        em.span("llm", "chat " + (model or ""), parent_id=parent,
                start_time=ts, end_time=ts,
                model=("anthropic/" + model) if model and "/" not in model else model,
                provider="anthropic", output=out_msgs, usage=ot_usage)
        # Then the tool spans (siblings of the llm, children of the agent).
        for b in tool_blocks:
            emit_tool_use(b, parent, ts)

    def emit_sidechain(task_tuid: str, task_block: Dict[str, Any],
                       parent: str, start_ts: Optional[str],
                       end_ts: Optional[str]) -> None:
        sub_entries = side_by_parent.get(task_tuid, [])
        sub_agent_type = (task_block.get("input") or {}).get("subagent_type", "subagent")
        h_id = em.span("handoff", "handoff -> " + sub_agent_type, parent_id=parent,
                       start_time=start_ts, end_time=end_ts,
                       from_agent="main", to_agent=sub_agent_type, mode="delegate")
        sub_id = em.span("agent", sub_agent_type, parent_id=h_id,
                         start_time=start_ts, end_time=end_ts,
                         agent={"id": task_tuid, "name": sub_agent_type,
                                "role": "subagent"})
        for e in sub_entries:
            if e.get("type") == "assistant":
                emit_assistant_entry(e, sub_id)

    for entry in main:
        etype = entry.get("type")
        if etype == "user" and not _is_tool_result_only(entry):
            # a real user prompt -> record as a user message event on the turn
            txt = _content_to_text((entry.get("message") or {}).get("content"))
            em.event("message", turn_id, entry.get("timestamp"),
                     message={"role": "user", "parts": [{"type": "text", "text": txt}]})
        elif etype == "assistant":
            emit_assistant_entry(entry, agent_id)
        # tool_result-only user entries are consumed via the tool_results index.

    em.footer(time=last_ts, outcome="success")
    return em.records


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert a Claude Code transcript .jsonl to OpenTrace")
    ap.add_argument("transcript", help="path to <session>.jsonl")
    ap.add_argument("-o", "--output", help="output .ot.jsonl (default stdout)")
    ap.add_argument("--session-id", help="override session id (default: filename stem)")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args(argv)

    entries = load_transcript(args.transcript)
    if not entries:
        print("no entries found in %s" % args.transcript, file=sys.stderr)
        return 1
    session_id = args.session_id or os.path.splitext(os.path.basename(args.transcript))[0]
    records = convert(entries, session_id)
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
