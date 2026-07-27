"""Valuation + $5M gate tests."""

from __future__ import annotations

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.enrichment.valuation import value_event
from whale_agent.models.enums import PriceSource
from whale_agent.scoring.score import is_near_threshold, passes_gate

SETTINGS = Settings()


def test_shares_times_stated_price_is_exact():
    ev = make_event(
        usd_value=None,
        share_count=500_000,
        price_used=15.0,
        price_source=PriceSource.FILING_STATED,
    )
    value_event(ev)
    assert ev.usd_value == 7_500_000
    assert ev.usd_value_is_estimate is False


def test_shares_priced_by_us_is_flagged_estimate():
    ev = make_event(
        usd_value=None,
        share_count=500_000,
        price_used=15.0,
        price_source=PriceSource.CLOSE_ON_DATE,
    )
    value_event(ev)
    assert ev.usd_value == 7_500_000
    assert ev.usd_value_is_estimate is True


def test_foreign_currency_amount_converted_to_usd():
    # 300,000,000 TWD at 0.031 -> 9.3M USD
    ev = make_event(usd_value=None, native_currency="TWD", native_amount=300_000_000)
    value_event(ev)
    assert round(ev.usd_value) == 9_300_000
    assert ev.fx_rate_used == 0.031


def test_percentage_threshold_implies_market_cap_slice():
    # 6.2% of (145M shares * $15.75) = 6.2% of ~2.284B = ~141.6M
    ev = make_event(
        usd_value=None,
        percent_of_company=6.2,
        shares_outstanding=145_000_000,
        price_used=15.75,
        percentage_threshold_crossed=True,
    )
    value_event(ev)
    assert 141_000_000 < ev.implied_usd_value < 142_000_000


def test_gate_passes_above_5m_and_fails_below():
    assert passes_gate(make_event(usd_value=5_000_000), SETTINGS) is True
    assert passes_gate(make_event(usd_value=4_000_000), SETTINGS) is False


def test_gate_uses_implied_value_for_percentage_filings():
    ev = make_event(
        usd_value=None,
        implied_usd_value=100_000_000,
        percentage_threshold_crossed=True,
    )
    assert passes_gate(ev, SETTINGS) is True


def test_near_threshold_band_is_flagged_not_dropped():
    ev = make_event(usd_value=4_700_000)
    assert passes_gate(ev, SETTINGS) is False
    assert is_near_threshold(ev, SETTINGS) is True


def test_micro_cap_five_percent_does_not_clear_bar():
    # 5% of a $50M micro-cap = $2.5M, below the gate.
    ev = make_event(
        usd_value=None,
        percent_of_company=5.0,
        shares_outstanding=10_000_000,
        price_used=5.0,
        percentage_threshold_crossed=True,
    )
    value_event(ev)
    assert ev.implied_usd_value == 2_500_000
    assert passes_gate(ev, SETTINGS) is False
