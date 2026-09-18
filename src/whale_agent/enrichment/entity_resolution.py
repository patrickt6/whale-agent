"""Entity resolution: map filers to notability and detect first-time filers.

Phase 0 is deliberately thin: notability comes from the seed registry, and first-time
detection is a set-membership check against filer ids already seen in storage. ISIN/
LEI/CUSIP cross-listing dedup is stubbed for a later phase.
"""

from __future__ import annotations

import unicodedata

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


def display_name(name: str) -> str:
    """Normalize a filer or issuer name for display.

    NFKC folds fullwidth Latin to ASCII, which matters more than it sounds: several
    Japanese and Taiwanese filings carry ordinary English company names typed in
    fullwidth characters, so "Ｆｉｄｅｌｉｔｙ　Ｍａｎａｇｅｍｅｎｔ" is not a translation
    problem at all -- it is the same string in a different width, and folding it makes it
    readable without inventing anything.

    Names genuinely written in kanji are left alone. Transliterating them here would be
    this codebase asserting a spelling no filing supplied, which is the same class of
    error as inventing a figure. Where a real English name exists, the adapter reads it
    from the filing instead.
    """
    if not name:
        return name
    return " ".join(unicodedata.normalize("NFKC", name).split())
