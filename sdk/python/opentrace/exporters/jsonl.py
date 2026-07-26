"""JSONL exporter: writes one OpenTrace record per line to a file or stream."""
from __future__ import annotations

import gzip
import io
import json
import sys
import threading
from typing import Any, Dict, Optional

from .base import Exporter


class JsonlExporter(Exporter):
    """Append OpenTrace records as JSON Lines.

    Parameters
    ----------
    path:
        File path to write to. If it ends with ``.gz`` the output is gzip-compressed.
        If ``None`` and ``stream`` is None, writes to stdout.
    stream:
        A writable text stream to use instead of a path.
    flush:
        Flush after every record (recommended for live/streaming traces).
    """

    def __init__(self, path: Optional[str] = None, stream=None, flush: bool = True):
        self._lock = threading.Lock()
        self._flush = flush
        self._owns_stream = False
        if stream is not None:
            self._fh = stream
        elif path is None:
            self._fh = sys.stdout
        elif path.endswith(".gz"):
            self._fh = io.TextIOWrapper(gzip.open(path, "wb"), encoding="utf-8")
            self._owns_stream = True
        else:
            self._fh = open(path, "a", encoding="utf-8")
            self._owns_stream = True

    def export(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                          default=_json_default)
        with self._lock:
            self._fh.write(line)
            self._fh.write("\n")
            if self._flush:
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._owns_stream:
                self._fh.close()
            else:
                self._fh.flush()


def _json_default(obj):
    # Best-effort serialization for non-JSON-native values.
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)
