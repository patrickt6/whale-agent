"""Filer history computed from our own store, and the cold-start suppression."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import make_event
from whale_agent.enrichment.filer_history import (
    MIN_OBSERVATIONS_FOR_HISTORY,
    attach_filer_history,
    history_is_warm,
    load_filer_history,
    mark_first_time,
)
from whale_agent.storage.db import Store

TODAY = date(2026, 7, 25)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _warm(store: Store, start: date = date(2025, 1, 1)) -> None:
    """Fill the store past both warm-up thresholds with unrelated filers."""
    events = [
        make_event(
            filer_name=f"Filler {i}",
            filer_id=f"FILL{i}",
            disclosure_date=start + timedelta(days=i % 200),
            usd_value=10_000_000.0,
        )
        for i in range(MIN_OBSERVATIONS_FOR_HISTORY + 10)
    ]
    store.record_filers(events)


def test_a_filer_we_have_never_recorded_has_no_history(store):
    """An unseen filer reports zero priors and None for everything unmeasurable."""
    hist = load_filer_history(store, make_event(filer_id="NEW"), TODAY)
    assert hist.prior_filings == 0
    assert hist.is_unseen
    assert hist.last_seen is None
    assert hist.days_since_last is None
    assert hist.typical_usd is None


def test_history_counts_prior_filings_and_takes_the_median_position_size(store):
    """Typical size is the median of past positions, not the mean or the latest."""
    past = [
        make_event(
            filer_id="REPEAT",
            filer_name="Repeat LP",
            transaction_date=date(2026, 1, i + 1),
            disclosure_date=date(2026, 1, i + 1),
            usd_value=size,
        )
        for i, size in enumerate([5_000_000.0, 8_000_000.0, 40_000_000.0])
    ]
    store.record_filers(past)
    hist = load_filer_history(store, make_event(filer_id="REPEAT"), TODAY)
    assert hist.prior_filings == 3
    assert hist.typical_usd == 8_000_000.0
    assert hist.last_seen == date(2026, 1, 3)


def test_history_excludes_the_event_being_scored_from_its_own_past(store):
    """Re-scoring a stored event must not let it find itself and stop being a debut."""
    ev = make_event(filer_id="SELF", disclosure_date=date(2026, 7, 25))
    store.record_filers([ev])
    hist = load_filer_history(store, ev)
    assert hist.prior_filings == 0


def test_days_since_last_measures_the_gap_to_the_reference_date(store):
    """The 18-month gap signal is only as good as this arithmetic."""
    old = make_event(
        filer_id="DORMANT",
        transaction_date=date(2024, 11, 1),
        disclosure_date=date(2024, 11, 1),
    )
    store.record_filers([old])
    hist = load_filer_history(store, make_event(filer_id="DORMANT"), TODAY)
    assert hist.days_since_last == (TODAY - date(2024, 11, 1)).days
    assert hist.is_returning_after_gap()


def test_size_versus_typical_is_none_when_there_is_nothing_to_compare_against(store):
    """No median means no claim -- not a ratio of one."""
    hist = load_filer_history(store, make_event(filer_id="BLANK"), TODAY)
    assert hist.size_vs_typical(50_000_000.0) is None


# -- cold start ------------------------------------------------------------------


def test_a_fresh_store_is_not_warm_enough_to_call_anyone_a_first_time_filer(store):
    """The bug: an empty store used to declare every filer a debut."""
    assert not history_is_warm(store)
    events = [make_event(filer_name=f"F{i}", filer_id=f"CIK{i}") for i in range(5)]
    seen = store.seen_filer_ids()
    marked = [mark_first_time(e, seen, history_is_warm=history_is_warm(store)) for e in events]
    assert not any(e.is_first_time_filer for e in marked)


def test_a_warm_store_does_flag_a_genuinely_new_filer(store):
    """Suppression must be a cold-start guard, not a permanent disabling of the signal."""
    _warm(store)
    assert history_is_warm(store)
    newcomer = make_event(filer_name="Newcomer LP", filer_id="NEWCOMER")
    mark_first_time(newcomer, store.seen_filer_ids(), history_is_warm=True)
    assert newcomer.is_first_time_filer


def test_a_warm_store_does_not_flag_a_filer_it_has_already_seen(store):
    """The set-membership check still does its job once history exists."""
    _warm(store)
    known = make_event(filer_name="Filler 3", filer_id="FILL3")
    mark_first_time(known, store.seen_filer_ids(), history_is_warm=True)
    assert not known.is_first_time_filer


def test_a_busy_afternoon_alone_does_not_make_the_store_warm(store):
    """Volume without elapsed time cannot tell us a quarterly filer is absent."""
    same_day = [
        make_event(
            filer_name=f"Burst {i}",
            filer_id=f"BURST{i}",
            disclosure_date=date(2026, 7, 25),
        )
        for i in range(MIN_OBSERVATIONS_FOR_HISTORY + 50)
    ]
    store.record_filers(same_day)
    assert store.filer_observation_count() > MIN_OBSERVATIONS_FOR_HISTORY
    assert not history_is_warm(store)


# -- attaching to context --------------------------------------------------------


def test_attaching_history_to_a_cold_store_leaves_context_none_not_zero(store):
    """Zero would read as "never seen before" and promote every event on day one."""
    ev = make_event(filer_id="ANY")
    attach_filer_history(store, [ev], TODAY)
    assert ev.context.filer_prior_filings is None
    assert ev.context.filer_typical_usd is None


def test_attaching_history_to_a_warm_store_populates_the_context_fields(store):
    """The scoring layer reads these fields; enrichment is what puts them there."""
    _warm(store)
    store.record_filers(
        [
            make_event(
                filer_id="TRACKED",
                transaction_date=date(2026, 5, 1),
                disclosure_date=date(2026, 5, 1),
                usd_value=12_000_000.0,
            )
        ]
    )
    ev = make_event(filer_id="TRACKED", usd_value=60_000_000.0)
    attach_filer_history(store, [ev], TODAY)
    assert ev.context.filer_prior_filings == 1
    assert ev.context.filer_typical_usd == 12_000_000.0
    assert ev.context.filer_days_since_last == (TODAY - date(2026, 5, 1)).days
