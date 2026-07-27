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
