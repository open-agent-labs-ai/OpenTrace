"""Helpers for building OpenTrace content parts and messages.

These produce plain dicts matching the spec's message/part model (§11), which is
identical to the OpenTelemetry GenAI structured message-part model.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .constants import REDACTED


# --- content parts ---------------------------------------------------------

def text(value: str) -> Dict[str, Any]:
    return {"type": "text", "text": value}


def reasoning(value: str, signature: Optional[str] = None,
              redacted: bool = False) -> Dict[str, Any]:
    part: Dict[str, Any] = {"type": "reasoning"}
    part["text"] = REDACTED if redacted else value
    if redacted:
        part["redacted"] = True
    if signature is not None:
        part["signature"] = signature
    return part


def tool_call(tool_call_id: str, name: str, arguments: Any) -> Dict[str, Any]:
    return {
        "type": "tool_call",
        "tool_call_id": tool_call_id,
        "name": name,
        "arguments": arguments,
    }


def tool_call_response(tool_call_id: str, result: Any,
                       is_error: bool = False) -> Dict[str, Any]:
    part: Dict[str, Any] = {
        "type": "tool_call_response",
        "tool_call_id": tool_call_id,
        "result": result,
    }
    if is_error:
        part["is_error"] = True
    return part


def blob(mime_type: str, data: Optional[str] = None,
         redacted: bool = False) -> Dict[str, Any]:
    return {"type": "blob", "mime_type": mime_type,
            "data": REDACTED if redacted or data is None else data}


def file_part(mime_type: str, name: str, uri: Optional[str] = None,
              nbytes: Optional[int] = None) -> Dict[str, Any]:
    part: Dict[str, Any] = {"type": "file", "mime_type": mime_type, "name": name}
    if uri is not None:
        part["uri"] = uri
    if nbytes is not None:
        part["bytes"] = nbytes
    return part


def uri(value: str, mime_type: Optional[str] = None) -> Dict[str, Any]:
    part: Dict[str, Any] = {"type": "uri", "uri": value}
    if mime_type is not None:
        part["mime_type"] = mime_type
    return part


def compaction(summary: str, replaced_count: Optional[int] = None) -> Dict[str, Any]:
    part: Dict[str, Any] = {"type": "compaction", "summary": summary}
    if replaced_count is not None:
        part["replaced_count"] = replaced_count
    return part


# --- messages --------------------------------------------------------------

def _as_parts(content) -> List[Dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [text(content)]
    if isinstance(content, dict):
        return [content]
    return list(content)


def message(role: str, content, name: Optional[str] = None,
            finish_reason: Optional[str] = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"role": role, "parts": _as_parts(content)}
    if name is not None:
        msg["name"] = name
    if finish_reason is not None:
        msg["finish_reason"] = finish_reason
    return msg


def system(content, **kw) -> Dict[str, Any]:
    return message("system", content, **kw)


def user(content, **kw) -> Dict[str, Any]:
    return message("user", content, **kw)


def assistant(content, finish_reason: Optional[str] = None, **kw) -> Dict[str, Any]:
    return message("assistant", content, finish_reason=finish_reason, **kw)


def tool(content, name: Optional[str] = None, **kw) -> Dict[str, Any]:
    return message("tool", content, name=name, **kw)
