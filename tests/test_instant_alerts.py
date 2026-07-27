"""The Tier-1 instant-alert gate: boundaries, dedup, and channel behaviour.

An alert that fires too easily gets muted, so the boundary tests here are the point of
the module: each condition is checked at the threshold and just under it.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.jobs.instant_alerts import (
    alert_text,
    is_instant_worthy,
    main,
    send_instant_alerts,
)
from whale_agent.models.enums import TransactionType
from whale_agent.storage.db import Store

SETTINGS = Settings(instant_usd=100_000_000.0)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


# -- size boundary --------------------------------------------------------------
def test_size_at_threshold_fires():
    assert is_instant_worthy(make_event(usd_value=100_000_000.0), SETTINGS) is True


def test_size_just_under_threshold_does_not_fire():
    assert is_instant_worthy(make_event(usd_value=99_999_999.0), SETTINGS) is False


# -- activist 13D ---------------------------------------------------------------
def test_first_time_activist_by_a_notable_filer_fires():
    event = make_event(
        filer_name="Elliott Management",
        transaction_type=TransactionType.ACTIVIST_13D,
        is_first_time_filer=True,
        usd_value=6_000_000.0,
    )
    assert is_instant_worthy(event, SETTINGS) is True


def test_activist_by_an_unknown_filer_does_not_fire():
    event = make_event(
        filer_name="Nobody Capital",
        transaction_type=TransactionType.ACTIVIST_13D,
        is_first_time_filer=True,
        usd_value=6_000_000.0,
    )
    assert is_instant_worthy(event, SETTINGS) is False


def test_repeat_activist_filer_does_not_fire():
    event = make_event(
        filer_name="Elliott Management",
        transaction_type=TransactionType.ACTIVIST_13D,
        is_first_time_filer=False,
        usd_value=6_000_000.0,
    )
    assert is_instant_worthy(event, SETTINGS) is False


# -- cluster --------------------------------------------------------------------
def test_cluster_of_three_buyers_fires():
    assert (
        is_instant_worthy(make_event(cluster_size=3, usd_value=6_000_000.0), SETTINGS) is True
    )


def test_cluster_of_two_does_not_fire():
    assert (
        is_instant_worthy(make_event(cluster_size=2, usd_value=6_000_000.0), SETTINGS) is False
    )


def test_cluster_of_sellers_does_not_fire():
    event = make_event(
        cluster_size=5,
        transaction_type=TransactionType.OPEN_MARKET_SELL,
        usd_value=6_000_000.0,
    )
    assert is_instant_worthy(event, SETTINGS) is False


def test_unvalued_event_does_not_fire():
    assert is_instant_worthy(make_event(usd_value=None), SETTINGS) is False


# -- alert body -----------------------------------------------------------------
def test_alert_text_uses_the_shared_formatter():
    event = make_event(filer_name="Warren Buffett", usd_value=250_000_000.0)
    text = alert_text(event, SETTINGS)
    assert "WHALE:" in text
    assert "$250.0M" in text  # identical to what the digest row would print
    assert "Warren Buffett" in text
    assert "TestCo" in text


def test_alert_text_marks_estimates_and_appends_the_digest_link():
    settings = Settings(instant_usd=1.0, digest_url="https://example.com/digest")
    event = make_event(usd_value=200_000_000.0, usd_value_is_estimate=True)
    text = alert_text(event, settings)
    assert "(est.)" in text
    assert text.endswith("https://example.com/digest")


# -- sending --------------------------------------------------------------------
def test_no_channel_configured_sends_nothing_and_does_not_raise(store):
    events = [make_event(usd_value=250_000_000.0)]
    results = send_instant_alerts(events, store, SETTINGS, on=date(2026, 7, 25))
    assert all(r.ok is False for r in results)


def test_non_qualifying_events_never_attempt_delivery(store):
    events = [make_event(usd_value=6_000_000.0)]
    assert send_instant_alerts(events, store, SETTINGS, on=date(2026, 7, 25)) == []


def test_alerts_are_deduplicated_across_runs(store, monkeypatch):
    sent: list[str] = []

    def fake_telegram(text, settings=None):
        from whale_agent.delivery.base import DeliveryResult

        sent.append(text)
        return DeliveryResult("telegram", True)

    monkeypatch.setattr("whale_agent.jobs.instant_alerts.send_telegram", fake_telegram)
    events = [make_event(usd_value=250_000_000.0)]

    send_instant_alerts(events, store, SETTINGS, on=date(2026, 7, 25))
    send_instant_alerts(events, store, SETTINGS, on=date(2026, 7, 25))
    # A re-run (backfill, retry, overlapping cron) must not page twice.
    assert len(sent) == 1


# -- CLI entrypoint ---------------------------------------------------------------
# `main()` is what makes this job reachable at all (previously nothing called
# `send_instant_alerts`, so `delivery/telegram.py` and `delivery/sms_twilio.py` were
# dead code end to end). These tests only need to prove the command runs and that
# `--dry-run` never touches a delivery channel -- the gating and dedup logic above is
# already covered against the pure functions.
def test_main_demo_dry_run_is_invocable_without_network_or_credentials(monkeypatch, capsys):
    """`--demo --dry-run` must work with a clean environment: no keys, no network."""
    monkeypatch.setattr("sys.argv", ["whale-instant-alerts", "--demo", "--dry-run"])

    def fail(*args, **kwargs):
        raise AssertionError("dry-run must not attempt delivery")

    monkeypatch.setattr("whale_agent.jobs.instant_alerts.send_telegram", fail)
    monkeypatch.setattr("whale_agent.jobs.instant_alerts.send_sms", fail)

    main()

    out = capsys.readouterr().out
    assert "instant-worthy event(s)" in out


def test_main_dry_run_never_calls_a_delivery_channel(monkeypatch, capsys, tmp_path):
    """Same guarantee without `--demo`, against a scratch on-disk store."""
    monkeypatch.setenv("WHALE_DB_PATH", str(tmp_path / "scratch.db"))
    monkeypatch.setattr(
        "sys.argv", ["whale-instant-alerts", "--dry-run", "--date", "2026-07-25"]
    )

    def fail(*args, **kwargs):
        raise AssertionError("dry-run must not attempt delivery")

    monkeypatch.setattr("whale_agent.jobs.instant_alerts.send_telegram", fail)
    monkeypatch.setattr("whale_agent.jobs.instant_alerts.send_sms", fail)

    main()

    out = capsys.readouterr().out
    assert "instant-worthy event(s) as of 2026-07-25" in out
