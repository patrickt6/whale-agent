"""Price provider: FMP response parsing, the close-preference order, and valuation wiring."""

from __future__ import annotations

import json
from datetime import date

from tests.conftest import make_event
from whale_agent.enrichment.price import (
    PriceQuote,
    StaticPriceProvider,
    parse_fmp_bars,
    pick_close,
)
from whale_agent.enrichment.valuation import value_event
from whale_agent.models.enums import PriceSource


def test_parse_fmp_bars_from_fixture(fixtures_dir):
    payload = json.loads((fixtures_dir / "fmp_price_response.json").read_text())
    bars = parse_fmp_bars(payload)
    assert bars[date(2026, 7, 24)] == 83.05
    assert len(bars) == 3


def test_parse_fmp_bars_accepts_flat_list():
    bars = parse_fmp_bars([{"date": "2026-07-24", "close": 10.0}])
    assert bars == {date(2026, 7, 24): 10.0}


def test_parse_fmp_bars_skips_rows_missing_a_close():
    # A defaulted zero would become a $0 position rather than an absent one.
    bars = parse_fmp_bars([{"date": "2026-07-24"}, {"close": 5.0}])
    assert bars == {}


def test_pick_close_prefers_the_transaction_date(fixtures_dir):
    bars = parse_fmp_bars(json.loads((fixtures_dir / "fmp_price_response.json").read_text()))
    quote = pick_close(bars, date(2026, 7, 24))
    assert quote == PriceQuote(83.05, PriceSource.CLOSE_ON_DATE, date(2026, 7, 24))


def test_pick_close_falls_back_to_most_recent_prior_close(fixtures_dir):
    bars = parse_fmp_bars(json.loads((fixtures_dir / "fmp_price_response.json").read_text()))
    # 2026-07-25 is a Saturday in this fixture's world: no bar.
    quote = pick_close(bars, date(2026, 7, 25))
    assert quote is not None
    assert quote.source == PriceSource.MOST_RECENT_CLOSE
    assert quote.as_of == date(2026, 7, 24)


def test_pick_close_never_looks_forward(fixtures_dir):
    bars = parse_fmp_bars(json.loads((fixtures_dir / "fmp_price_response.json").read_text()))
    # A date before every bar must return nothing rather than a future price.
    assert pick_close(bars, date(2026, 7, 1)) is None


def test_pick_close_on_empty_bars():
    assert pick_close({}, date(2026, 7, 24)) is None


def test_valuation_uses_price_provider_for_share_count_filings():
    provider = StaticPriceProvider(bars={"AGO": {date(2026, 7, 24): 83.05}})
    ev = make_event(
        ticker="AGO",
        usd_value=None,
        share_count=100_000.0,
        price_used=None,
        price_source=PriceSource.NOT_PRICED,
        transaction_date=date(2026, 7, 24),
    )
    value_event(ev, price=provider)
    assert ev.price_used == 83.05
    assert ev.price_source == PriceSource.CLOSE_ON_DATE
    assert ev.usd_value == 100_000.0 * 83.05
    # Anything not stated in the filing itself is an estimate.
    assert ev.usd_value_is_estimate is True


def test_valuation_never_overrides_a_filing_stated_price():
    provider = StaticPriceProvider(bars={"AGO": {date(2026, 7, 24): 999.0}})
    ev = make_event(
        ticker="AGO",
        usd_value=None,
        share_count=1_000.0,
        price_used=50.0,
        price_source=PriceSource.FILING_STATED,
        transaction_date=date(2026, 7, 24),
    )
    value_event(ev, price=provider)
    assert ev.price_used == 50.0
    assert ev.usd_value_is_estimate is False


def test_valuation_of_percent_filing_uses_provider_shares_outstanding():
    provider = StaticPriceProvider(
        bars={"2330.TW": {date(2026, 7, 24): 30.0}},
        shares={"2330.TW": 1_000_000.0},
    )
    ev = make_event(
        ticker="2330.TW",
        usd_value=None,
        share_count=None,
        percent_of_company=6.0,
        shares_outstanding=None,
        price_used=None,
        price_source=PriceSource.NOT_PRICED,
        transaction_date=date(2026, 7, 24),
        native_currency="USD",
    )
    value_event(ev, price=provider)
    assert ev.shares_outstanding == 1_000_000.0
    assert ev.implied_usd_value == 0.06 * 1_000_000.0 * 30.0


def test_valuation_without_a_provider_leaves_the_event_unvalued():
    ev = make_event(
        ticker="AGO",
        usd_value=None,
        share_count=100_000.0,
        price_used=None,
        price_source=PriceSource.NOT_PRICED,
    )
    value_event(ev, price=None)
    assert ev.usd_value is None
    assert ev.effective_usd is None
