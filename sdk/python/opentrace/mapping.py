"""Mapping tables and helpers: OpenTrace -> OpenTelemetry (§17 of the spec)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# OpenTrace span kind -> (gen_ai.operation.name | None, openinference.span.kind, otel SpanKind)
KIND_MAP = {
    "llm":        ("chat", "LLM", "CLIENT"),
    "tool":       ("execute_tool", "TOOL", "INTERNAL"),
    "agent":      ("invoke_agent", "AGENT", "INTERNAL"),
    "workflow":   ("invoke_workflow", "CHAIN", "INTERNAL"),
    "chain":      (None, "CHAIN", "INTERNAL"),
    "retriever":  ("retrieval", "RETRIEVER", "INTERNAL"),
    "embedding":  ("embeddings", "EMBEDDING", "CLIENT"),
    "reranker":   (None, "RERANKER", "INTERNAL"),
    "classifier": ("chat", "LLM", "INTERNAL"),
    "guardrail":  (None, "GUARDRAIL", "INTERNAL"),
    "evaluator":  (None, "EVALUATOR", "INTERNAL"),
    "plan":       ("plan", "CHAIN", "INTERNAL"),
    "handoff":    ("invoke_agent", "AGENT", "INTERNAL"),
    "turn":       (None, "CHAIN", "SERVER"),
    "session":    (None, "CHAIN", "INTERNAL"),
    "harness":    (None, "CHAIN", "INTERNAL"),
    "human":      (None, "CHAIN", "INTERNAL"),
    "memory":     (None, "CHAIN", "INTERNAL"),
    "custom":     (None, "CHAIN", "INTERNAL"),
}

# usage key -> OTel gen_ai attribute
USAGE_MAP = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "total_tokens": "gen_ai.usage.total_tokens",
    "cache_read_tokens": "gen_ai.usage.cache_read.input_tokens",
    "cache_creation_tokens": "gen_ai.usage.cache_creation.input_tokens",
    "reasoning_tokens": "gen_ai.usage.reasoning.output_tokens",
}


def span_otel_attributes(span: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an OpenTrace span record into OTLP-friendly attributes.

    Complex values (messages, arguments) are JSON-encoded strings, which is what
    most OTel backends expect for gen_ai.* content attributes.
    """
    import json

    kind = span.get("kind", "chain")
    attrs: Dict[str, Any] = {}
    op, oi_kind, _otel_kind = KIND_MAP.get(kind, (None, "CHAIN", "INTERNAL"))

    attrs["opentrace.kind"] = kind
    attrs["openinference.span.kind"] = oi_kind
    if op:
        attrs["gen_ai.operation.name"] = op

    if span.get("model"):
        attrs["gen_ai.request.model"] = span["model"]
    if span.get("provider"):
        attrs["gen_ai.provider.name"] = span["provider"]

    req = span.get("request") or {}
    for k in ("temperature", "top_p", "top_k", "max_tokens", "seed"):
        if req.get(k) is not None:
            attrs["gen_ai.request." + k] = req[k]

    resp = span.get("response") or {}
    if resp.get("id"):
        attrs["gen_ai.response.id"] = resp["id"]
    if resp.get("finish_reasons"):
        attrs["gen_ai.response.finish_reasons"] = json.dumps(resp["finish_reasons"])

    usage = span.get("usage") or {}
    for k, v in usage.items():
        otel_key = USAGE_MAP.get(k, "gen_ai.usage." + k)
        attrs[otel_key] = v

    cost = span.get("cost") or {}
    if cost.get("total") is not None:
        attrs["gen_ai.usage.cost"] = cost["total"]

    # Content as structured JSON strings.
    inp = span.get("input") or {}
    if isinstance(inp, dict) and inp.get("messages"):
        attrs["gen_ai.input.messages"] = json.dumps(inp["messages"], ensure_ascii=False)
    out = span.get("output") or {}
    if isinstance(out, dict) and out.get("messages"):
        attrs["gen_ai.output.messages"] = json.dumps(out["messages"], ensure_ascii=False)

    # Tool fields.
    if span.get("tool_name"):
        attrs["gen_ai.tool.name"] = span["tool_name"]
    if span.get("tool_call_id"):
        attrs["gen_ai.tool.call.id"] = span["tool_call_id"]
    if span.get("tool_type"):
        attrs["gen_ai.tool.type"] = span["tool_type"]
    if span.get("arguments") is not None:
        attrs["gen_ai.tool.call.arguments"] = json.dumps(span["arguments"], ensure_ascii=False)
    if span.get("result") is not None:
        attrs["gen_ai.tool.call.result"] = json.dumps(span["result"], ensure_ascii=False)

    # Agent fields.
    agent = span.get("agent") or {}
    if agent.get("id"):
        attrs["gen_ai.agent.id"] = agent["id"]
    if agent.get("name"):
        attrs["gen_ai.agent.name"] = agent["name"]

    # Handoff fields (no native OTel slot -> opentrace.*).
    for k in ("from_agent", "to_agent", "mode", "reason"):
        if span.get(k) is not None:
            attrs["opentrace.handoff." + k] = span[k]

    # Classifier / guardrail extras.
    for k in ("chosen", "confidence", "rationale", "policy", "verdict"):
        if span.get(k) is not None:
            v = span[k]
            attrs["opentrace." + k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v)

    # Plan.
    if span.get("plan") is not None:
        attrs["opentrace.plan"] = json.dumps(span["plan"], ensure_ascii=False)

    err = span.get("error") or {}
    if err.get("type"):
        attrs["error.type"] = err["type"]

    return attrs


def otel_status_code(status: Optional[Dict[str, Any]]) -> str:
    if not status:
        return "STATUS_CODE_UNSET"
    code = status.get("code")
    if code == "ok":
        return "STATUS_CODE_OK"
    if code in ("error", "timeout", "cancelled"):
        return "STATUS_CODE_ERROR"
    return "STATUS_CODE_UNSET"
