"""Deduplication and amendment handling.

- Stable event key via NormalizedEvent.stable_key().
- Amendments supersede originals: keep both, mark original amended, surface latest.
- Multi-row filings are exploded upstream by the adapter; this module only dedups.
"""

from __future__ import annotations

from whale_agent.models.event import NormalizedEvent


def dedup_events(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
    """Collapse duplicate events by stable key.

    When two events share a key, the later disclosure_date wins. If a winner is an
    amendment, the superseded original is marked amended and dropped from output but
    the winner records supersedes_id (history retention is the storage layer's job).
    """
    by_key: dict[str, NormalizedEvent] = {}
    for ev in events:
        key = ev.stable_key()
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = ev
            continue
        winner, loser = _pick_winner(existing, ev)
        if winner.is_amendment and not loser.is_amendment:
            winner.supersedes_id = loser.event_id
            loser.amended = True
        by_key[key] = winner
    return list(by_key.values())


def _pick_winner(
    a: NormalizedEvent, b: NormalizedEvent
) -> tuple[NormalizedEvent, NormalizedEvent]:
    """Return (winner, loser). Amendments win; otherwise later disclosure_date wins."""
    if a.is_amendment != b.is_amendment:
        return (a, b) if a.is_amendment else (b, a)
    if b.disclosure_date >= a.disclosure_date:
        return b, a
    return a, b
