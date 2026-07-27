"""SQLite storage: idempotent upserts and first-time filer tracking."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import make_event
from whale_agent.storage.db import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_upsert_is_idempotent(store):
    ev = make_event()
    store.upsert(ev)
    store.upsert(ev)  # same key again
    assert store.count() == 1


def test_reingesting_a_run_does_not_duplicate(store):
    events = [make_event(filer_name=f"F{i}", filer_id=f"CIK{i}") for i in range(5)]
    store.upsert_many(events)
    store.upsert_many(events)
    assert store.count() == 5


def test_roundtrip_preserves_fields(store):
    ev = make_event(usd_value=8_000_000)
    store.upsert(ev)
    got = store.get(ev.event_id)
    assert got is not None
    assert got.usd_value == 8_000_000
    assert got.filer_name == ev.filer_name


def test_first_time_filer_tracking(store):
    ev = make_event(filer_id="CIK-NEW")
    assert ev.filer_id.lower() not in store.seen_filer_ids()
    store.record_filers([ev])
    assert "cik-new" in store.seen_filer_ids()


def test_events_since_filters_by_date(store):
    old = make_event(filer_id="OLD", disclosure_date=date(2026, 7, 1))
    new = make_event(filer_id="NEW", disclosure_date=date(2026, 7, 25))
    store.upsert_many([old, new])
    recent = store.events_since(date(2026, 7, 20))
    assert len(recent) == 1
    assert recent[0].filer_id == "NEW"


# -- filer observation history (feeds the unfamiliarity terms in scoring) ---------


def test_recording_filers_also_records_what_they_did(store):
    """`seen_filers` is one bit; ranking needs the shape of the relationship."""
    ev = make_event(filer_id="OBS", usd_value=7_000_000.0)
    store.record_filers([ev])
    rows = store.filer_observations("obs")
    assert len(rows) == 1
    assert rows[0]["usd_value"] == 7_000_000.0
    assert rows[0]["disclosure_date"] == ev.disclosure_date


def test_recording_the_same_run_twice_does_not_double_a_filers_history(store):
    """Backfills and cron overlaps must not inflate how often we have seen someone."""
    events = [
        make_event(
            filer_id="DUP",
            transaction_date=date(2026, 7, i + 1),
            disclosure_date=date(2026, 7, i + 1),
        )
        for i in range(3)
    ]
    store.record_filers(events)
    store.record_filers(events)
    assert len(store.filer_observations("dup")) == 3


def test_filer_observations_can_be_limited_to_what_preceded_a_date(store):
    """Scoring compares an event against its past, never against itself."""
    store.record_filers(
        [
            make_event(
                filer_id="SEQ",
                transaction_date=date(2026, 1, 5),
                disclosure_date=date(2026, 1, 5),
            ),
            make_event(
                filer_id="SEQ",
                transaction_date=date(2026, 6, 5),
                disclosure_date=date(2026, 6, 5),
            ),
        ]
    )
    assert len(store.filer_observations("seq", before=date(2026, 6, 5))) == 1


def test_history_span_reports_the_calendar_reach_of_what_we_hold(store):
    """The warm-up check needs elapsed time, not just row count."""
    store.record_filers(
        [
            make_event(
                filer_id="A",
                transaction_date=date(2026, 1, 1),
                disclosure_date=date(2026, 1, 1),
            ),
            make_event(
                filer_id="B",
                transaction_date=date(2026, 3, 2),
                disclosure_date=date(2026, 3, 2),
            ),
        ]
    )
    assert store.filer_history_span_days() == 60
    assert store.filer_observation_count() == 2


def test_an_empty_store_reports_no_history_span(store):
    """Cold start must answer cleanly rather than raise on a NULL aggregate."""
    assert store.filer_history_span_days() == 0
    assert store.filer_observation_count() == 0
