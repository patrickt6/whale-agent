"""The weekly must survive a scheduled run that starts hours late.

Written after 2026-08-03, when GitHub started the 11:00 UTC cron at 13:43 UTC. The old
gate matched the local hour exactly, saw 09:43, skipped every step and reported success.
Nothing sent, nothing alerted.

These tests pin the replacement property: lateness is harmless, and a second run in the
same week cannot produce a second email.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from whale_agent.jobs.schedule_gate import MIN_LOCAL_HOUR, should_send_weekly
from whale_agent.storage.db import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_an_on_time_run_sends(store):
    # 11:00 UTC on 2026-08-03 is 07:00 EDT.
    send, _ = should_send_weekly(store, _utc(2026, 8, 3, 11))
    assert send is True


def test_the_run_that_actually_happened_would_now_send(store):
    """13:43 UTC = 09:43 EDT. The old gate skipped this and sent nothing."""
    send, reason = should_send_weekly(store, _utc(2026, 8, 3, 13, 43))
    assert send is True, reason


def test_a_run_three_and_a_half_hours_late_still_sends(store):
    send, _ = should_send_weekly(store, _utc(2026, 8, 3, 14, 30))
    assert send is True


def test_a_run_before_the_morning_floor_does_not_send(store):
    # 09:00 UTC is 05:00 EDT.
    send, reason = should_send_weekly(store, _utc(2026, 8, 3, 9))
    assert send is False
    assert "floor" in reason


def test_the_floor_is_seven_local():
    assert MIN_LOCAL_HOUR == 7


def test_a_second_run_the_same_day_does_not_send_twice(store):
    """The property that makes redundant crons safe."""
    store.record_digest_run(date(2026, 8, 3), "weekly", "email", True, "sent")
    send, reason = should_send_weekly(store, _utc(2026, 8, 3, 15))
    assert send is False
    assert "already delivered" in reason


def test_a_failed_delivery_does_not_count_as_sent(store):
    """A weekly that errored must be retried by the next run, not suppressed."""
    store.record_digest_run(date(2026, 8, 3), "weekly", "email", False, "SMTP refused")
    send, _ = should_send_weekly(store, _utc(2026, 8, 3, 15))
    assert send is True


def test_last_weeks_delivery_does_not_suppress_this_week(store):
    store.record_digest_run(date(2026, 7, 27), "weekly", "email", True, "sent")
    send, _ = should_send_weekly(store, _utc(2026, 8, 3, 11))
    assert send is True


def test_a_daily_digest_does_not_count_as_the_weekly(store):
    store.record_digest_run(date(2026, 8, 3), "daily", "email", True, "sent")
    send, _ = should_send_weekly(store, _utc(2026, 8, 3, 11))
    assert send is True


def test_dst_is_no_longer_a_special_case(store):
    """In January, 12:00 UTC is 07:00 EST -- still past the floor, so it sends."""
    send, _ = should_send_weekly(store, _utc(2027, 1, 4, 12))
    assert send is True


def test_exactly_one_send_per_week_across_many_late_runs(store):
    """Four redundant crons, all arriving at random lateness, produce one email."""
    sends = 0
    for hour in (11, 13, 15, 17):
        send, _ = should_send_weekly(store, _utc(2026, 8, 3, hour))
        if send:
            sends += 1
            store.record_digest_run(date(2026, 8, 3), "weekly", "email", True, "sent")
    assert sends == 1


def test_a_naive_datetime_is_rejected_rather_than_guessed(store):
    with pytest.raises(ValueError):
        should_send_weekly(store, datetime(2026, 8, 3, 11, 0))


def test_the_next_week_sends_again_after_the_cooldown(store):
    store.record_digest_run(date(2026, 8, 3), "weekly", "email", True, "sent")
    assert should_send_weekly(store, _utc(2026, 8, 10, 11))[0] is True
    assert timedelta(days=7) > timedelta(days=6)
