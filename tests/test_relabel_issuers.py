"""The maintenance pass that backfills English issuer names onto already-stored rows.

The rule in `enrich_event` runs at ingest, so it cannot reach rows persisted before it
existed. The weekly report reads storage rather than re-enriching, which is why a fix at
ingest alone left 360 Japanese names in front of the reader.
"""

from __future__ import annotations

from datetime import date, timedelta

from tests.conftest import make_event
from whale_agent.enrichment.market_context import (
    MarketSnapshot,
    StaticMarketContextProvider,
)
from whale_agent.jobs.relabel_issuers import relabel
from whale_agent.storage.db import Store

SINCE = date.today() - timedelta(days=365)


def _store(tmp_path, events):
    store = Store(str(tmp_path / "whale.db"))
    for event in events:
        store.upsert(event)
    return store


def _provider():
    return StaticMarketContextProvider(
        {
            "7936.T": MarketSnapshot(ticker="7936.T", company_name="ASICS Corporation"),
            "FSBC": MarketSnapshot(ticker="FSBC", company_name="Five Star Bancorp"),
        }
    )


def test_a_stored_japanese_name_is_rewritten_in_place(tmp_path):
    event = make_event(issuer_name="株式会社アシックス", ticker="7936.T")
    store = _store(tmp_path, [event])
    try:
        changed = relabel(store, _provider(), since=SINCE)
        assert changed == [("株式会社アシックス", "ASICS Corporation")]
        stored = store.get(event.event_id)
        assert stored.issuer_name == "ASICS Corporation"
        assert stored.raw_payload["issuer_name_local"] == "株式会社アシックス"
    finally:
        store.close()


def test_dry_run_reports_without_writing(tmp_path):
    event = make_event(issuer_name="株式会社アシックス", ticker="7936.T")
    store = _store(tmp_path, [event])
    try:
        changed = relabel(store, _provider(), since=SINCE, dry_run=True)
        assert len(changed) == 1
        assert store.get(event.event_id).issuer_name == "株式会社アシックス"
    finally:
        store.close()


def test_running_twice_changes_nothing_the_second_time(tmp_path):
    """Idempotence is what makes this safe to re-run after a later ingest."""
    event = make_event(issuer_name="株式会社アシックス", ticker="7936.T")
    store = _store(tmp_path, [event])
    try:
        assert len(relabel(store, _provider(), since=SINCE)) == 1
        assert relabel(store, _provider(), since=SINCE) == []
    finally:
        store.close()


def test_latin_names_and_tickerless_rows_are_left_alone(tmp_path):
    latin = make_event(issuer_name="Five Star Bancorp Inc /DE/", ticker="FSBC")
    tickerless = make_event(issuer_name="株式会社マイネット", ticker=None)
    store = _store(tmp_path, [latin, tickerless])
    try:
        assert relabel(store, _provider(), since=SINCE) == []
        assert store.get(tickerless.event_id).issuer_name == "株式会社マイネット"
    finally:
        store.close()
