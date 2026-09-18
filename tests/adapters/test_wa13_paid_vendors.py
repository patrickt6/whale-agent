"""WA-13 paid adapters: Finnhub insider transactions, Unusual Whales congress trades.

Fixtures are SCHEMA-DERIVED from the vendors' published specs, not recorded live
responses (no keys were available). See the `_comment` in each fixture.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from whale_agent.config import Settings
from whale_agent.errors import NotConfiguredError
from whale_agent.ingestion.registry import build_sources
from whale_agent.ingestion.vendors.finnhub import FinnhubInsiderAdapter
from whale_agent.ingestion.vendors.unusual_whales import UnusualWhalesCongressAdapter
from whale_agent.models.enums import PriceSource, TransactionType

UNCONFIGURED = Settings()


def _load(fixtures_dir, name):
    return json.loads((fixtures_dir / name).read_text())


def test_finnhub_parse_and_normalize(fixtures_dir):
    adapter = FinnhubInsiderAdapter(UNCONFIGURED)
    rows = adapter.parse(
        _load(fixtures_dir, "finnhub_insider_transactions_schema_derived.json")
    )
    assert len(rows) == 3
    sell = adapter.normalize(rows[0])
    assert sell.source == "finnhub_insider"
    assert sell.transaction_type == TransactionType.OPEN_MARKET_SELL
    assert sell.share_count == 20000  # |change|, not `share` (held after)
    assert sell.price_used == 300.5
    assert sell.price_source == PriceSource.FILING_STATED
    assert sell.transaction_date == date(2026, 9, 8)
    assert sell.disclosure_date == date(2026, 9, 10)
    assert sell.native_amount is None  # no dollar field in the schema
    buy = adapter.normalize(rows[1])
    assert buy.ticker == "TSLA"  # taken from the envelope symbol
    assert buy.transaction_type == TransactionType.OPEN_MARKET_BUY


def test_finnhub_implausible_price_is_rejected_not_kept(fixtures_dir):
    adapter = FinnhubInsiderAdapter(UNCONFIGURED)
    rows = adapter.parse(
        _load(fixtures_dir, "finnhub_insider_transactions_schema_derived.json")
    )
    event = adapter.normalize(rows[2])
    assert event.price_used is None
    assert event.price_source == PriceSource.NOT_PRICED


def test_unusual_whales_parse_and_normalize(fixtures_dir):
    adapter = UnusualWhalesCongressAdapter(UNCONFIGURED)
    rows = adapter.parse(_load(fixtures_dir, "unusual_whales_congress_schema_derived.json"))
    assert len(rows) == 3
    buy = adapter.normalize(rows[0])
    assert buy.transaction_type == TransactionType.OPEN_MARKET_BUY
    assert buy.native_amount == 15001  # lower bound, never a midpoint
    assert buy.usd_value_is_estimate is True
    assert buy.filer_id == "18f9fc95-4661-444e-99f5-99d3778e0c31"
    sale = adapter.normalize(rows[1])
    assert sale.transaction_type == TransactionType.OPEN_MARKET_SELL
    assert sale.native_amount == 1_000_001
    other = adapter.normalize(rows[2])
    assert other.transaction_type == TransactionType.OTHER
    assert other.native_amount is None
    assert other.filer_name == "Someone"


def test_keys_required():
    with pytest.raises(NotConfiguredError):
        FinnhubInsiderAdapter(UNCONFIGURED)._get({})
    with pytest.raises(NotConfiguredError):
        UnusualWhalesCongressAdapter(UNCONFIGURED)._get({})


def test_registry_off_by_default_even_with_key():
    s = Settings(finnhub_api_key="x", unusual_whales_api_key="y")
    specs = {spec.name: spec for spec in build_sources(s)}
    assert specs["finnhub_insider"].enabled is False
    assert specs["finnhub_insider"].disabled_reason == "disabled via WHALE_ENABLE_FINNHUB"
    assert specs["unusual_whales_congress"].enabled is False


def test_registry_enabled_without_key_names_the_key():
    s = Settings(enable_finnhub=True, enable_unusual_whales=True)
    specs = {spec.name: spec for spec in build_sources(s)}
    assert specs["finnhub_insider"].disabled_reason == "FINNHUB_API_KEY not set"
    assert specs["unusual_whales_congress"].disabled_reason == "UNUSUAL_WHALES_API_KEY not set"
    s2 = Settings(
        enable_finnhub=True,
        finnhub_api_key="x",
        enable_unusual_whales=True,
        unusual_whales_api_key="y",
    )
    specs2 = {spec.name: spec for spec in build_sources(s2)}
    assert specs2["finnhub_insider"].enabled and specs2["unusual_whales_congress"].enabled


def test_doctor_warns_for_enabled_keyless_paid_sources():
    from whale_agent.cli.doctor import check_sources

    s = Settings(
        enable_finnhub=True,
        enable_unusual_whales=True,
        enable_quiver=True,
        enable_arkham=True,
        enable_whale_alert=True,
    )
    names = {c.name for c in check_sources(s) if not c.ok}
    for n in (
        "finnhub_insider",
        "unusual_whales_congress",
        "quiver_congress",
        "arkham",
        "whale_alert",
    ):
        assert f"source {n}" in names


def test_settings_picker_lists_every_keyed_source():
    from whale_agent.cli import app

    keys = {s.key: s.section for s in app.SETTINGS}
    for flag in (
        "WHALE_ENABLE_FMP",
        "WHALE_ENABLE_QUIVER",
        "WHALE_ENABLE_ARKHAM",
        "WHALE_ENABLE_WHALE_ALERT",
        "WHALE_ENABLE_FINNHUB",
        "WHALE_ENABLE_UNUSUAL_WHALES",
    ):
        assert keys[flag] == "Sources"
    for key in (
        "FMP_API_KEY",
        "QUIVER_QUANT_API_KEY",
        "ARKHAM_API_KEY",
        "WHALE_ALERT_API_KEY",
        "FINNHUB_API_KEY",
        "UNUSUAL_WHALES_API_KEY",
    ):
        assert keys[key] == "Keys"
