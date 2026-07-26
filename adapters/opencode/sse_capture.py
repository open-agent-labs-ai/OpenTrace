#!/usr/bin/env python3
"""OpenCode SSE event stream -> OpenTrace.

OpenCode's server exposes a Server-Sent-Events stream at
``GET http://127.0.0.1:4096/event``. This script subscribes to it and writes
OpenTrace v0.1 records, one ``.ot.jsonl`` file per session under
``--out-dir`` (default ``./opencode-opentrace``).

Run OpenCode's server first, e.g.:
    opencode serve --port 4096
then in another terminal:
    python sse_capture.py --host 127.0.0.1 --port 4096 --out-dir ./traces

Events consumed (OpenCode event bus):
    session.created        -> trace.start (+ root turn + main agent)
    tool.execute.before    -> tool span.start
    tool.execute.after     -> tool span.end
    message.part.updated   -> reasoning event / step-finish -> llm span w/ usage
    session.idle           -> close turn + trace.end

Standard library only. The OpenTrace SDK is optional (used only if you pass
``--validate`` after capture stops, which validates each written file).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid as _uuidlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.request import urlopen, Request

SPEC_VERSION = "0.1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _sid() -> str:
    return _uuidlib.uuid4().hex[:16]


def _tid() -> str:
    return _uuidlib.uuid4().hex


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class Session:
    def __init__(self, session_id: str, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)
        self.session_id = session_id
        self.trace_id = _tid()
        self.seq = 0
        self.path = os.path.join(out_dir, session_id + ".ot.jsonl")
        self.turn_id: Optional[str] = None
        self.agent_id: Optional[str] = None
        self.open_tools: Dict[str, str] = {}
        self._start()

    def _next(self) -> int:
        s = self.seq
        self.seq += 1
        return s

    def emit(self, rec: Dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_clean(rec), ensure_ascii=False) + "\n")

    def span_start(self, kind: str, name: str, parent: Optional[str], **f) -> str:
        span_id = _sid()
        self.emit({"ot": SPEC_VERSION, "type": "span.start", "trace_id": self.trace_id,
                   "span_id": span_id, "parent_id": parent, "seq": self._next(),
                   "time": _now(), "kind": kind, "name": name, "turn_id": self.turn_id,
                   "session_id": self.session_id, **_clean(f)})
        return span_id

    def span_end(self, span_id: str, kind: str, status: str = "ok", **f) -> None:
        self.emit({"ot": SPEC_VERSION, "type": "span.end", "trace_id": self.trace_id,
                   "span_id": span_id, "seq": self._next(), "time": _now(), "kind": kind,
                   "status": {"code": status}, "turn_id": self.turn_id, **_clean(f)})

    def event(self, name: str, span_id: str, **f) -> None:
        self.emit({"ot": SPEC_VERSION, "type": "event", "trace_id": self.trace_id,
                   "span_id": span_id, "seq": self._next(), "time": _now(),
                   "name": name, "turn_id": self.turn_id, **_clean(f)})

    def _start(self) -> None:
        self.emit({"ot": SPEC_VERSION, "type": "trace.start", "trace_id": self.trace_id,
                   "seq": self._next(), "time": _now(), "name": "opencode session",
                   "session_id": self.session_id,
                   "producer": {"name": "opencode", "sdk": "opentrace-opencode-sse"}})
        self.turn_id = self.span_start("turn", "turn", None)
        self.agent_id = self.span_start("agent", "OpenCode", self.turn_id,
                                        agent={"id": "main", "name": "OpenCode",
                                               "role": "orchestrator"})

    def finish(self) -> None:
        for cid, sid in list(self.open_tools.items()):
            self.span_end(sid, "tool", "ok")
            del self.open_tools[cid]
        if self.agent_id:
            self.span_end(self.agent_id, "agent", "ok")
        if self.turn_id:
            self.span_end(self.turn_id, "turn", "ok")
        self.emit({"ot": SPEC_VERSION, "type": "trace.end", "trace_id": self.trace_id,
                   "seq": self._next(), "time": _now(), "outcome": "success"})


class Capture:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        self.sessions: Dict[str, Session] = {}

    def get(self, session_id: Optional[str]) -> Session:
        session_id = session_id or "default"
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(session_id, self.out_dir)
        return self.sessions[session_id]

    def handle(self, evt: Dict[str, Any]) -> None:
        etype = evt.get("type")
        props = evt.get("properties") or {}
        session_id = (props.get("sessionID") or (props.get("info") or {}).get("sessionID")
                      or (props.get("info") or {}).get("id"))
        if etype == "session.created":
            self.get(session_id)
        elif etype == "session.idle":
            s = self.sessions.pop(session_id, None)
            if s:
                s.finish()
        elif etype == "tool.execute.before":
            s = self.get(session_id)
            call_id = props.get("callID") or _sid()
            span_id = s.span_start("tool", props.get("tool", "tool"), s.agent_id,
                                   tool_name=props.get("tool"), tool_call_id=call_id,
                                   arguments=(props.get("output") or {}).get("args") or props.get("args"))
            s.open_tools[call_id] = span_id
        elif etype == "tool.execute.after":
            s = self.get(session_id)
            call_id = props.get("callID")
            span_id = s.open_tools.pop(call_id, None)
            if span_id:
                out = props.get("output") or {}
                meta = out.get("metadata") or props.get("metadata") or {}
                is_err = bool(meta.get("error") or out.get("error"))
                s.span_end(span_id, "tool", "error" if is_err else "ok",
                           result=out.get("output") or out.get("title") or props.get("output"))
        elif etype == "message.part.updated":
            part = props.get("part") or {}
            s = self.get(part.get("sessionID") or session_id)
            ptype = part.get("type")
            if ptype == "reasoning" and part.get("text"):
                s.event("reasoning", s.agent_id,
                        content=[{"type": "reasoning", "text": part["text"]}])
            elif ptype == "step-finish":
                usage = part.get("tokens") or part.get("usage") or {}
                cache = usage.get("cache") or {}
                llm = s.span_start("llm", "chat", s.agent_id,
                                   model=part.get("model") or part.get("modelID"),
                                   provider=part.get("provider") or part.get("providerID"))
                s.span_end(llm, "llm", "ok",
                           usage=_clean({"input_tokens": usage.get("input"),
                                         "output_tokens": usage.get("output"),
                                         "cache_read_tokens": cache.get("read"),
                                         "cache_creation_tokens": cache.get("write")}) or None,
                           cost=({"total": part["cost"], "currency": "USD"}
                                 if part.get("cost") is not None else None))
            elif ptype == "subtask":
                to = part.get("agent") or part.get("name") or "subtask"
                h = s.span_start("handoff", "handoff -> " + to, s.agent_id,
                                 from_agent="main", to_agent=to, mode="delegate")
                sub = s.span_start("agent", to, h,
                                   agent={"id": part.get("id") or to, "name": to,
                                          "role": "subagent"})
                s.span_end(sub, "agent", "ok")
                s.span_end(h, "handoff", "ok")

    def finish_all(self) -> None:
        for s in list(self.sessions.values()):
            s.finish()
        self.sessions.clear()


def stream(host: str, port: int, cap: Capture) -> None:
    url = "http://%s:%d/event" % (host, port)
    req = Request(url, headers={"Accept": "text/event-stream"})
    print("subscribing to %s ..." % url, file=sys.stderr)
    with urlopen(req) as resp:  # nosec - localhost dev server
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data = line[len("data:"):].strip()
                if not data:
                    continue
                try:
                    evt = json.loads(data)
                except ValueError:
                    continue
                cap.handle(evt)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Capture OpenCode SSE into OpenTrace")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4096)
    ap.add_argument("--out-dir", default="./opencode-opentrace")
    ap.add_argument("--validate", action="store_true",
                    help="on exit, validate each written file with the OpenTrace SDK")
    args = ap.parse_args(argv)

    cap = Capture(args.out_dir)
    try:
        stream(args.host, args.port, cap)
    except KeyboardInterrupt:
        print("\nstopping; closing open sessions", file=sys.stderr)
    except Exception as exc:
        print("stream error: %s" % exc, file=sys.stderr)
    finally:
        cap.finish_all()

    if args.validate:
        try:
            from opentrace import validate_stream
        except ImportError:
            print("opentrace SDK not importable; skipping validation", file=sys.stderr)
            return 0
        rc = 0
        for name in os.listdir(args.out_dir):
            if not name.endswith(".ot.jsonl"):
                continue
            path = os.path.join(args.out_dir, name)
            with open(path, "r", encoding="utf-8") as fh:
                count, problems = validate_stream(fh)
            errs = [p for p in problems if "warning" not in p]
            print("%s: %d records, %d error(s)" % (name, count, len(errs)), file=sys.stderr)
            rc = rc or (1 if errs else 0)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
