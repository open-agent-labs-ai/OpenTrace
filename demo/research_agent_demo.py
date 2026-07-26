#!/usr/bin/env python3
"""OpenTrace demo: a simulated multi-agent research assistant.

This produces a rich, realistic OpenTrace stream WITHOUT calling any real model, so it
runs anywhere. It exercises nearly every part of the spec: multi-turn sessions, an
agentic loop, planning, a router/classifier, RAG retrieval, reasoning, a tool loop,
streaming deltas, a sub-agent handoff, a guardrail, an evaluator, human approval,
usage/cost accounting, and an error+retry.

Run:
    python3 demo/research_agent_demo.py            # writes demo/sample_trace.ot.jsonl
    opentrace tree demo/sample_trace.ot.jsonl
    opentrace stats demo/sample_trace.ot.jsonl
    # then open viewer/index.html and load the file
"""
from __future__ import annotations

import os
import sys

# Allow running from a source checkout without installing.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk", "python"))

import opentrace as ot  # noqa: E402
from opentrace import content as C  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "sample_trace.ot.jsonl")

TOOLS = [
    {"name": "web_search", "type": "function", "description": "Search the web"},
    {"name": "calculator", "type": "function", "description": "Evaluate arithmetic"},
    {"name": "fetch_url", "type": "function", "description": "Fetch a web page"},
    {"name": "write_report", "type": "agent", "description": "Delegate to the writer sub-agent"},
]


def main() -> None:
    # Start fresh each run (JSONL files are append-only by design).
    if os.path.exists(OUT):
        os.remove(OUT)
    tracer = ot.Tracer(
        name="research-assistant",
        producer={"name": "hermes-agent", "version": "3.2.0"},
        output=OUT,
        environment="production",
        execution_context="runtime",
        resource={"service.name": "research-assistant", "service.version": "3.2.0",
                  "host.name": "worker-7"},
        profile=ot.Profile.STREAM,
    )

    with tracer.trace(
        name="quarterly-ai-market-brief",
        session_id="sess_9f2",
        user={"id": "u_hash_1a2b", "anonymized": True},
        inputs={"goal": "Write a brief on the AI agent market and its 2026 growth rate."},
        tool_definitions=TOOLS,
        system_instructions=[C.system("You are Hermes, a meticulous research assistant.")],
        metadata={"capture_level": "full"},
        agent={"id": "orchestrator", "name": "Hermes", "role": "orchestrator",
               "model": "anthropic/claude-opus-4-8", "framework": "hermes-agent"},
    ) as tr:

        # ---- TURN 1 ----------------------------------------------------
        with tr.turn(name="turn 1",
                     input=[C.user("Research the AI agent market and draft a short brief.")]) as turn:

            with turn.agent(id="orchestrator", name="Hermes", role="orchestrator",
                            model="anthropic/claude-opus-4-8", framework="hermes-agent") as ag:

                # Router / classifier decides this needs the research workflow.
                with ag.classifier(name="intent-router", model="anthropic/claude-haiku-4-5",
                                   candidates=["chitchat", "research", "coding"],
                                   chosen="research", confidence=0.97,
                                   scores={"research": 0.97, "coding": 0.02, "chitchat": 0.01},
                                   rationale="Query asks to research a market and draft a brief.") as clf:
                    clf.set_usage({"input_tokens": 220, "output_tokens": 12})
                    clf.set_cost(total=0.0002, currency="USD")

                # Planning.
                with ag.plan(goal="Produce a sourced brief on the AI agent market",
                             steps=[
                                 {"id": "s1", "description": "Gather sources (search + fetch)",
                                  "status": "pending"},
                                 {"id": "s2", "description": "Compute 2026 growth rate",
                                  "status": "pending", "depends_on": ["s1"]},
                                 {"id": "s3", "description": "Delegate drafting to writer",
                                  "status": "pending", "depends_on": ["s2"]},
                                 {"id": "s4", "description": "Safety + quality review",
                                  "status": "pending", "depends_on": ["s3"]},
                             ]):
                    pass

                # RAG retrieval over an internal knowledge base.
                with ag.retriever(query="AI agent market size 2025 2026 forecast",
                                  name="kb-retrieval") as r:
                    r.set_field(documents=[
                        {"id": "kb_12", "score": 0.91,
                         "content": "Agent platform revenue reached $5.1B in 2025..."},
                        {"id": "kb_47", "score": 0.86,
                         "content": "Analysts project 38% YoY growth into 2026..."},
                    ])
                    r.set_usage({"input_tokens": 0, "output_tokens": 0})

                # LLM reasons and decides to call the web_search tool. (streaming)
                with ag.llm(model="anthropic/claude-opus-4-8", provider="anthropic",
                            request={"temperature": 0.2, "max_tokens": 2048,
                                     "reasoning_effort": "high", "stream": True},
                            input=[C.user("Research the AI agent market and draft a short brief.")]) as call:
                    call.reasoning("The KB is a year old; I should verify 2026 numbers with a live search "
                                   "before quoting a growth rate.", signature="sig_a1")
                    for chunk in ["I'll ", "search ", "for the ", "latest ", "figures."]:
                        call.delta(chunk, channel="text")
                    call.set_output(
                        [C.assistant(
                            [C.reasoning("Verify with live search.", signature="sig_a1"),
                             C.text("I'll search for the latest figures."),
                             C.tool_call("call_s1", "web_search",
                                         {"query": "AI agent market size 2026 forecast"})],
                            finish_reason="tool_call")],
                        usage={"input_tokens": 1840, "output_tokens": 96,
                               "reasoning_tokens": 210, "cache_read_tokens": 1600},
                        cost={"input": 0.0055, "output": 0.0072, "total": 0.0127, "currency": "USD"},
                        response={"id": "msg_01", "finish_reasons": ["tool_call"],
                                  "time_to_first_token_ms": 340},
                    )

                # Tool loop: web_search, then a transient failure + retry on fetch_url, then calculator.
                with ag.tool("web_search", arguments={"query": "AI agent market size 2026 forecast"},
                             tool_call_id="call_s1") as t:
                    t.set_result({"results": [
                        {"title": "Agent market to hit $7B in 2026", "url": "https://example.com/a"},
                        {"title": "Enterprise agent adoption up 40%", "url": "https://example.com/b"}]})

                ag.plan_update("plan_1", "s1", "in_progress")

                # First fetch fails (rate limited) -> recorded error, then retried successfully.
                with ag.tool("fetch_url", arguments={"url": "https://example.com/a"},
                             tool_call_id="call_f1") as t:
                    t.record_error(RuntimeError("429 rate limited by upstream"))
                    t.set_status("error", "rate limited")
                tr._counts["retries"] += 1  # demo: note a retry happened
                with ag.tool("fetch_url", arguments={"url": "https://example.com/a", "retry": 1},
                             tool_call_id="call_f2") as t:
                    t.set_result({"status": 200, "text": "...$7.0B in 2026, up from $5.1B in 2025..."})

                with ag.tool("calculator", arguments={"expr": "(7.0-5.1)/5.1*100"},
                             tool_call_id="call_c1") as t:
                    t.set_result({"value": 37.25, "unit": "percent"})

                ag.plan_update("plan_1", "s1", "done")
                ag.plan_update("plan_1", "s2", "done")

                # Hand off to a Writer sub-agent to draft the brief.
                with ag.handoff(from_agent="orchestrator", to_agent="writer", mode="delegate",
                                reason="sources gathered; draft the brief",
                                payload={"growth_rate_pct": 37.25,
                                         "sources": ["example.com/a", "example.com/b"]}) as h:
                    with h.agent(id="writer", name="Writer", role="subagent",
                                 model="anthropic/claude-sonnet-4-6") as writer:
                        with writer.llm(model="anthropic/claude-sonnet-4-6", provider="anthropic",
                                        input=[C.user("Draft a 120-word brief using the sources and 37.25% growth.")]) as wc:
                            wc.set_output(
                                [C.assistant("The AI agent market grew from $5.1B (2025) to a projected "
                                             "$7.0B (2026), a 37% increase ...", finish_reason="stop")],
                                usage={"input_tokens": 900, "output_tokens": 260},
                                cost={"total": 0.006, "currency": "USD"})
                        writer.artifact({"id": "artifact_brief", "mime_type": "text/markdown",
                                         "name": "ai-agent-market-brief.md", "bytes": 812})

                ag.plan_update("plan_1", "s3", "done")

                # Safety guardrail + quality evaluation of the draft.
                with ag.guardrail(policy="pii-and-claims", verdict="pass",
                                  categories=["pii", "unsupported_claims"], score=0.02):
                    pass
                with ag.evaluator(name="brief-quality", target_span_id=None,
                                  criteria=["accuracy", "sourcing", "concision"]) as ev:
                    ev.score("accuracy", 0.93)
                    ev.score("sourcing", 0.88)
                    ev.set_field(scores={"accuracy": 0.93, "sourcing": 0.88, "concision": 0.9})

                # Human-in-the-loop approval before "publishing".
                with ag.human(interaction="approval",
                              request={"summary": "Publish the brief?"}) as hu:
                    hu.set_field(response={"decision": "approved", "actor": "user:u_hash_1a2b"})

                ag.plan_update("plan_1", "s4", "done")

            turn.set_output(messages=[C.assistant(
                "Here is a sourced brief: the AI agent market is projected to grow ~37% "
                "to $7.0B in 2026. (Draft attached, reviewer-approved.)")])

        # ---- TURN 2 (follow-up) ----------------------------------------
        with tr.turn(name="turn 2",
                     input=[C.user("Make it one sentence shorter.")]) as turn:
            with turn.agent(id="orchestrator", name="Hermes", role="orchestrator",
                            model="anthropic/claude-opus-4-8") as ag:
                with ag.llm(model="anthropic/claude-opus-4-8", provider="anthropic",
                            input=[C.user("Make it one sentence shorter.")]) as call:
                    call.set_output([C.assistant("Trimmed the brief by one sentence.", finish_reason="stop")],
                                    usage={"input_tokens": 640, "output_tokens": 48},
                                    cost={"total": 0.003, "currency": "USD"})
            turn.set_output(messages=[C.assistant("Done — shortened by one sentence.")])

        tr.finish(outcome="success",
                  outputs={"artifact": "ai-agent-market-brief.md",
                           "growth_rate_pct": 37.25},
                  summary="Produced and shortened a sourced brief on the AI agent market.")

    tracer.close()
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
