#!/usr/bin/env node
/**
 * `opentrace` command-line tool: validate, stats, tree, convert.
 *
 *   opentrace validate trace.ot.jsonl
 *   opentrace stats    trace.ot.jsonl
 *   opentrace tree     trace.ot.jsonl
 *   opentrace convert  trace.ot.jsonl --to otlp -o trace.otlp.json
 *   opentrace convert  trace.ot.jsonl --to otlp --endpoint http://localhost:4318
 */
import { readFileSync, writeFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { TraceModel, load, splitTraces } from "./analyze.js";
import { recordsToOtlp } from "./otel.js";
import { validateStream } from "./validate.js";

type Dict = { [key: string]: any };

function readLines(path: string): string[] {
  const raw = readFileSync(path);
  const text = path.endsWith(".gz")
    ? gunzipSync(raw).toString("utf-8")
    : raw.toString("utf-8");
  return text.split("\n");
}

function cmdValidate(file: string, strict: boolean): number {
  const { count, problems } = validateStream(readLines(file), strict);
  const errors = problems.filter((p) => !p.includes("warning"));
  const warnings = problems.filter((p) => p.includes("warning"));
  for (const w of warnings) console.log("WARN  " + w);
  for (const e of errors) console.log("ERROR " + e);
  console.log(
    `\n${count} records, ${errors.length} error(s), ${warnings.length} warning(s)`
  );
  return errors.length ? 1 : 0;
}

function cmdStats(file: string): number {
  const records = load(file);
  for (const recs of splitTraces(records)) {
    const r = new TraceModel(recs).rollup();
    console.log(`trace ${r.trace_id}  (${r.name})`);
    console.log(`  outcome   : ${r.outcome}`);
    console.log(`  duration  : ${r.duration_ms} ms`);
    console.log(`  spans     : ${r.spans}   events: ${r.events}   errors: ${r.errors}`);
    console.log(
      "  by kind   : " +
        Object.entries(r.by_kind)
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([k, v]) => `${k}=${v}`)
          .join(", ")
    );
    console.log(
      `  tokens    : in=${r.tokens.input_tokens} out=${r.tokens.output_tokens} total=${r.tokens.total_tokens}`
    );
    console.log(`  cost      : $${r.cost.toFixed(6)}`);
    console.log("");
  }
  return 0;
}

function cmdTree(file: string): number {
  const records = load(file);
  for (const recs of splitTraces(records)) {
    const model = new TraceModel(recs);
    if (model.header) {
      console.log(`● ${model.header.name}  [${(model.traceId ?? "").slice(0, 12)}]`);
    }
    const walk = (sid: string, depth: number): void => {
      const s = model.spans[sid];
      let dur = "";
      if (typeof s.duration_ms === "number") dur = `  ${Math.round(s.duration_ms)}ms`;
      const usage = s.usage ?? {};
      const tok = usage.total_tokens ? `  ${usage.total_tokens}tok` : "";
      const status = (s.status ?? {}).code ?? "";
      const mark = status === "error" ? "✗" : "•";
      const label = s.name ?? s.kind;
      console.log(
        "  ".repeat(depth + 1) + `${mark} [${s.kind}] ${label}${dur}${tok}`
      );
      for (const ev of model.eventsBySpan[sid] ?? []) {
        console.log("  ".repeat(depth + 2) + `◦ ${ev.name}`);
      }
      for (const child of s.children) walk(child, depth + 1);
    };
    for (const root of model.roots()) walk(root, 0);
    console.log("");
  }
  return 0;
}

async function cmdConvert(
  file: string,
  to: string,
  output: string | undefined,
  endpoint: string | undefined,
  pretty: boolean
): Promise<number> {
  const records = load(file);
  if (to !== "otlp") {
    console.error(`unsupported target: ${to}`);
    return 2;
  }
  const payload = recordsToOtlp(records);
  if (endpoint) return postOtlp(payload, endpoint);
  const text = JSON.stringify(payload, null, pretty ? 2 : undefined);
  if (output) {
    writeFileSync(output, text);
    console.log(`wrote ${output}`);
  } else {
    console.log(text);
  }
  return 0;
}

async function postOtlp(payload: Dict, endpoint: string): Promise<number> {
  let url = endpoint.replace(/\/+$/, "");
  if (!url.endsWith("/v1/traces")) url += "/v1/traces";
  try {
    // Node 18+ ships a global fetch; fall back to a clear message otherwise.
    const f = (globalThis as any).fetch as typeof fetch | undefined;
    if (!f) {
      console.error("global fetch unavailable (need Node 18+) — write to a file with -o instead");
      return 1;
    }
    const resp = await f(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    console.log(`POST ${url} -> ${resp.status}`);
    return resp.ok ? 0 : 1;
  } catch (e) {
    console.error(`failed to POST to ${url}: ${(e as Error).message}`);
    return 1;
  }
}

function parseFlags(args: string[]): { positional: string[]; flags: Dict } {
  const positional: string[] = [];
  const flags: Dict = {};
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--strict") flags.strict = true;
    else if (a === "--pretty") flags.pretty = true;
    else if (a === "--to") flags.to = args[++i];
    else if (a === "-o" || a === "--output") flags.output = args[++i];
    else if (a === "--endpoint") flags.endpoint = args[++i];
    else positional.push(a);
  }
  return { positional, flags };
}

function usage(): void {
  console.log(
    [
      "OpenTrace CLI",
      "",
      "Usage:",
      "  opentrace validate <file> [--strict]",
      "  opentrace stats    <file>",
      "  opentrace tree     <file>",
      "  opentrace convert  <file> --to otlp [-o out.json] [--endpoint URL] [--pretty]",
    ].join("\n")
  );
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  const [cmd, ...rest] = argv;
  const { positional, flags } = parseFlags(rest);
  const file = positional[0];
  switch (cmd) {
    case "validate":
      return cmdValidate(file, !!flags.strict);
    case "stats":
      return cmdStats(file);
    case "tree":
      return cmdTree(file);
    case "convert":
      return cmdConvert(file, flags.to ?? "otlp", flags.output, flags.endpoint, !!flags.pretty);
    default:
      usage();
      return 0;
  }
}

// Execute when run directly as the `opentrace` bin.
main()
  .then((code) => process.exit(code))
  .catch((e) => {
    console.error(e);
    process.exit(1);
  });
