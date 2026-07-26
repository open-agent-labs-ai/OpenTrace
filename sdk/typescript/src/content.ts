/**
 * Helpers for building OpenTrace content parts and messages.
 *
 * These produce plain objects matching the spec's message/part model (§11), which
 * is identical to the OpenTelemetry GenAI structured message-part model. Every key
 * emitted here is already snake_case, matching the Python SDK's `content.py`.
 */
import { REDACTED } from "./constants.js";

export type Role = "system" | "user" | "assistant" | "tool";

/** A single content part (text, reasoning, tool_call, ...). */
export interface Part {
  type: string;
  [key: string]: unknown;
}

/** A message: a role plus an ordered list of content parts. */
export interface Message {
  role: string;
  parts: Part[];
  name?: string;
  finish_reason?: string;
  [key: string]: unknown;
}

/** Content accepted by message builders: a string, one part, or a list of parts. */
export type Content = string | Part | Array<string | Part> | null | undefined;

// --- content parts ---------------------------------------------------------

export function text(value: string): Part {
  return { type: "text", text: value };
}

export function reasoning(
  value: string,
  signature?: string,
  redacted = false
): Part {
  const part: Part = { type: "reasoning" };
  part.text = redacted ? REDACTED : value;
  if (redacted) part.redacted = true;
  if (signature !== undefined) part.signature = signature;
  return part;
}

export function toolCall(
  toolCallId: string,
  name: string,
  args: unknown
): Part {
  return {
    type: "tool_call",
    tool_call_id: toolCallId,
    name,
    arguments: args,
  };
}

export function toolCallResponse(
  toolCallId: string,
  result: unknown,
  isError = false
): Part {
  const part: Part = {
    type: "tool_call_response",
    tool_call_id: toolCallId,
    result,
  };
  if (isError) part.is_error = true;
  return part;
}

export function blob(
  mimeType: string,
  data?: string | null,
  redacted = false
): Part {
  return {
    type: "blob",
    mime_type: mimeType,
    data: redacted || data == null ? REDACTED : data,
  };
}

export function file(
  mimeType: string,
  name: string,
  uriValue?: string,
  nbytes?: number
): Part {
  const part: Part = { type: "file", mime_type: mimeType, name };
  if (uriValue !== undefined) part.uri = uriValue;
  if (nbytes !== undefined) part.bytes = nbytes;
  return part;
}

export function uri(value: string, mimeType?: string): Part {
  const part: Part = { type: "uri", uri: value };
  if (mimeType !== undefined) part.mime_type = mimeType;
  return part;
}

export function compaction(summary: string, replacedCount?: number): Part {
  const part: Part = { type: "compaction", summary };
  if (replacedCount !== undefined) part.replaced_count = replacedCount;
  return part;
}

// --- messages --------------------------------------------------------------

function asParts(content: Content): Part[] {
  if (content == null) return [];
  if (typeof content === "string") return [text(content)];
  if (Array.isArray(content)) {
    return content.map((c) => (typeof c === "string" ? text(c) : c));
  }
  return [content];
}

export interface MessageOptions {
  name?: string;
  finishReason?: string;
}

export function message(
  role: string,
  content: Content,
  opts: MessageOptions = {}
): Message {
  const msg: Message = { role, parts: asParts(content) };
  if (opts.name !== undefined) msg.name = opts.name;
  if (opts.finishReason !== undefined) msg.finish_reason = opts.finishReason;
  return msg;
}

export function system(content: Content, opts: MessageOptions = {}): Message {
  return message("system", content, opts);
}

export function user(content: Content, opts: MessageOptions = {}): Message {
  return message("user", content, opts);
}

export function assistant(
  content: Content,
  opts: MessageOptions = {}
): Message {
  return message("assistant", content, opts);
}

export function tool(content: Content, opts: MessageOptions = {}): Message {
  return message("tool", content, opts);
}
