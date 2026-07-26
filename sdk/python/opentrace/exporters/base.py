"""Exporter interface. An exporter receives finished OpenTrace records (dicts)."""
from __future__ import annotations

from typing import Any, Dict, List


class Exporter:
    """Base exporter. Subclasses implement ``export`` and optionally ``close``."""

    def export(self, record: Dict[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class MultiExporter(Exporter):
    """Fan a record out to several exporters (e.g. jsonl file + live OTLP)."""

    def __init__(self, exporters: List[Exporter]):
        self._exporters = list(exporters)

    def export(self, record: Dict[str, Any]) -> None:
        for exp in self._exporters:
            exp.export(record)

    def close(self) -> None:
        for exp in self._exporters:
            exp.close()


class MemoryExporter(Exporter):
    """Collect records in memory. Useful for tests and in-process analysis."""

    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    def export(self, record: Dict[str, Any]) -> None:
        self.records.append(record)
