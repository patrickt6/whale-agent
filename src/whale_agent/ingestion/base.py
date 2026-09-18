"""Adapter ABC: every jurisdiction implements fetch -> parse -> normalize.

- fetch(): pull raw source documents (network). Returns opaque raw items.
- parse(raw): turn one raw document into intermediate dict(s). Pure, testable.
- normalize(parsed): produce NormalizedEvent(s). Pure, testable.

Golden-file tests exercise parse()/normalize() against saved fixtures, so a site
format change fails a test before it breaks production.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from whale_agent.models.event import NormalizedEvent


class Adapter(ABC):
    name: str
    jurisdiction: str

    @abstractmethod
    def fetch(self, **kwargs: Any) -> Iterable[Any]:
        """Pull raw source documents. Network-bound; not covered by golden tests."""

    @abstractmethod
    def parse(self, raw: Any) -> list[dict]:
        """Parse one raw document into intermediate dict rows (multi-row filings explode)."""

    @abstractmethod
    def normalize(self, parsed: dict) -> NormalizedEvent:
        """Turn one parsed row into a NormalizedEvent."""

    def ingest(self, **kwargs: Any) -> list[NormalizedEvent]:
        """Full pipeline for live use: fetch -> parse -> normalize, flattened."""
        events: list[NormalizedEvent] = []
        for raw in self.fetch(**kwargs):
            for row in self.parse(raw):
                events.append(self.normalize(row))
        return events
