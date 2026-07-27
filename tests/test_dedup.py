"""Dedup + amendment handling tests."""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.scoring.dedup import dedup_events


def test_identical_events_collapse_to_one():
    a = make_event()
    b = make_event()
    assert a.stable_key() == b.stable_key()
    assert len(dedup_events([a, b])) == 1


def test_different_share_counts_are_distinct_events():
    a = make_event(share_count=100)
    b = make_event(share_count=200)
    assert len({e.stable_key() for e in (a, b)}) == 2
    assert len(dedup_events([a, b])) == 2


def test_amendment_supersedes_original():
    original = make_event(disclosure_date=date(2026, 7, 25))
    amendment = make_event(disclosure_date=date(2026, 7, 26), is_amendment=True)
    result = dedup_events([original, amendment])
    assert len(result) == 1
    winner = result[0]
    assert winner.is_amendment is True
    assert winner.supersedes_id == original.event_id


def test_later_disclosure_wins_when_neither_is_amendment():
    early = make_event(disclosure_date=date(2026, 7, 20))
    late = make_event(disclosure_date=date(2026, 7, 25))
    result = dedup_events([early, late])
    assert len(result) == 1
    assert result[0].disclosure_date == date(2026, 7, 25)
