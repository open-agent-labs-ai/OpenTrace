#!/usr/bin/env python3
"""Claude Code hook -> OpenTrace live capture.

Claude Code invokes this script once per hook event, passing a JSON object on
**stdin**. Because each hook is a *fresh process*, this adapter is stateless
between invocations: it appends OpenTrace records to a per-session
``.ot.jsonl`` file and keeps a tiny sidecar ``.state.json`` so that a
``PostToolUse`` can find the span id that its ``PreToolUse`` opened.

Wire it up in ``~/.claude/settings.json`` (or ``.claude/settings.json`` in a
project). See README.md for the exact block. Every hook command is:

    python3 /abs/path/hook_capture.py

Hook stdin fields we read (Claude Code hook payload):
    session_id, transcript_path, cwd, hook_event_name,
    tool_name, tool_input, tool_use_id, tool_response,
    agent_id, agent_type, prompt

Event mapping:
    SessionStart     -> trace.start (+ root turn placeholder)
    UserPromptSubmit -> new turn (records the user prompt)
    PreToolUse       -> tool span.start
    PostToolUse      -> tool span.end (result / error)
    SubagentStart    -> handoff span.start + sub-agent span.start
    SubagentStop     -> sub-agent span.end + handoff span.end
    Stop             -> close open turn
    SessionEnd       -> trace.end

Records are written as OpenTrace streaming records (``span.start`` / ``span.end``
/ ``event``), which validate against the v0.1 schema.

This script uses only the standard library so it can run as a hook without any
install. If the OpenTrace SDK is on ``PYTHONPATH`` it is *not* required.
"""
from __future__ import annotations

import json
import os
import sys
import uuid as _uuidlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SPEC_VERSION = "0.1"

# Where to write traces. Override with OPENTRACE_DIR.
TRACE_DIR = os.environ.get(
    "OPENTRACE_DIR",
    os.path.expanduser("~/.claude/opentrace"),
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _sid() -> str:
    return _uuidlib.uuid4().hex[:16]


def _tid() -> str:
    return _uuidlib.uuid4().hex


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class SessionStore:
    """Per-session trace file + JSON sidecar state (span ids, seq counter)."""

    def __init__(self, session_id: str):
        os.makedirs(TRACE_DIR, exist_ok=True)
        self.session_id = session_id
        self.trace_path = os.path.join(TRACE_DIR, session_id + ".ot.jsonl")
        self.state_path = os.path.join(TRACE_DIR, session_id + ".state.json")
        self.state = self._load()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (ValueError, OSError):
                pass
        return {
            "trace_id": _tid(),
            "seq": 0,
            "turn_id": None,
            "agent_id": None,
            "open_tools": {},     # tool_use_id -> span_id
            "subagents": {},      # agent_id -> {"handoff": sid, "agent": sid}
            "started": False,
        }

    def save(self) -> None:
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)

    def next_seq(self) -> int:
        s = self.state["seq"]
        self.state["seq"] = s + 1
        return s

    def emit(self, rec: Dict[str, Any]) -> None:
        rec = _clean(rec)
        with open(self.trace_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # -- record builders --------------------------------------------------
    def span_start(self, kind: str, name: str, parent_id: Optional[str],
                   **fields) -> str:
        span_id = _sid()
        self.emit({
            "ot": SPEC_VERSION, "type": "span.start",
            "trace_id": self.state["trace_id"], "span_id": span_id,
            "parent_id": parent_id, "seq": self.next_seq(), "time": _now(),
            "kind": kind, "name": name, "turn_id": self.state.get("turn_id"),
            "session_id": self.session_id, **_clean(fields),
        })
        return span_id

    def span_end(self, span_id: str, kind: str, status: str = "ok",
                 **fields) -> None:
        self.emit({
            "ot": SPEC_VERSION, "type": "span.end",
            "trace_id": self.state["trace_id"], "span_id": span_id,
            "seq": self.next_seq(), "time": _now(), "kind": kind,
            "status": {"code": status}, "turn_id": self.state.get("turn_id"),
            **_clean(fields),
        })

    def event(self, name: str, span_id: str, **fields) -> None:
        self.emit({
            "ot": SPEC_VERSION, "type": "event",
            "trace_id": self.state["trace_id"], "span_id": span_id,
            "seq": self.next_seq(), "time": _now(), "name": name,
            "turn_id": self.state.get("turn_id"), **_clean(fields),
        })


# --------------------------------------------------------------------------- #
# Hook handlers
# --------------------------------------------------------------------------- #

def on_session_start(store: SessionStore, payload: Dict[str, Any]) -> None:
    if store.state.get("started"):
        return
    store.emit({
        "ot": SPEC_VERSION, "type": "trace.start",
        "trace_id": store.state["trace_id"], "seq": store.next_seq(),
        "time": _now(), "name": "claude-code session",
        "session_id": store.session_id,
        "producer": {"name": "claude-code", "sdk": "opentrace-claude-code-hook"},
        "resource": _clean({"working_directory": payload.get("cwd")}),
    })
    store.state["started"] = True


def _ensure_agent(store: SessionStore) -> str:
    """Ensure a root turn + main agent exist; return the main agent span id."""
    if store.state.get("turn_id") is None:
        store.state["turn_id"] = store.span_start("turn", "turn", parent_id=None)
    if store.state.get("agent_id") is None:
        store.state["agent_id"] = store.span_start(
            "agent", "Claude Code", parent_id=store.state["turn_id"],
            agent={"id": "main", "name": "Claude Code", "role": "orchestrator"})
    return store.state["agent_id"]


def on_user_prompt(store: SessionStore, payload: Dict[str, Any]) -> None:
    # Close a previous turn's agent/turn, then open a fresh turn for this prompt.
    _close_turn(store)
    store.state["turn_id"] = store.span_start("turn", "turn", parent_id=None)
    agent_id = _ensure_agent(store)
    prompt = payload.get("prompt") or ""
    store.event("message", store.state["turn_id"],
                message={"role": "user", "parts": [{"type": "text", "text": prompt}]})
    _ = agent_id


def _current_parent(store: SessionStore, payload: Dict[str, Any]) -> str:
    """Tool spans nest under the active sub-agent if one is running, else main."""
    agent_type = payload.get("agent_type") or payload.get("agent_id")
    subs = store.state.get("subagents", {})
    if agent_type and agent_type in subs:
        return subs[agent_type]["agent"]
    return _ensure_agent(store)


def on_pre_tool(store: SessionStore, payload: Dict[str, Any]) -> None:
    parent = _current_parent(store, payload)
    tool_use_id = payload.get("tool_use_id") or _sid()
    span_id = store.span_start(
        "tool", payload.get("tool_name", "tool"), parent_id=parent,
        tool_name=payload.get("tool_name"), tool_call_id=tool_use_id,
        arguments=payload.get("tool_input"))
    store.state.setdefault("open_tools", {})[tool_use_id] = span_id


def on_post_tool(store: SessionStore, payload: Dict[str, Any]) -> None:
    tool_use_id = payload.get("tool_use_id")
    open_tools = store.state.setdefault("open_tools", {})
    span_id = open_tools.pop(tool_use_id, None)
    if span_id is None:
        # PreToolUse was missed; open+close in one shot.
        parent = _current_parent(store, payload)
        span_id = store.span_start(
            "tool", payload.get("tool_name", "tool"), parent_id=parent,
            tool_name=payload.get("tool_name"), tool_call_id=tool_use_id,
            arguments=payload.get("tool_input"))
    resp = payload.get("tool_response")
    is_error = isinstance(resp, dict) and bool(resp.get("is_error") or resp.get("error"))
    store.span_end(span_id, "tool", status="error" if is_error else "ok",
                   result=resp)


def on_subagent_start(store: SessionStore, payload: Dict[str, Any]) -> None:
    agent_type = payload.get("agent_type") or payload.get("agent_id") or "subagent"
    main_agent = _ensure_agent(store)
    handoff_id = store.span_start(
        "handoff", "handoff -> " + agent_type, parent_id=main_agent,
        from_agent="main", to_agent=agent_type, mode="delegate")
    sub_agent_id = store.span_start(
        "agent", agent_type, parent_id=handoff_id,
        agent={"id": payload.get("agent_id") or agent_type, "name": agent_type,
               "role": "subagent"})
    store.state.setdefault("subagents", {})[agent_type] = {
        "handoff": handoff_id, "agent": sub_agent_id}


def on_subagent_stop(store: SessionStore, payload: Dict[str, Any]) -> None:
    agent_type = payload.get("agent_type") or payload.get("agent_id") or "subagent"
    subs = store.state.setdefault("subagents", {})
    entry = subs.pop(agent_type, None)
    if not entry:
        return
    store.span_end(entry["agent"], "agent", status="ok")
    store.span_end(entry["handoff"], "handoff", status="ok")


def _close_turn(store: SessionStore) -> None:
    # Close any open sub-agents, the main agent, then the turn.
    for agent_type, entry in list(store.state.get("subagents", {}).items()):
        store.span_end(entry["agent"], "agent", status="ok")
        store.span_end(entry["handoff"], "handoff", status="ok")
    store.state["subagents"] = {}
    if store.state.get("agent_id") is not None:
        store.span_end(store.state["agent_id"], "agent", status="ok")
        store.state["agent_id"] = None
    if store.state.get("turn_id") is not None:
        store.span_end(store.state["turn_id"], "turn", status="ok")
        store.state["turn_id"] = None


def on_stop(store: SessionStore, payload: Dict[str, Any]) -> None:
    _close_turn(store)


def on_session_end(store: SessionStore, payload: Dict[str, Any]) -> None:
    _close_turn(store)
    store.emit({
        "ot": SPEC_VERSION, "type": "trace.end",
        "trace_id": store.state["trace_id"], "seq": store.next_seq(),
        "time": _now(), "outcome": "success",
    })


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "PreToolUse": on_pre_tool,
    "PostToolUse": on_post_tool,
    "SubagentStart": on_subagent_start,
    "SubagentStop": on_subagent_stop,
    "Stop": on_stop,
    "SessionEnd": on_session_end,
}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        # Never break the Claude Code session because of a tracing hiccup.
        return 0
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id") or "unknown-session"
    handler = HANDLERS.get(event)
    if handler is None:
        return 0
    try:
        store = SessionStore(session_id)
        # A tool event may arrive before SessionStart in some configs.
        if not store.state.get("started"):
            on_session_start(store, payload)
        handler(store, payload)
        store.save()
    except Exception as exc:  # never fail the host session
        sys.stderr.write("opentrace hook error: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
