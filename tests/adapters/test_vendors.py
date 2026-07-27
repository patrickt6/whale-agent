"""Golden-file tests for the vendor adapters (FMP, Quiver).

No network anywhere: every test parses a saved fixture, exactly the way the real
adapters parse a live response. A vendor changing its field names fails here before it
silently empties the digest.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from whale_agent.config import Settings
from whale_agent.errors import NotConfiguredError
from whale_agent.ingestion.vendors.fmp import (
    FmpCongressAdapter,
    FmpInsiderAdapter,
    FmpInstitutionalAdapter,
    FmpInstitutionalHolderAdapter,
    range_lower_bound,
)
from whale_agent.ingestion.vendors.quiver import (
    QuiverCongressAdapter,
    QuiverGovContractsAdapter,
    QuiverLobbyingAdapter,
)
from whale_agent.models.enums import PriceSource, TransactionType

UNCONFIGURED = Settings()  # no keys at all


# -- FMP ----------------------------------------------------------------------
def test_fmp_insider_parse_and_normalize(fixtures_dir):
    raw = json.loads((fixtures_dir / "fmp_insider_response.json").read_text())
    adapter = FmpInsiderAdapter(UNCONFIGURED)
    rows = adapter.parse(raw)
    assert len(rows) == 2

    buy = adapter.normalize(rows[0])
    assert buy.transaction_type == TransactionType.OPEN_MARKET_BUY
    assert buy.ticker == "AGO"
    assert buy.share_count == 65000
    assert buy.price_used == 83.05
    # FMP echoes the Form 4's own price, so it counts as filing-stated.
    assert buy.price_source == PriceSource.FILING_STATED
    assert buy.transaction_date == date(2026, 7, 24)
    assert buy.disclosure_date == date(2026, 7, 25)

    sell = adapter.normalize(rows[1])
    assert sell.transaction_type == TransactionType.OPEN_MARKET_SELL


def test_fmp_congress_uses_the_range_lower_bound(fixtures_dir):
    raw = json.loads((fixtures_dir / "fmp_congress_response.json").read_text())
    adapter = FmpCongressAdapter(UNCONFIGURED)
    event = adapter.normalize(adapter.parse(raw)[0])
    assert event.filer_name == "Jane Doe"
    assert event.ticker == "NVDA"
    # "$1,000,001 - $5,000,000" -> the disclosed floor, never the midpoint.
    assert event.native_amount == 1_000_001.0
    assert event.usd_value_is_estimate is True
    assert event.transaction_type == TransactionType.OPEN_MARKET_BUY


@pytest.mark.parametrize(
    "text, expected",
    [
        ("$1,001 - $15,000", 1001.0),
        ("$50,000,000+", 50_000_000.0),
        ("15000", 15000.0),
        ("", None),
        (None, None),
        ("unknown", None),
    ],
)
def test_range_lower_bound(text, expected):
    assert range_lower_bound(text) == expected


def test_fmp_institutional_reports_a_snapshot_row_as_a_holding(fixtures_dir):
    """A 13F extract row is a position, not a trade, and is typed as such.

    The live endpoint carries no delta field of any kind, so there is nothing in a row
    that could justify calling it a purchase. Valuing the whole holding and typing it
    OTHER is the honest reading; anything else would invent a transaction.
    """
    rows = json.loads((fixtures_dir / "fmp_institutional_extract_response.json").read_text())
    adapter = FmpInstitutionalAdapter(UNCONFIGURED)
    event = adapter.normalize(rows[0])
    assert event.transaction_type == TransactionType.OTHER
    assert event.ticker == "STZ"
    assert event.issuer_name == "CONSTELLATION BRANDS INC"
    assert event.issuer_id == "21036P108"
    assert event.share_count == 632890
    assert event.native_amount == 94933500
    assert event.filer_id == "0001067983"
    assert event.transaction_date == date(2026, 3, 31)
    assert event.disclosure_date == date(2026, 5, 15)
    assert event.source_url.endswith("53405.xml")


def test_fmp_without_a_key_raises_not_configured():
    adapter = FmpInsiderAdapter(UNCONFIGURED)
    with pytest.raises(NotConfiguredError, match="fmp_api_key"):
        adapter._get("/stable/insider-trading/search", {})


# -- Quiver -------------------------------------------------------------------
def test_quiver_congress_parse_and_normalize(fixtures_dir):
    raw = json.loads((fixtures_dir / "quiver_congress_response.json").read_text())
    adapter = QuiverCongressAdapter(UNCONFIGURED)
    event = adapter.normalize(adapter.parse(raw)[0])
    assert event.filer_name == "Nancy Example"
    assert event.ticker == "MSFT"
    assert event.native_amount == 5_000_001.0
    assert event.transaction_type == TransactionType.OPEN_MARKET_BUY
    assert event.transaction_date == date(2026, 7, 1)
    assert event.disclosure_date == date(2026, 7, 20)


def test_quiver_lobbying_produces_annotations_not_events(fixtures_dir):
    raw = json.loads((fixtures_dir / "quiver_lobbying_response.json").read_text())
    adapter = QuiverLobbyingAdapter(UNCONFIGURED)
    rows = adapter.parse(raw)

    # Lobbying is context; forcing it through the event pipeline is a category error
    # and the adapter says so rather than emitting something misleading.
    with pytest.raises(NotImplementedError):
        adapter.normalize(rows[0])

    annotations = adapter.annotations(rows)
    assert len(annotations) == 2
    assert annotations[0].kind == "lobbying"
    assert annotations[0].amount_usd == 3_120_000.0
    assert annotations[0].as_of == date(2026, 6, 30)


def test_quiver_gov_contracts_anchor_quarterly_rows():
    adapter = QuiverGovContractsAdapter(UNCONFIGURED)
    annotations = adapter.annotations(
        [{"Ticker": "LMT", "Year": 2026, "Qtr": 3, "Amount": "1200000", "Agency": "DoD"}]
    )
    assert annotations[0].as_of == date(2026, 7, 1)
    assert "DoD" in annotations[0].label


def test_quiver_without_a_key_raises_not_configured():
    with pytest.raises(NotConfiguredError, match="quiver_api_key"):
        QuiverCongressAdapter(UNCONFIGURED)._get("/beta/live/congresstrading")


def test_fmp_institutional_extract_parses_the_real_snapshot_schema(fixtures_dir):
    """The live extract endpoint uses nameOfIssuer/shares/value and carries no delta."""
    raw = json.loads((fixtures_dir / "fmp_institutional_extract_response.json").read_text())
    adapter = FmpInstitutionalAdapter(UNCONFIGURED)
    event = adapter.normalize(adapter.parse(raw)[0])
    assert event.issuer_name == "CONSTELLATION BRANDS INC"
    assert event.ticker == "STZ"
    assert event.issuer_id == "21036P108"
    assert event.share_count == 632890
    assert event.native_amount == 94933500
    assert event.transaction_date == date(2026, 3, 31)
    assert event.disclosure_date == date(2026, 5, 15)
    # A snapshot cannot tell a new position from an old one, and does not pretend to.
    assert event.transaction_type == TransactionType.OTHER


def test_fmp_holder_analytics_reports_a_brand_new_position(fixtures_dir):
    """isNew/isSoldOut and the quarter's deltas land on the event context."""
    raw = json.loads((fixtures_dir / "fmp_institutional_holder_response.json").read_text())
    adapter = FmpInstitutionalHolderAdapter(UNCONFIGURED)
    rows = adapter.parse(raw)

    trimmed = adapter.normalize(rows[0])
    assert trimmed.filer_name == "BLACKROCK, INC."
    assert trimmed.context.is_new_position is False
    assert trimmed.context.is_sold_out is False
    assert trimmed.context.avg_price_paid == 233.36
    assert trimmed.context.change_in_shares_pct == pytest.approx(-0.8635)
    # A reduced holding is not a purchase.
    assert trimmed.transaction_type == TransactionType.OTHER

    opened = adapter.normalize(rows[1])
    assert opened.context.is_new_position is True
    assert opened.transaction_type == TransactionType.FUND_NEW_POSITION
    assert opened.share_count == 953847648


def test_fmp_holder_analytics_requires_a_symbol():
    """It is keyed by ticker, not by manager CIK; asking wrongly fails loudly."""
    adapter = FmpInstitutionalHolderAdapter(UNCONFIGURED)
    with pytest.raises(NotConfiguredError, match="symbol"):
        list(adapter.fetch(year=2026, quarter=1))
