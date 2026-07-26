"""OpenTrace constants: spec version and the controlled vocabularies."""
from __future__ import annotations

SPEC_VERSION = "0.1"
SDK_NAME = "opentrace-py"
SDK_VERSION = "0.1.0"

REDACTED = "__REDACTED__"


class RecordType:
    TRACE_START = "trace.start"
    TRACE_END = "trace.end"
    SPAN_START = "span.start"
    SPAN_END = "span.end"
    SPAN = "span"
    EVENT = "event"


class SpanKind:
    SESSION = "session"
    TURN = "turn"
    AGENT = "agent"
    WORKFLOW = "workflow"
    CHAIN = "chain"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVER = "retriever"
    EMBEDDING = "embedding"
    RERANKER = "reranker"
    CLASSIFIER = "classifier"
    GUARDRAIL = "guardrail"
    EVALUATOR = "evaluator"
    PLAN = "plan"
    HANDOFF = "handoff"
    HARNESS = "harness"
    HUMAN = "human"
    MEMORY = "memory"
    CUSTOM = "custom"


class EventName:
    MESSAGE = "message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DECISION = "decision"
    PLAN_UPDATE = "plan_update"
    DELTA = "delta"
    STATE = "state"
    ARTIFACT = "artifact"
    SCORE = "score"
    METRIC = "metric"
    INTERRUPT = "interrupt"
    APPROVAL = "approval"
    LOG = "log"
    ERROR = "error"
    CUSTOM = "custom"


class Outcome:
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class StatusCode:
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    IN_PROGRESS = "in_progress"


class Profile:
    STREAM = "stream"
    BATCH = "batch"
