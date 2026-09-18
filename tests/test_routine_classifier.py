"""Cohen-Malloy-Pomorski opportunistic/routine classification.

`classify_routine` (the boolean, scoring-facing heuristic) already had boundary
coverage nowhere in the suite -- it had zero importers, hence zero tests. These cover
both the boolean heuristic and the tri-state `routine_label` built on top of it, which
is what the weekly report now displays: the tri-state form must never collapse
"insufficient history" into "opportunistic", since that would be an unsourced claim.
"""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.enrichment.routine_classifier import (
    attach_routine_labels,
    classify_routine,
    label_for_event,
    routine_label,
)
from whale_agent.models.enums import TransactionType
from whale_agent.storage.db import Store, filer_key_for


def _dates(*ymd: tuple[int, int, int]) -> list[date]:
    return [date(y, m, d) for y, m, d in ymd]


# -- classify_routine (boolean, scoring-facing) ------------------------------------
def test_same_month_across_three_years_is_routine():
    dates = _dates((2023, 3, 15), (2024, 3, 10), (2025, 3, 20))
    assert classify_routine(dates) is True


def test_scattered_months_is_not_routine():
    dates = _dates((2023, 3, 15), (2024, 7, 10), (2025, 11, 20))
    assert classify_routine(dates) is False


def test_below_min_years_defaults_false():
    dates = _dates((2024, 3, 15), (2025, 3, 20))  # only two distinct years
    assert classify_routine(dates) is False


# -- routine_label (tri-state, display-facing) -------------------------------------
def test_routine_label_matches_the_boolean_when_history_is_sufficient():
    routine_dates = _dates((2023, 3, 15), (2024, 3, 10), (2025, 3, 20))
    assert routine_label(routine_dates) == "routine"

    opportunistic_dates = _dates((2023, 3, 15), (2024, 7, 10), (2025, 11, 20))
    assert routine_label(opportunistic_dates) == "opportunistic"


def test_routine_label_is_unknown_rather_than_a_default_guess():
    # classify_routine(dates) would silently return False here -- routine_label must
    # not let that read as "opportunistic", a claim the data does not support.
    dates = _dates((2024, 3, 15), (2025, 3, 20))
    assert routine_label(dates) == "unknown"


def test_routine_label_is_unknown_on_no_history():
    assert routine_label([]) == "unknown"


# -- store-driven lookup -------------------------------------------------------------
def test_label_for_event_reads_filer_history_from_the_store():
    store = Store(":memory:")
    try:
        event = make_event(
            filer_name="Serial Filer",
            transaction_type=TransactionType.OPEN_MARKET_BUY,
            disclosure_date=date(2026, 3, 18),
        )
        key = filer_key_for(event)
        store.conn.execute(
            "INSERT INTO filer_observations (filer_key, event_id, disclosure_date, usd_value) "
            "VALUES (?, ?, ?, ?)",
            (key, "prior-1", "2023-03-01", 5_000_000.0),
        )
        store.conn.execute(
            "INSERT INTO filer_observations (filer_key, event_id, disclosure_date, usd_value) "
            "VALUES (?, ?, ?, ?)",
            (key, "prior-2", "2024-03-05", 5_000_000.0),
        )
        store.conn.execute(
            "INSERT INTO filer_observations (filer_key, event_id, disclosure_date, usd_value) "
            "VALUES (?, ?, ?, ?)",
            (key, "prior-3", "2025-03-10", 5_000_000.0),
        )
        store.conn.commit()

        assert label_for_event(store, event) == "routine"
    finally:
        store.close()


def test_label_for_event_is_unknown_on_a_cold_store():
    store = Store(":memory:")
    try:
        event = make_event(filer_name="Never Seen Before")
        assert label_for_event(store, event) == "unknown"
    finally:
        store.close()


def test_attach_routine_labels_sets_the_field_without_touching_is_routine():
    store = Store(":memory:")
    try:
        event = make_event(filer_name="Fresh Filer")
        assert event.routine_label is None
        assert event.is_routine is False

        attach_routine_labels(store, [event])

        assert event.routine_label == "unknown"  # cold store: no history to judge
        assert event.is_routine is False  # scoring's flag is untouched
    finally:
        store.close()
