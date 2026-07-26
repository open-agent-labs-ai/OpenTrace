// Smoke test for the OpenTrace TypeScript SDK.
//
// Builds a trace containing a plan, an LLM call, a tool call, and a sub-agent
// handoff; writes it to a temp file; reads it back; and asserts every line
// parses and the stream validates. Run after `npx tsc`:
//
//   node test/smoke.mjs
//
// Exits non-zero on any failure.
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  Tracer,
  JsonlExporter,
  MemoryExporter,
  MultiExporter,
  Profile,
  content as C,
  validateStream,
  validate,
  recordsToOtlp,
} from "../dist/index.js";

let failures = 0;
function assert(cond, msg) {
  if (!cond) {
    failures += 1;
    console.error("FAIL: " + msg);
  } else {
    console.log("ok  : " + msg);
  }
}

const dir = mkdtempSync(join(tmpdir(), "opentrace-ts-"));
const file = join(dir, "trace.ot.jsonl");
const mem = new MemoryExporter();

try {
  const tracer = new Tracer({
    name: "smoke-agent",
    producer: { name: "smoke", version: "1.0" },
    exporter: new MultiExporter([new JsonlExporter(file), mem]),
    environment: "test",
  });

  const tr = tracer.trace("answer-question", {
    inputs: { q: "weather in Paris?" },
    sessionId: "sess_1",
  });

  // Callback form (primary): a turn wrapping a sub-agent.
  await tr.turn({ input: [C.user("What's the weather in Paris?")] }, async (turn) => {
    await turn.agent(
      { id: "main", name: "Assistant", role: "orchestrator", model: "anthropic/claude-opus-4-8" },
      async (ag) => {
        // Plan (manual form).
        const plan = ag.plan({
          goal: "answer weather query",
          steps: [{ id: "s1", description: "call weather tool", status: "pending" }],
        });
        plan.end();

        // LLM call that decides to call a tool (callback form).
        await ag.llm(
          { model: "anthropic/claude-opus-4-8", provider: "anthropic",
            input: [C.user("What's the weather in Paris?")] },
          async (call) => {
            call.setOutput({
              messages: [
                C.assistant([C.toolCall("call_1", "get_weather", { city: "Paris" })], {
                  finishReason: "tool_call",
                }),
              ],
              usage: { input_tokens: 1200, output_tokens: 40 },
              cost: { total: 0.008, currency: "USD" },
            });
          }
        );

        // Tool execution (manual form).
        const t = ag.tool("get_weather", { arguments: { city: "Paris" }, toolCallId: "call_1" });
        t.setResult({ temp_c: 21, sky: "clear" });
        t.end();

        // Sub-agent handoff (callback form) with a nested LLM call.
        await ag.handoff(
          { fromAgent: "main", toAgent: "weather_specialist", mode: "delegate",
            reason: "domain expertise" },
          async (h) => {
            await h.agent({ id: "weather", name: "WeatherBot", role: "subagent" }, async (sub) => {
              await sub.llm({ model: "openai/gpt-5" }, async (c2) => {
                c2.setOutput({
                  messages: [C.assistant("21C and clear in Paris.")],
                  usage: { input_tokens: 10, output_tokens: 8 },
                });
              });
            });
          }
        );

        ag.planUpdate("plan_1", "s1", "done");
      }
    );
  });

  tr.finish({ outcome: "success", outputs: { answer: "21C and clear in Paris." } });
  tracer.close();

  // --- read the file back ---------------------------------------------------
  const raw = readFileSync(file, "utf-8");
  const lines = raw.split("\n").filter((l) => l.trim());
  assert(lines.length > 0, "wrote at least one record");

  const parsed = lines.map((l) => JSON.parse(l)); // throws on malformed JSON
  assert(parsed.length === lines.length, "every line parses as JSON");

  assert(parsed[0].type === "trace.start", "first record is trace.start");
  assert(parsed[parsed.length - 1].type === "trace.end", "last record is trace.end");

  // Envelope + monotonic seq.
  let expectedSeq = 0;
  let seqOk = true;
  for (const rec of parsed) {
    if (rec.seq !== expectedSeq) seqOk = false;
    expectedSeq += 1;
    if (rec.ot !== "0.1") seqOk = false;
  }
  assert(seqOk, "seq is monotonic from 0 and ot=='0.1' everywhere");

  // Presence of the requested span kinds.
  const kinds = new Set(parsed.filter((r) => r.kind).map((r) => r.kind));
  for (const k of ["turn", "agent", "plan", "llm", "tool", "handoff"]) {
    assert(kinds.has(k), `stream contains a '${k}' span`);
  }

  // snake_case field interop checks.
  const toolStart = parsed.find((r) => r.kind === "tool" && r.tool_name);
  assert(!!toolStart && toolStart.tool_call_id === "call_1", "tool_call_id emitted in snake_case");
  const handoff = parsed.find((r) => r.kind === "handoff" && r.from_agent);
  assert(!!handoff && handoff.to_agent === "weather_specialist", "from_agent/to_agent snake_case");

  // Footer metrics rollup.
  const footer = parsed[parsed.length - 1];
  assert(footer.outcome === "success", "footer outcome=success");
  assert(footer.metrics && footer.metrics.llm_calls === 2, "footer counts 2 llm_calls");
  assert(footer.metrics.tool_calls === 1, "footer counts 1 tool_call");
  assert(footer.metrics.total_tokens === 1258, "footer sums total_tokens (1200+40+10+8)");

  // Validation via both entry points.
  const v1 = validateStream(lines);
  const hardErrors = v1.problems.filter((p) => !p.includes("warning"));
  assert(hardErrors.length === 0, "validateStream reports no hard errors: " + JSON.stringify(hardErrors));

  const v2 = validate(parsed);
  const hardErrors2 = v2.problems.filter((p) => !p.includes("warning"));
  assert(hardErrors2.length === 0, "validate(records) reports no hard errors");

  // Memory exporter captured the same records.
  assert(mem.records.length === parsed.length, "MemoryExporter captured every record");

  // OTLP conversion produces spans.
  const otlp = recordsToOtlp(parsed);
  const otlpSpans = otlp.resourceSpans[0].scopeSpans[0].spans;
  assert(otlpSpans.length >= 6, "recordsToOtlp produced spans (>=6)");
  const traceIdsOk = otlpSpans.every((s) => /^[0-9a-f]{32}$/.test(s.traceId));
  assert(traceIdsOk, "OTLP spans carry 32-hex trace ids");

  // --- batch profile: single `span` records --------------------------------
  const batchMem = new MemoryExporter();
  const bt = new Tracer({ name: "batch", producer: { name: "batch" }, exporter: batchMem,
    profile: Profile.BATCH });
  const btr = bt.trace("batch-demo");
  const bspan = btr.span("llm", "chat openai/gpt-5", { model: "openai/gpt-5" });
  bspan.setInput([C.user("hi")]);
  bspan.setOutput({ messages: [C.assistant("hello")], usage: { input_tokens: 10, output_tokens: 5 } });
  bspan.end();
  btr.finish({ outcome: "success" });
  bt.close();
  const spanRecs = batchMem.records.filter((r) => r.type === "span");
  assert(spanRecs.length === 1, "batch profile emits exactly one compact 'span' record");
  assert(spanRecs[0].start_time && spanRecs[0].end_time && typeof spanRecs[0].duration_ms === "number",
    "batch span has start_time/end_time/duration_ms");
  assert(spanRecs[0].usage.total_tokens === 15, "batch span usage total_tokens computed");
} finally {
  rmSync(dir, { recursive: true, force: true });
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll smoke assertions passed.");
