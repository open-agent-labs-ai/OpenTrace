"""``opentrace`` command-line tool: validate, stats, tree, convert.

    opentrace validate trace.ot.jsonl
    opentrace stats    trace.ot.jsonl
    opentrace tree     trace.ot.jsonl
    opentrace convert  trace.ot.jsonl --to otlp -o trace.otlp.json
    opentrace convert  trace.ot.jsonl --to otlp --endpoint http://localhost:4318
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from typing import List

from .analyze import TraceModel, load, split_traces
from .otel_json import records_to_otlp
from .validate import validate_stream


def _open_lines(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            yield line


def cmd_validate(args) -> int:
    count, problems = validate_stream(_open_lines(args.file), strict=args.strict)
    errors = [p for p in problems if "warning" not in p]
    warnings = [p for p in problems if "warning" in p]
    for w in warnings:
        print("WARN  " + w)
    for e in errors:
        print("ERROR " + e)
    print("\n%d records, %d error(s), %d warning(s)" % (count, len(errors), len(warnings)))
    return 1 if errors else 0


def cmd_stats(args) -> int:
    records = load(args.file)
    for recs in split_traces(records):
        model = TraceModel(recs)
        r = model.rollup()
        print("trace %s  (%s)" % (r["trace_id"], r["name"]))
        print("  outcome   : %s" % r["outcome"])
        print("  duration  : %s ms" % r["duration_ms"])
        print("  spans     : %d   events: %d   errors: %d" % (r["spans"], r["events"], r["errors"]))
        print("  by kind   : " + ", ".join("%s=%d" % (k, v) for k, v in sorted(r["by_kind"].items())))
        print("  tokens    : in=%d out=%d total=%d" % (
            r["tokens"]["input_tokens"], r["tokens"]["output_tokens"], r["tokens"]["total_tokens"]))
        print("  cost      : $%.6f" % r["cost"])
        print("")
    return 0


def cmd_tree(args) -> int:
    records = load(args.file)
    for recs in split_traces(records):
        model = TraceModel(recs)
        if model.header:
            print("● %s  [%s]" % (model.header.get("name"), model.trace_id[:12]))

        def walk(sid: str, depth: int):
            s = model.spans[sid]
            dur = ""
            if s.get("duration_ms") is not None:
                dur = "  %.0fms" % s["duration_ms"]
            elif s.get("start_time") and s.get("end_time"):
                dur = ""
            usage = s.get("usage") or {}
            tok = ("  %dtok" % usage["total_tokens"]) if usage.get("total_tokens") else ""
            status = (s.get("status") or {}).get("code", "")
            mark = "✗" if status == "error" else "•"
            label = s.get("name") or s.get("kind")
            print("  " * (depth + 1) + "%s [%s] %s%s%s" % (mark, s.get("kind"), label, dur, tok))
            for ev in model.events_by_span.get(sid, []):
                print("  " * (depth + 2) + "◦ %s" % ev.get("name"))
            for child in s["children"]:
                walk(child, depth + 1)

        for root in model.roots():
            walk(root, 0)
        print("")
    return 0


def cmd_convert(args) -> int:
    records = load(args.file)
    if args.to != "otlp":
        print("unsupported target: %s" % args.to, file=sys.stderr)
        return 2
    payload = records_to_otlp(records)
    if args.endpoint:
        return _post_otlp(payload, args.endpoint)
    text = json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("wrote %s" % args.output)
    else:
        print(text)
    return 0


def _post_otlp(payload, endpoint: str) -> int:
    import urllib.request
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url = url + "/v1/traces"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("POST %s -> %s" % (url, resp.status))
        return 0
    except Exception as e:  # pragma: no cover
        print("failed to POST to %s: %s" % (url, e), file=sys.stderr)
        return 1


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(prog="opentrace", description="OpenTrace CLI")
    sub = p.add_subparsers(dest="cmd")

    pv = sub.add_parser("validate", help="validate a trace file")
    pv.add_argument("file")
    pv.add_argument("--strict", action="store_true", help="treat unknown enums as errors")
    pv.set_defaults(func=cmd_validate)

    ps = sub.add_parser("stats", help="print token/cost/span rollups")
    ps.add_argument("file")
    ps.set_defaults(func=cmd_stats)

    pt = sub.add_parser("tree", help="print the span tree")
    pt.add_argument("file")
    pt.set_defaults(func=cmd_tree)

    pc = sub.add_parser("convert", help="convert to OTLP/JSON (and optionally POST it)")
    pc.add_argument("file")
    pc.add_argument("--to", default="otlp", choices=["otlp"])
    pc.add_argument("-o", "--output")
    pc.add_argument("--endpoint", help="OTLP/HTTP endpoint, e.g. http://localhost:4318")
    pc.add_argument("--pretty", action="store_true")
    pc.set_defaults(func=cmd_convert)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
