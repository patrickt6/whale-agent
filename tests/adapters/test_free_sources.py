"""Golden-file tests for the free sources: Taiwan MOPS, Japan EDINET, crypto, Dataroma.

These are the sources that run with no paid subscription, so they carry the digest on
their own. All parsing is fixture-driven; nothing here touches the network.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from whale_agent.config import Settings
from whale_agent.enrichment.price import PriceQuote
from whale_agent.ingestion.crypto_arkham import ArkhamAdapter
from whale_agent.ingestion.crypto_whale_alert import WhaleAlertAdapter
from whale_agent.ingestion.dataroma import DataromaInsiderBuysAdapter
from whale_agent.ingestion.japan_edinet import (
    JapanEdinetAdapter,
    decode_edinet_document,
    parse_holdings_csv,
)
from whale_agent.ingestion.taiwan_mops import TaiwanMopsAdapter, parse_roc_date
from whale_agent.models.enums import Jurisdiction, PriceSource, TransactionType

SETTINGS = Settings()


# -- Taiwan --------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1150725", date(2026, 7, 25)),  # ROC year 115 -> 2026
        ("2026-07-25", date(2026, 7, 25)),
        ("2026/07/25", date(2026, 7, 25)),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_roc_date(raw, expected):
    assert parse_roc_date(raw) == expected


def test_taiwan_parse_and_normalize(fixtures_dir):
    raw = json.loads((fixtures_dir / "taiwan_mops_response.json").read_text())
    adapter = TaiwanMopsAdapter(SETTINGS)
    rows = adapter.parse(raw)
    assert len(rows) == 2

    chairman = adapter.normalize(rows[0])
    assert chairman.jurisdiction == Jurisdiction.TAIWAN
    assert chairman.ticker == "2330.TW"
    assert chairman.native_currency == "TWD"
    assert chairman.percent_of_company == 6.10
    assert chairman.percentage_threshold_crossed is True
    assert chairman.share_count == 1_450_000
    assert "劉德音" in chairman.filer_name

    # No dollar figure is stated anywhere in this feed, so without a price provider the
    # event is ingested but cannot be valued. That is the intended behaviour.
    assert chairman.usd_value is None


def test_taiwan_unchanged_holdings_produce_a_stable_key(fixtures_dir):
    """The same monthly balance must dedup against itself across runs."""
    raw = json.loads((fixtures_dir / "taiwan_mops_response.json").read_text())
    adapter = TaiwanMopsAdapter(SETTINGS)
    rows = adapter.parse(raw)
    first = adapter.normalize(rows[0])
    again = adapter.normalize(rows[0])
    assert first.event_id == again.event_id

    changed = dict(rows[0])
    changed["目前持有股數"] = "1500000"
    assert adapter.normalize(changed).event_id != first.event_id


# -- Japan ---------------------------------------------------------------------
# Every Japan fixture below is a byte-for-byte capture of a real
# api.edinet-fsa.go.jp/api/v2 response taken on 2026-07-24, not a hand-written sample.
# The previous fixtures were invented, and each of the three things they got wrong
# (metadata that identifies the issuer, ratios expressed as percentages, one holder per
# filing) was a live bug that kept this source at zero usable rows.


def test_edinet_document_body_is_a_zip_not_text(fixtures_dir):
    """`documents/{docID}?type=5` serves a ZIP; reading it as text loses every number."""
    body = (fixtures_dir / "edinet_document_S100YS2X.zip").read_bytes()
    assert body[:2] == b"PK"
    text = decode_edinet_document(body)
    assert "\u682a\u5238\u7b49\u4fdd\u6709\u5272\u5408" in text
    assert parse_holdings_csv(body)["shares"] == 5_420_371


def test_decode_edinet_document_survives_junk():
    assert decode_edinet_document(b"") == ""
    assert decode_edinet_document(b"PK\x03\x04not-an-archive") == ""
    assert parse_holdings_csv(b"PK\x03\x04not-an-archive") == {}


def test_parse_holdings_csv_takes_the_group_total(fixtures_dir):
    """Two co-holders plus a group-total row: the total is the reportable number."""
    text = (fixtures_dir / "edinet_holdings.csv").read_text(encoding="utf-8")
    found = parse_holdings_csv(text)
    # Not 214,871 / 0.42, which is co-holder 1 and what first-match matching returned.
    assert found["shares"] == 5_420_371
    assert found["ratio"] == 10.52
    # XBRL `pure` ratios are fractions; 0.0887 is 8.87 percent, not 0.0887 percent.
    assert found["prior_ratio"] == 8.87
    # The issuer is only in the body, never in documents.json.
    assert found["issuer_name"] == "\u65e5\u672c\u96fb\u5b50\u682a\u5f0f\u4f1a\u793e"
    assert found["issuer_code"] == "6951"
    assert found["shares_outstanding"] == 51_532_800
    assert found["obligation_date"] == "2026-07-16"


def test_parse_holdings_csv_single_holder_has_no_group_total(fixtures_dir):
    """A lone holder context is the total; requiring an aggregate row would drop it."""
    text = (fixtures_dir / "edinet_holdings_single_holder.csv").read_text(encoding="utf-8")
    found = parse_holdings_csv(text)
    assert found["shares"] == 1_234_600
    assert found["ratio"] == 8.82
    assert found["shares_outstanding"] == 13_991_900
    # An initial report has no previous ratio, and absent must stay absent, never 0.0.
    assert "prior_ratio" not in found


def test_edinet_ignores_non_large_holding_documents(fixtures_dir):
    docs = json.loads((fixtures_dir / "edinet_documents.json").read_text())["results"]
    adapter = JapanEdinetAdapter(SETTINGS)
    assert adapter.parse({"meta": docs[2], "csv": ""}) == []  # 120, a securities report


def test_edinet_normalize(fixtures_dir):
    docs = json.loads((fixtures_dir / "edinet_documents.json").read_text())["results"]
    csv_text = (fixtures_dir / "edinet_holdings.csv").read_text(encoding="utf-8")
    adapter = JapanEdinetAdapter(SETTINGS)
    rows = adapter.parse({"meta": docs[0], "csv": csv_text})
    event = adapter.normalize(rows[0])

    assert event.jurisdiction == Jurisdiction.JAPAN
    # documents.json really does return secCode=None here, so the ticker can only come
    # from the body's SecurityCodeOfIssuer.
    assert docs[0]["secCode"] is None
    assert event.ticker == "6951.T"
    assert event.issuer_name == "\u65e5\u672c\u96fb\u5b50\u682a\u5f0f\u4f1a\u793e"
    assert event.percent_of_company == 10.52
    assert event.share_count == 5_420_371
    assert event.shares_outstanding == 51_532_800
    assert event.native_currency == "JPY"
    assert event.percentage_threshold_crossed is True
    # docTypeCode 350 covers the change report too, so it is not an amendment.
    assert event.is_amendment is False
    # Priced at the obligation date, not the submission date five days later.
    assert event.transaction_date == date(2026, 7, 16)
    assert event.disclosure_date == date(2026, 7, 24)
    # Japanese names are stored verbatim; nothing here translates anything.
    assert event.filer_name == "\u91ce\u6751\u8b49\u5238\u682a\u5f0f\u4f1a\u793e"


def test_edinet_survives_a_missing_document_body(fixtures_dir):
    docs = json.loads((fixtures_dir / "edinet_documents.json").read_text())["results"]
    adapter = JapanEdinetAdapter(SETTINGS)
    event = adapter.normalize(adapter.parse({"meta": docs[0], "csv": ""})[0])
    assert event.percent_of_company is None
    assert event.share_count is None
    assert event.filer_name == "\u91ce\u6751\u8b49\u5238\u682a\u5f0f\u4f1a\u793e"


def test_edinet_event_clears_the_five_million_gate(fixtures_dir):
    """End to end: a real filing plus a real .T price must produce a real dollar figure."""
    from whale_agent.enrichment.valuation import value_event

    docs = json.loads((fixtures_dir / "edinet_documents.json").read_text())["results"]
    csv_text = (fixtures_dir / "edinet_holdings.csv").read_text(encoding="utf-8")
    adapter = JapanEdinetAdapter(SETTINGS)
    event = adapter.normalize(adapter.parse({"meta": docs[0], "csv": csv_text})[0])

    class _Px:
        """Stands in for FMP, which does resolve 6951.T (verified live: 8,321 JPY)."""

        def close_on(self, ticker, on):
            assert ticker == "6951.T"
            return PriceQuote(price=8321.0, source=PriceSource.CLOSE_ON_DATE, as_of=on)

        def shares_outstanding(self, ticker):  # pragma: no cover - filing supplies it
            raise AssertionError("the filing states its own denominator")

    value_event(event, None, _Px())
    assert event.usd_value is not None
    assert event.usd_value > 5_000_000
    assert event.usd_value_is_estimate is True


# -- Crypto ---------------------------------------------------------------------
def test_whale_alert_drops_fully_anonymous_transfers(fixtures_dir):
    raw = json.loads((fixtures_dir / "whale_alert_response.json").read_text())
    adapter = WhaleAlertAdapter(SETTINGS)
    rows = adapter.parse(raw)
    # The $61M BTC transfer has no owner on either side: a number with no subject.
    assert len(rows) == 1
    assert rows[0]["from_owner"] == "binance"


def test_whale_alert_normalize(fixtures_dir):
    raw = json.loads((fixtures_dir / "whale_alert_response.json").read_text())
    adapter = WhaleAlertAdapter(SETTINGS)
    event = adapter.normalize(adapter.parse(raw)[0])
    assert event.jurisdiction == Jurisdiction.CRYPTO
    assert event.transaction_type == TransactionType.CRYPTO_TRANSFER
    assert event.native_amount == 42_000_000
    assert event.usd_value_is_estimate is True


def test_arkham_requires_an_entity_label(fixtures_dir):
    raw = json.loads((fixtures_dir / "arkham_response.json").read_text())
    adapter = ArkhamAdapter(SETTINGS)
    rows = adapter.parse(raw)
    assert len(rows) == 1  # the unlabelled 0xfeed02 transfer is dropped
    event = adapter.normalize(rows[0])
    assert event.filer_name == "Jump Trading -> Coinbase"
    assert event.native_amount == 75_000_000
    assert event.transaction_date == date(2026, 7, 24)


# -- Dataroma --------------------------------------------------------------------
def test_dataroma_parses_by_column_header(fixtures_dir):
    html = (fixtures_dir / "dataroma_insider_buys.html").read_text()
    adapter = DataromaInsiderBuysAdapter(SETTINGS)
    rows = adapter.parse(html)
    assert len(rows) == 2
    assert rows[0]["insider"] == "Frederico Dominic J"

    event = adapter.normalize(rows[0])
    assert event.ticker == "AGO"
    assert event.issuer_name == "Assured Guaranty Ltd."
    assert event.transaction_type == TransactionType.OPEN_MARKET_BUY
    assert event.native_amount == 5_398_250
    assert event.price_used == 83.05
    assert event.transaction_date == date(2026, 7, 24)
    assert event.source_url.endswith("/m/stock.php?sym=AGO")
