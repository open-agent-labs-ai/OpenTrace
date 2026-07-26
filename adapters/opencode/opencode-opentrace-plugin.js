/**
 * OpenCode -> OpenTrace plugin.
 *
 * Drop this file in `.opencode/plugins/` (project) or `~/.config/opencode/plugins/`
 * (global). OpenCode loads it and calls the exported hooks. It writes OpenTrace
 * v0.1 records as JSON lines to `.opencode/opentrace/<sessionID>.ot.jsonl`
 * (override with the OPENTRACE_DIR env var).
 *
 * We write records directly (a tiny inline writer) rather than depend on a TS
 * SDK, so this has zero dependencies beyond Node's stdlib.
 *
 * Hooks used (OpenCode plugin API):
 *   - event({ event })             : the server event bus (session.created,
 *                                    session.idle, message.part.updated, ...)
 *   - "tool.execute.before"(input, output) : input={tool,sessionID,callID},
 *                                            output={args}
 *   - "tool.execute.after"(input, output)  : input={tool,sessionID,callID},
 *                                            output={title,output,metadata}
 *
 * Mapping:
 *   session.created        -> trace.start (+ root turn + main agent)
 *   tool.execute.before    -> tool span.start
 *   tool.execute.after     -> tool span.end
 *   message.part.updated   -> reasoning event (reasoning parts) /
 *                             step-finish -> usage+cost on the last llm span
 *   subtask part           -> handoff + sub-agent (best effort)
 *   session.idle           -> close turn + trace.end
 */

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const SPEC_VERSION = "0.1";

function sid() {
  return crypto.randomBytes(8).toString("hex");
}
function tid() {
  return crypto.randomBytes(16).toString("hex");
}
function now() {
  // OpenTrace wants ISO-8601 with a trailing Z and microseconds; JS gives ms.
  return new Date().toISOString().replace("Z", "000Z");
}
function clean(o) {
  const r = {};
  for (const k of Object.keys(o)) if (o[k] !== undefined && o[k] !== null) r[k] = o[k];
  return r;
}

/** Per-session writer + minimal state kept in memory for the plugin lifetime. */
class Session {
  constructor(sessionID, dir) {
    this.sessionID = sessionID;
    this.traceId = tid();
    this.seq = 0;
    this.file = path.join(dir, `${sessionID}.ot.jsonl`);
    fs.mkdirSync(dir, { recursive: true });
    this.turnId = null;
    this.agentId = null;
    this.openTools = {}; // callID -> spanId
    this.started = false;
  }
  nextSeq() {
    return this.seq++;
  }
  emit(rec) {
    fs.appendFileSync(this.file, JSON.stringify(clean(rec)) + "\n");
  }
  spanStart(kind, name, parentId, fields = {}) {
    const spanId = sid();
    this.emit({
      ot: SPEC_VERSION, type: "span.start", trace_id: this.traceId,
      span_id: spanId, parent_id: parentId, seq: this.nextSeq(), time: now(),
      kind, name, turn_id: this.turnId, session_id: this.sessionID, ...clean(fields),
    });
    return spanId;
  }
  spanEnd(spanId, kind, status = "ok", fields = {}) {
    this.emit({
      ot: SPEC_VERSION, type: "span.end", trace_id: this.traceId, span_id: spanId,
      seq: this.nextSeq(), time: now(), kind, status: { code: status },
      turn_id: this.turnId, ...clean(fields),
    });
  }
  event(name, spanId, fields = {}) {
    this.emit({
      ot: SPEC_VERSION, type: "event", trace_id: this.traceId, span_id: spanId,
      seq: this.nextSeq(), time: now(), name, turn_id: this.turnId, ...clean(fields),
    });
  }
  start() {
    if (this.started) return;
    this.emit({
      ot: SPEC_VERSION, type: "trace.start", trace_id: this.traceId,
      seq: this.nextSeq(), time: now(), name: "opencode session",
      session_id: this.sessionID,
      producer: { name: "opencode", sdk: "opentrace-opencode-plugin" },
    });
    this.turnId = this.spanStart("turn", "turn", null);
    this.agentId = this.spanStart("agent", "OpenCode", this.turnId, {
      agent: { id: "main", name: "OpenCode", role: "orchestrator" },
    });
    this.started = true;
  }
  finish() {
    // close any open tools, the agent, and the turn, then end the trace.
    for (const [callID, spanId] of Object.entries(this.openTools)) {
      this.spanEnd(spanId, "tool", "ok");
      delete this.openTools[callID];
    }
    if (this.agentId) this.spanEnd(this.agentId, "agent", "ok");
    if (this.turnId) this.spanEnd(this.turnId, "turn", "ok");
    this.emit({
      ot: SPEC_VERSION, type: "trace.end", trace_id: this.traceId,
      seq: this.nextSeq(), time: now(), outcome: "success",
    });
  }
}

export const OpenTracePlugin = async ({ project, directory, worktree }) => {
  const baseDir =
    process.env.OPENTRACE_DIR ||
    path.join(directory || worktree || process.cwd(), ".opencode", "opentrace");
  /** @type {Record<string, Session>} */
  const sessions = {};

  function get(sessionID) {
    if (!sessionID) sessionID = "default";
    if (!sessions[sessionID]) {
      sessions[sessionID] = new Session(sessionID, baseDir);
      sessions[sessionID].start();
    }
    return sessions[sessionID];
  }

  return {
    // Server event bus.
    event: async ({ event }) => {
      try {
        const type = event?.type;
        const props = event?.properties || {};
        const sessionID = props.sessionID || props.info?.sessionID || props.info?.id;
        if (type === "session.created") {
          get(sessionID); // start trace lazily
        } else if (type === "session.idle") {
          const s = sessions[sessionID];
          if (s) {
            s.finish();
            delete sessions[sessionID];
          }
        } else if (type === "message.part.updated") {
          const part = props.part || {};
          const s = get(part.sessionID || sessionID);
          if (part.type === "reasoning" && part.text) {
            s.event("reasoning", s.agentId, {
              content: [{ type: "reasoning", text: part.text }],
            });
          } else if (part.type === "step-finish") {
            // token/cost accounting for the assistant step -> an llm span.
            const usage = part.tokens || part.usage || {};
            const llm = s.spanStart("llm", "chat", s.agentId, {
              model: part.model || part.modelID,
              provider: part.provider || part.providerID,
            });
            s.spanEnd(llm, "llm", "ok", {
              usage: clean({
                input_tokens: usage.input,
                output_tokens: usage.output,
                cache_read_tokens: usage.cache?.read,
                cache_creation_tokens: usage.cache?.write,
              }),
              cost: part.cost !== undefined ? { total: part.cost, currency: "USD" } : undefined,
            });
          } else if (part.type === "subtask") {
            // a sub-agent invocation -> handoff + nested agent (best effort).
            const to = part.agent || part.name || "subtask";
            const h = s.spanStart("handoff", `handoff -> ${to}`, s.agentId, {
              from_agent: "main", to_agent: to, mode: "delegate",
            });
            const sub = s.spanStart("agent", to, h, {
              agent: { id: part.id || to, name: to, role: "subagent" },
            });
            s.spanEnd(sub, "agent", "ok");
            s.spanEnd(h, "handoff", "ok");
          }
        }
      } catch (e) {
        process.stderr.write(`opentrace plugin event error: ${e}\n`);
      }
    },

    // Tool execution hooks (fire around every tool call).
    "tool.execute.before": async (input, output) => {
      try {
        const s = get(input.sessionID);
        const spanId = s.spanStart("tool", input.tool || "tool", s.agentId, {
          tool_name: input.tool,
          tool_call_id: input.callID,
          arguments: output?.args,
        });
        s.openTools[input.callID] = spanId;
      } catch (e) {
        process.stderr.write(`opentrace plugin before error: ${e}\n`);
      }
    },
    "tool.execute.after": async (input, output) => {
      try {
        const s = get(input.sessionID);
        const spanId = s.openTools[input.callID];
        if (!spanId) return;
        delete s.openTools[input.callID];
        const meta = output?.metadata || {};
        const isError = meta.error === true || output?.error !== undefined;
        s.spanEnd(spanId, "tool", isError ? "error" : "ok", {
          result: output?.output ?? output?.title,
        });
      } catch (e) {
        process.stderr.write(`opentrace plugin after error: ${e}\n`);
      }
    },
  };
};

export default OpenTracePlugin;
