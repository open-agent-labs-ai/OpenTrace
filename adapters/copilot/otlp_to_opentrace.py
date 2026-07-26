#!/usr/bin/env python3
"""GitHub Copilot (CLI + VS Code Copilot Chat) OTLP GenAI -> OpenTrace.

Copilot emits standard OpenTelemetry **GenAI** spans, so this adapter simply
INGESTS Copilot's OTLP/JSON output and reuses the generic reverse-mapping
converter in ``adapters/opentelemetry-ingest/otlp_to_opentrace.py``.

Enable Copilot's file OTLP exporter (see README.md), which writes an
OTLP/JSON-lines traces file, then run this script on it.

Usage:
    python otlp_to_opentrace.py copilot-traces.jsonl -o copilot.ot.jsonl --validate

Copilot-specific niceties on top of the generic converter:
- tolerates Copilot's JSON-lines OTLP file (one ExportTraceServiceRequest/line);
- promotes a couple of Copilot resource attributes
  (``gen_ai.system``/``service.name``) into the trace name when present.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Reuse the generic ingest module regardless of how this file is invoked.
_HERE = os.path.dirname(os.path.abspath(__file__))
_GENERIC = os.path.normpath(os.path.join(_HERE, "..", "opentelemetry-ingest"))
if _GENERIC not in sys.path:
    sys.path.insert(0, _GENERIC)

import otlp_to_opentrace as generic  # noqa: E402  (path injected above)


def convert(path: str):
    """Convert a Copilot OTLP/JSON(-lines) traces file to OpenTrace records."""
    return generic.convert_file(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert GitHub Copilot OTLP GenAI traces to OpenTrace")
    ap.add_argument("input", help="Copilot OTLP/JSON traces file, or - for stdin")
    ap.add_argument("-o", "--output", help="output .ot.jsonl (default stdout)")
    ap.add_argument("--validate", action="store_true",
                    help="validate output with the OpenTrace SDK if importable")
    args = ap.parse_args(argv)

    records = convert(args.input)
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    text = "\n".join(lines) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("wrote %d records to %s" % (len(records), args.output), file=sys.stderr)
    else:
        sys.stdout.write(text)

    if args.validate:
        try:
            from opentrace import validate_stream
        except ImportError:
            print("opentrace SDK not importable; skipping validation", file=sys.stderr)
            return 0
        count, problems = validate_stream(iter(lines))
        errs = [p for p in problems if "warning" not in p]
        for p in problems:
            print(("ERROR " if "warning" not in p else "WARN  ") + p, file=sys.stderr)
        print("validated %d records, %d error(s)" % (count, len(errs)), file=sys.stderr)
        return 1 if errs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
