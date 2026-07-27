"""Tests for the market-context enrichment layer.

Golden-file throughout: every parser is exercised against a saved real FMP response, so
a vendor renaming `floatShares` or re-casing `ytd` fails here rather than quietly
emptying the context on every event. Nothing in this file touches the network.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from whale_agent.config import Settings
from whale_agent.enrichment.market_context import (
    FmpMarketContextProvider,
    MarketSnapshot,
    StaticMarketContextProvider,
    context_from_snapshot,
    enrich_events,
    parse_fmp_price_change,
    parse_fmp_profile,
    parse_fmp_shares_float,
    percent_of,
)
from whale_agent.models.enums import Jurisdiction, TransactionType
from whale_agent.models.event import NormalizedEvent

UNCONFIGURED = Settings()  # no keys at all


def _event(ticker: str | None = "AGO", **kwargs) -> NormalizedEvent:
    return NormalizedEvent(
        jurisdiction=Jurisdiction.US,
        source="test",
        issuer_name="Assured Guaranty Ltd.",
        ticker=ticker,
        filer_name="Example Capital",
        transaction_type=TransactionType.OPEN_MARKET_BUY,
        transaction_date=date(2026, 7, 24),
        disclosure_date=date(2026, 7, 25),
        **kwargs,
    )


# -- parsers ------------------------------------------------------------------
def test_profile_parse_reads_scale_and_classification(fixtures_dir):
    """The profile response yields market cap, sector, industry and exchange."""
    raw = json.loads((fixtures_dir / "fmp_profile_response.json").read_text())
    fields = parse_fmp_profile(raw)
    assert fields["market_cap_usd"] == 3779954200
    assert fields["sector"] == "Financial Services"
    assert fields["industry"] == "Insurance - Specialty"
    assert fields["exchange"] == "NYSE"


def test_shares_float_parse_prefers_float_over_shares_outstanding(fixtures_dir):
    """Float, not outstanding, is what percent-of-company is measured against."""
    raw = json.loads((fixtures_dir / "fmp_shares_float_response.json").read_text())
    assert parse_fmp_shares_float(raw) == 41538224
    # The outstanding count sits in the same row and must not be the one we take.
    assert raw[0]["outstandingShares"] != 41538224


def test_price_change_parse_handles_the_vendors_mixed_key_casing(fixtures_dir):
    """`1M` and `3M` are upper case, `ytd` is lower; all three must come through."""
    raw = json.loads((fixtures_dir / "fmp_stock_price_change_response.json").read_text())
    fields = parse_fmp_price_change(raw)
    assert fields["price_change_1m_pct"] == pytest.approx(7.00765)
    assert fields["price_change_3m_pct"] == pytest.approx(2.35012)
    assert fields["price_change_ytd_pct"] == pytest.approx(-5.01836)


def test_parsers_return_empty_rather_than_raising_on_junk_payloads():
    """A truncated or error payload leaves fields unlearned instead of blowing up."""
    for payload in (None, [], {}, "error", [{"symbol": "AGO"}]):
        assert parse_fmp_profile(payload).get("market_cap_usd") is None
        assert parse_fmp_shares_float(payload) is None
        assert parse_fmp_price_change(payload).get("price_change_1m_pct") is None


# -- percentages --------------------------------------------------------------
@pytest.mark.parametrize(
    "numerator, denominator, expected",
    [
        (50_000, 1_000_000, 5.0),
        (None, 1_000_000, None),
        (50_000, None, None),
        (50_000, 0, None),
        (50_000, -1, None),
    ],
)
def test_percent_of_returns_none_whenever_an_input_is_missing_or_unusable(
    numerator, denominator, expected
):
    """Missing inputs never become zero, and a zero denominator never becomes infinity."""
    assert percent_of(numerator, denominator) == expected


def test_context_computes_both_percentages_from_the_events_own_figures():
    """Percent of float uses shares; percent of market cap uses the USD value."""
    snapshot = MarketSnapshot(
        ticker="AGO", market_cap_usd=4_000_000_000.0, shares_float=40_000_000.0
    )
    context = context_from_snapshot(snapshot, share_count=400_000, usd_value=40_000_000)
    assert context.percent_of_float == pytest.approx(1.0)
    assert context.percent_of_market_cap == pytest.approx(1.0)


def test_context_leaves_percentages_none_when_the_event_has_no_share_count():
    """An unvalued event gets scale context but no invented ratios."""
    snapshot = MarketSnapshot(ticker="AGO", market_cap_usd=4e9, shares_float=4e7)
    context = context_from_snapshot(snapshot, share_count=None, usd_value=None)
    assert context.market_cap_usd == 4e9
    assert context.percent_of_float is None
    assert context.percent_of_market_cap is None


def test_context_from_a_missing_snapshot_changes_nothing():
    """An uncovered ticker leaves the context exactly as it was found."""
    event = _event(share_count=1000)
    event.context.filer_prior_filings = 3
    context_from_snapshot(None, share_count=1000, into=event.context)
    assert event.context.market_cap_usd is None
    assert event.context.filer_prior_filings == 3


# -- providers ----------------------------------------------------------------
def test_static_provider_is_case_insensitive_and_returns_none_for_unknowns():
    """The test double answers on any casing, and admits when it does not know."""
    provider = StaticMarketContextProvider({"ago": MarketSnapshot("AGO", market_cap_usd=1.0)})
    assert provider.snapshot("AGO").market_cap_usd == 1.0
    assert provider.snapshot("ZZZZ") is None
    assert provider.snapshot("") is None


def test_enriching_a_batch_costs_one_lookup_per_distinct_ticker():
    """Twenty events across two issuers must not become twenty vendor calls."""
    calls: list[str] = []

    class CountingProvider(StaticMarketContextProvider):
        def snapshot(self, ticker: str):
            calls.append(ticker)
            return super().snapshot(ticker)

    provider = CountingProvider({"AGO": MarketSnapshot("AGO", shares_float=1_000_000.0)})
    fmp_provider = FmpMarketContextProvider(UNCONFIGURED)
    fmp_provider._cache["AGO"] = provider.snapshot("AGO")
    calls.clear()

    events = [_event("AGO", share_count=10_000) for _ in range(20)]
    enrich_events(events, fmp_provider)

    assert calls == []  # the cached snapshot served all twenty
    assert all(e.context.percent_of_float == pytest.approx(1.0) for e in events)


def test_events_without_a_ticker_are_left_alone():
    """No fuzzy symbol matching: an unmatched filing gets no other company's numbers."""
    provider = StaticMarketContextProvider({"AGO": MarketSnapshot("AGO", market_cap_usd=1.0)})
    event = _event(ticker=None, share_count=10_000)
    enrich_events([event], provider)
    assert event.context.market_cap_usd is None


def test_fmp_provider_degrades_to_none_when_no_key_is_configured():
    """A missing credential is reported as "unknown", never as an exception."""
    provider = FmpMarketContextProvider(UNCONFIGURED)
    assert provider.snapshot("AGO") is None
    # The negative result is cached too, so a dead ticker is not retried per event.
    assert provider._cache == {"AGO": None}


def test_fmp_provider_builds_a_snapshot_from_the_three_saved_responses(
    fixtures_dir, monkeypatch
):
    """The three endpoints combine into one snapshot, and one snapshot only."""
    payloads = {
        "/stable/profile": json.loads(
            (fixtures_dir / "fmp_profile_response.json").read_text()
        ),
        "/stable/shares-float": json.loads(
            (fixtures_dir / "fmp_shares_float_response.json").read_text()
        ),
        "/stable/stock-price-change": json.loads(
            (fixtures_dir / "fmp_stock_price_change_response.json").read_text()
        ),
    }
    requested: list[str] = []

    provider = FmpMarketContextProvider(Settings(fmp_api_key="test-key"))
    monkeypatch.setattr(
        provider,
        "_get",
        lambda path, params: (requested.append(path), payloads[path])[1],
    )

    snapshot = provider.snapshot("ago")
    assert snapshot.ticker == "AGO"
    assert snapshot.market_cap_usd == 3779954200
    assert snapshot.shares_float == 41538224
    assert snapshot.sector == "Financial Services"
    assert snapshot.price_change_1m_pct == pytest.approx(7.00765)

    provider.snapshot("AGO")
    assert len(requested) == 3  # second call served from cache


def test_fmp_provider_keeps_the_endpoints_that_worked_when_one_fails(monkeypatch):
    """A float lookup that dies still leaves market cap and price action attached."""
    from whale_agent.errors import SourceUnavailableError

    provider = FmpMarketContextProvider(Settings(fmp_api_key="test-key"))

    def flaky(path: str, params: dict):
        if path == "/stable/shares-float":
            raise SourceUnavailableError("boom")
        if path == "/stable/profile":
            return [{"marketCap": 1_000_000.0, "sector": "Technology"}]
        return [{"1M": 3.5}]

    monkeypatch.setattr(provider, "_get", flaky)
    snapshot = provider.snapshot("AGO")
    assert snapshot.market_cap_usd == 1_000_000.0
    assert snapshot.shares_float is None
    assert snapshot.price_change_1m_pct == 3.5


def test_fmp_provider_returns_none_when_every_endpoint_is_empty(monkeypatch):
    """A ticker FMP does not cover yields no snapshot at all, not an empty one."""
    provider = FmpMarketContextProvider(Settings(fmp_api_key="test-key"))
    monkeypatch.setattr(provider, "_get", lambda path, params: [])
    assert provider.snapshot("NOSUCH") is None


# -- company names ----------------------------------------------------------------
# FMP's insider feed returns companyName as null, so an event arrives with the ticker
# standing in for the issuer, and articles get headlined "filers bought FSBC".


def test_enrichment_replaces_a_ticker_placeholder_with_the_company_name():
    from tests.conftest import make_event
    from whale_agent.enrichment.market_context import (
        MarketSnapshot,
        StaticMarketContextProvider,
        enrich_event,
    )

    provider = StaticMarketContextProvider(
        {
            "FSBC": MarketSnapshot(
                ticker="FSBC", company_name="Five Star Bancorp", market_cap_usd=1_000_617_722.0
            )
        }
    )
    event = make_event(issuer_name="FSBC", ticker="FSBC")
    enrich_event(event, provider)
    assert event.issuer_name == "Five Star Bancorp"


def test_enrichment_never_overwrites_a_real_issuer_name():
    """A name the filing supplied is authoritative; the vendor's is only a fallback."""
    from tests.conftest import make_event
    from whale_agent.enrichment.market_context import (
        MarketSnapshot,
        StaticMarketContextProvider,
        enrich_event,
    )

    provider = StaticMarketContextProvider(
        {"FSBC": MarketSnapshot(ticker="FSBC", company_name="Five Star Bancorp")}
    )
    event = make_event(issuer_name="Five Star Bancorp Inc /DE/", ticker="FSBC")
    enrich_event(event, provider)
    assert event.issuer_name == "Five Star Bancorp Inc /DE/"


# EDINET supplies issuer names in Japanese, which is faithful to the filing and unreadable
# to a reader who does not read Japanese. FMP already prices these tickers, and its profile
# carries the English name, so the display name is swapped and the filing's own name is kept
# in raw_payload rather than discarded.


def test_a_japanese_issuer_name_is_replaced_with_the_english_one():
    from tests.conftest import make_event
    from whale_agent.enrichment.market_context import (
        MarketSnapshot,
        StaticMarketContextProvider,
        enrich_event,
    )

    provider = StaticMarketContextProvider(
        {"7936.T": MarketSnapshot(ticker="7936.T", company_name="ASICS Corporation")}
    )
    event = make_event(issuer_name="株式会社アシックス", ticker="7936.T")
    enrich_event(event, provider)
    assert event.issuer_name == "ASICS Corporation"
    assert event.raw_payload["issuer_name_local"] == "株式会社アシックス"


def test_a_latin_issuer_name_is_still_never_overwritten():
    """The CJK rule must not become a general licence to prefer the vendor's name."""
    from tests.conftest import make_event
    from whale_agent.enrichment.market_context import (
        MarketSnapshot,
        StaticMarketContextProvider,
        enrich_event,
    )

    provider = StaticMarketContextProvider(
        {"FSBC": MarketSnapshot(ticker="FSBC", company_name="Five Star Bancorp")}
    )
    event = make_event(issuer_name="Five Star Bancorp Inc /DE/", ticker="FSBC")
    enrich_event(event, provider)
    assert event.issuer_name == "Five Star Bancorp Inc /DE/"
    assert "issuer_name_local" not in event.raw_payload


def test_a_japanese_name_survives_when_the_vendor_name_is_also_japanese():
    """Swapping one Japanese name for another gains nothing and loses the filing's own."""
    from tests.conftest import make_event
    from whale_agent.enrichment.market_context import (
        MarketSnapshot,
        StaticMarketContextProvider,
        enrich_event,
    )

    provider = StaticMarketContextProvider(
        {"7936.T": MarketSnapshot(ticker="7936.T", company_name="アシックス")}
    )
    event = make_event(issuer_name="株式会社アシックス", ticker="7936.T")
    enrich_event(event, provider)
    assert event.issuer_name == "株式会社アシックス"


def test_a_holding_larger_than_the_free_float_is_not_reported_as_a_percentage_of_it():
    """A 53.89% holder was rendered as "171.2% of float", which reads as a bug.

    It is arithmetically right: free float excludes the holder's own block, so dividing
    by it can exceed 100%. It is still the wrong number to show a reader, because the
    sentence "171% of float" cannot be true of anything. Above 100% the denominator is
    not describing the same universe as the numerator, so the figure is dropped rather
    than printed or clamped. Clamping to 100% would be inventing a figure.
    """
    from whale_agent.enrichment.market_context import MarketSnapshot, context_from_snapshot

    snapshot = MarketSnapshot(
        ticker="TEST", shares_float=1_000_000.0, market_cap_usd=50_000_000.0
    )
    ctx = context_from_snapshot(snapshot, share_count=1_712_000.0, usd_value=1_000_000.0)
    assert ctx.percent_of_float is None

    ordinary = context_from_snapshot(snapshot, share_count=120_000.0, usd_value=1_000_000.0)
    assert ordinary.percent_of_float == pytest.approx(12.0)
