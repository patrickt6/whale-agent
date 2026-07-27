"""Entity resolution: map filers to notability and detect first-time filers.

Phase 0 is deliberately thin: notability comes from the seed registry, and first-time
detection is a set-membership check against filer ids already seen in storage. ISIN/
LEI/CUSIP cross-listing dedup is stubbed for a later phase.
"""

from __future__ import annotations

from whale_agent.models.entity import lookup_notability
from whale_agent.models.event import NormalizedEvent


def enrich_notability(event: NormalizedEvent) -> NormalizedEvent:
    """Attach nothing structural yet; notability is read at scoring time by name.

    Kept as a seam so a richer resolver (id-based) can replace name lookup later.
    """
    return event


def notability_for(event: NormalizedEvent) -> float:
    return lookup_notability(event.filer_name)


def mark_first_time(event: NormalizedEvent, seen_filer_ids: set[str]) -> NormalizedEvent:
    """Set is_first_time_filer if this filer has not been seen before."""
    key = (event.filer_id or event.filer_name).strip().lower()
    event.is_first_time_filer = key not in seen_filer_ids
    return event
