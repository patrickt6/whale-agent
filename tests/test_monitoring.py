"""Freshness watchdog and dead-man's switch.

The failure being guarded against is silence: a source that quietly stops reporting, or
a digest that quietly never sends. Both are invisible without these checks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from whale_agent.config import Settings
from whale_agent.jobs.watchdog import build_report
from whale_agent.monitoring.health import digest_overdue, next_deadline, stale_sources
from whale_agent.storage.db import Store

NOW = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_fresh_source_is_not_stale(store):
    store.record_source_run("sec_form4", ok=True, event_count=5, at=NOW - timedelta(hours=2))
    assert stale_sources(store, now=NOW) == []


def test_source_past_its_cadence_is_stale(store):
    store.record_source_run("sec_form4", ok=True, at=NOW - timedelta(hours=72))
    stale = stale_sources(store, now=NOW)
    assert [s.name for s in stale] == ["sec_form4"]
    assert "last success" in stale[0].describe()


def test_a_failing_source_does_not_refresh_its_last_success(store):
    store.record_source_run("sec_form4", ok=True, at=NOW - timedelta(hours=72))
    store.record_source_run("sec_form4", ok=False, error="HTTP 500", at=NOW)
    stale = stale_sources(store, now=NOW)
    # Attempting and failing is not liveness; it is exactly the condition to alert on.
    assert stale[0].name == "sec_form4"
    assert stale[0].last_error == "HTTP 500"


def test_source_that_never_succeeded_is_stale(store):
    store.record_source_run("japan_edinet", ok=False, error="no key", at=NOW)
    stale = stale_sources(store, now=NOW)
    assert stale[0].last_success_at is None
    assert "no successful run" in stale[0].describe()


def test_only_filter_ignores_deliberately_disabled_sources(store):
    store.record_source_run("arkham", ok=True, at=NOW - timedelta(days=10))
    assert stale_sources(store, now=NOW, only={"sec_form4"}) == []


def test_digest_not_overdue_before_the_deadline(store):
    settings = Settings(digest_deadline_hour_utc=13)
    morning = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    assert digest_overdue(store, on=date(2026, 7, 25), now=morning, settings=settings) is False


def test_digest_overdue_after_the_deadline_with_no_delivery(store):
    settings = Settings(digest_deadline_hour_utc=13)
    assert digest_overdue(store, on=date(2026, 7, 25), now=NOW, settings=settings) is True


def test_recorded_delivery_clears_the_dead_mans_switch(store):
    settings = Settings(digest_deadline_hour_utc=13)
    store.record_digest_run(date(2026, 7, 25), "daily", "smtp", True, "sent")
    assert digest_overdue(store, on=date(2026, 7, 25), now=NOW, settings=settings) is False


def test_a_failed_delivery_does_not_clear_the_switch(store):
    settings = Settings(digest_deadline_hour_utc=13)
    store.record_digest_run(date(2026, 7, 25), "daily", "smtp", False, "auth error")
    assert digest_overdue(store, on=date(2026, 7, 25), now=NOW, settings=settings) is True


def test_next_deadline_rolls_to_tomorrow_once_passed():
    settings = Settings(digest_deadline_hour_utc=13)
    assert next_deadline(settings, NOW).date() == date(2026, 7, 26)
    morning = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    assert next_deadline(settings, morning).date() == date(2026, 7, 25)


def test_watchdog_report_is_clean_when_healthy(store, monkeypatch):
    store.record_digest_run(date(2026, 7, 25), "daily", "smtp", True)
    report, unhealthy = build_report(store, on=date(2026, 7, 25))
    assert unhealthy is False
    assert "healthy" in report


# --- Core versus non-core: which silences block a send, and which are disclosed -------

from whale_agent.jobs.watchdog import gate_report  # noqa: E402
from whale_agent.monitoring.health import (  # noqa: E402
    CORE_SOURCES,
    core_sources_stale,
    coverage_notes_for_stale_sources,
    non_core_stale,
)


def test_core_sources_are_the_us_backbone():
    assert frozenset({"sec_form4", "sec_13dg", "fmp_insider"}) == CORE_SOURCES


def test_stale_core_source_is_reported_by_the_gate(store):
    store.record_source_run("sec_form4", ok=True, at=NOW - timedelta(hours=72))
    assert [s.name for s in core_sources_stale(store, now=NOW)] == ["sec_form4"]


def test_stale_non_core_source_does_not_reach_the_gate(store):
    # taiwan_mops allows 72h; 96h is stale for it but must not block the send.
    store.record_source_run("taiwan_mops", ok=True, at=NOW - timedelta(hours=96))
    assert core_sources_stale(store, now=NOW) == []
    assert [s.name for s in non_core_stale(store, now=NOW)] == ["taiwan_mops"]


def test_a_core_source_never_appears_as_a_coverage_note(store):
    store.record_source_run("sec_form4", ok=True, at=NOW - timedelta(hours=72))
    assert coverage_notes_for_stale_sources(store, now=NOW) == []


def test_non_core_staleness_becomes_a_readable_coverage_note(store):
    store.record_source_run("taiwan_mops", ok=True, at=NOW - timedelta(hours=96))
    notes = coverage_notes_for_stale_sources(store, now=NOW)
    assert len(notes) == 1
    assert "taiwan_mops" in notes[0]
    assert "incomplete" in notes[0]


def test_a_source_with_no_run_on_record_is_not_invented(store):
    # An empty table means nothing was ever attempted, not that everything is stale.
    assert core_sources_stale(store, now=NOW) == []
    assert coverage_notes_for_stale_sources(store, now=NOW) == []


def test_gate_passes_when_core_sources_are_fresh(store):
    for name in ("sec_form4", "sec_13dg", "fmp_insider"):
        store.record_source_run(name, ok=True, at=NOW - timedelta(hours=2))
    _populate(store, MIN_WINDOW_EVENTS + 1)
    report, blocked = gate_report(store, now=NOW, on=ON)
    assert blocked is False
    assert "may send" in report


def test_gate_blocks_and_names_the_stale_core_source(store):
    store.record_source_run("sec_form4", ok=True, at=NOW - timedelta(hours=2))
    store.record_source_run("sec_13dg", ok=True, at=NOW - timedelta(hours=72))
    report, blocked = gate_report(store, now=NOW)
    assert blocked is True
    assert "BLOCKED" in report
    assert "sec_13dg" in report
    # The operator must be able to act without opening the database.
    assert "last success" in report


def test_gate_ignores_a_stale_non_core_source(store):
    store.record_source_run("sec_form4", ok=True, at=NOW - timedelta(hours=2))
    store.record_source_run("taiwan_mops", ok=True, at=NOW - timedelta(hours=96))
    _populate(store, MIN_WINDOW_EVENTS + 1)
    _, blocked = gate_report(store, now=NOW, on=ON)
    assert blocked is False


# --- Freshness is not completeness ----------------------------------------------------

from datetime import date as _date  # noqa: E402

from tests.conftest import make_event  # noqa: E402
from whale_agent.monitoring.health import MIN_WINDOW_EVENTS, thin_window  # noqa: E402

ON = _date(2026, 7, 25)


def _populate(store, n, on=ON):
    """n distinct events in the window. Filer names must differ: identical filer,
    issuer and amount is one event by dedup design, not n."""
    for i in range(n):
        store.upsert(
            make_event(
                event_id=f"e{i}",
                filer_name=f"Filer {i}",
                disclosure_date=on,
            )
        )


def test_a_populated_window_is_not_thin(store):
    _populate(store, MIN_WINDOW_EVENTS + 1)
    assert thin_window(store, on=ON) is None


def test_a_hole_in_the_corpus_is_reported_with_its_count(store):
    _populate(store, 9)
    assert thin_window(store, on=ON) == 9


def test_fresh_sources_do_not_excuse_an_empty_window(store):
    """The exact 2026-08-02 failure: ingest stopped, one run made everything look fine."""
    for name in ("sec_form4", "sec_13dg", "fmp_insider"):
        store.record_source_run(name, ok=True, at=NOW - timedelta(hours=1))
    _populate(store, 9)
    report, blocked = gate_report(store, now=NOW, on=ON)
    assert blocked is True
    assert "implausibly thin" in report
    assert "9 events" in report


def test_a_full_week_with_fresh_sources_sends(store):
    for name in ("sec_form4", "sec_13dg", "fmp_insider"):
        store.record_source_run(name, ok=True, at=NOW - timedelta(hours=1))
    _populate(store, MIN_WINDOW_EVENTS + 1)
    report, blocked = gate_report(store, now=NOW, on=ON)
    assert blocked is False
    assert "may send" in report


def test_a_stale_core_source_is_reported_ahead_of_thinness(store):
    """Both wrong at once: name the dead adapter, not the symptom."""
    store.record_source_run("sec_form4", ok=True, at=NOW - timedelta(hours=72))
    _populate(store, 9)
    report, blocked = gate_report(store, now=NOW, on=ON)
    assert blocked is True
    assert "core source has gone stale" in report


# --- A per-source volume floor ---------------------------------------------------------

from whale_agent.monitoring.health import EXPECTED_WEEKLY_EVENTS, silent_sources  # noqa: E402


def test_a_source_at_zero_is_visible_behind_a_healthy_total(store):
    """3,425 events from one source hid a second source at exactly nothing."""
    for i in range(200):
        store.upsert(
            make_event(
                event_id=f"e{i}", filer_name=f"F{i}", disclosure_date=ON, source="fmp_insider"
            )
        )
    silent = silent_sources(store, on=ON)
    assert "sec_edgar_13dg" in silent
    assert "fmp_insider" not in silent


def test_a_source_that_is_producing_is_not_reported(store):
    for i in range(200):
        store.upsert(
            make_event(
                event_id=f"a{i}", filer_name=f"A{i}", disclosure_date=ON, source="fmp_insider"
            )
        )
    for i in range(20):
        store.upsert(
            make_event(
                event_id=f"b{i}",
                filer_name=f"B{i}",
                disclosure_date=ON,
                source="sec_edgar_13dg",
            )
        )
    assert "sec_edgar_13dg" not in silent_sources(store, on=ON)


def test_every_expected_source_has_a_floor():
    assert set(EXPECTED_WEEKLY_EVENTS) >= {"fmp_insider", "sec_edgar_13dg", "sec_form4"}
    assert all(v > 0 for v in EXPECTED_WEEKLY_EVENTS.values())
