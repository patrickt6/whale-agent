"""CIK to ticker resolution (enrichment/tickers.py).

No network anywhere: the one test that exercises the SEC-backed resolver injects a fake
transport, which is also how it can assert the "one request for the whole run" promise
that the rest of the enrichment layer depends on.
"""

from __future__ import annotations

import json

import pytest

from whale_agent.config import Settings
from whale_agent.enrichment import tickers as tickers_module
from whale_agent.enrichment.tickers import (
    CachingTickerResolver,
    NullTickerResolver,
    SecTickerResolver,
    StaticTickerResolver,
    normalize_cik,
    parse_company_tickers,
)
from whale_agent.errors import SourceUnavailableError

SETTINGS = Settings(sec_user_agent="whale-agent test@example.com")


@pytest.fixture
def sec_payload(fixtures_dir):
    return (fixtures_dir / "sec_company_tickers.json").read_text()


@pytest.fixture
def resolver(tmp_path, sec_payload, monkeypatch):
    """A SEC resolver wired to a counting fake transport and a throwaway cache file."""
    calls: list[str] = []

    def fake_request_text(url, **kwargs):
        calls.append(url)
        return sec_payload

    monkeypatch.setattr(tickers_module, "request_text", fake_request_text)
    made = SecTickerResolver(SETTINGS, cache_path=tmp_path / "company_tickers.json")
    made.calls = calls  # type: ignore[attr-defined]
    return made


# -- normalisation ------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["0000320193", "320193", 320193, " 320193 ", "CIK0000320193"],
)
def test_every_spelling_of_a_cik_normalises_to_the_same_key(raw):
    """Padded, unpadded, integer and CIK-prefixed forms all name one registrant.

    Sources disagree about padding -- ownership XML pads, SEC's own JSON does not -- and
    comparing them raw produces a miss that is indistinguishable from an unknown issuer.
    """
    assert normalize_cik(raw) == "0000320193"


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-cik"])
def test_a_cik_with_no_digits_normalises_to_none(raw):
    """Garbage in gives None, not a zero-padded string of nothing."""
    assert normalize_cik(raw) is None


def test_the_sec_file_parses_into_padded_cik_to_ticker(sec_payload):
    """The published index-keyed object becomes a flat mapping we can look up."""
    mapping = parse_company_tickers(json.loads(sec_payload))
    assert mapping["0000320193"] == "AAPL"
    assert mapping["0001045810"] == "NVDA"


def test_a_multi_class_registrant_keeps_the_first_listed_symbol(sec_payload):
    """Alphabet files GOOGL and GOOG under one CIK; SEC lists the larger class first."""
    mapping = parse_company_tickers(json.loads(sec_payload))
    assert mapping["0001652044"] == "GOOGL"


def test_a_row_missing_a_ticker_is_skipped_rather_than_defaulted(sec_payload):
    """A registrant with no listed symbol must not resolve to an empty or wrong one."""
    mapping = parse_company_tickers(json.loads(sec_payload))
    assert "0001499961" not in mapping


# -- static resolver ----------------------------------------------------------
def test_the_static_resolver_accepts_either_cik_spelling():
    """Tests and demos can key the mapping however is convenient."""
    static = StaticTickerResolver({"320193": "aapl"})
    assert static.resolve_cik("0000320193") == "AAPL"
    assert static.resolve_cik(320193) == "AAPL"


def test_the_null_resolver_resolves_nothing():
    """The explicit way to say "ticker lookup is switched off"."""
    assert NullTickerResolver().resolve_cik("0000320193") is None


# -- SEC resolver -------------------------------------------------------------
def test_the_sec_resolver_finds_a_ticker_from_a_padded_or_unpadded_cik(resolver):
    """The live callers pass padded CIKs; the SEC file stores unpadded integers."""
    assert resolver.resolve_cik("0000320193") == "AAPL"
    assert resolver.resolve_cik(1045810) == "NVDA"


def test_an_unknown_cik_resolves_to_none_rather_than_raising(resolver):
    """A registrant we cannot name costs its event enrichment, not the run."""
    assert resolver.resolve_cik("0009999999") is None
    assert resolver.resolve_cik(None) is None


def test_resolving_many_events_makes_exactly_one_request(resolver):
    """One file covers every registrant, so a digest of any size costs one fetch.

    Per-event lookups would be hundreds of requests against a source that asks us to be
    polite, and would put a network round trip in the middle of normalize().
    """
    for _ in range(50):
        resolver.resolve_cik("0000320193")
        resolver.resolve_cik("0001045810")
        resolver.resolve_cik("0009999999")
    assert resolver.fetch_count == 1
    assert len(resolver.calls) == 1


def test_a_second_resolver_reads_the_cache_instead_of_the_network(tmp_path, resolver):
    """The mapping changes on the order of weeks, so runs should not refetch it."""
    assert resolver.resolve_cik("0000320193") == "AAPL"
    second = SecTickerResolver(SETTINGS, cache_path=resolver.cache_path)
    assert second.resolve_cik("0000320193") == "AAPL"
    assert second.fetch_count == 0


def test_a_sec_outage_degrades_to_no_tickers_and_is_not_retried(tmp_path, monkeypatch):
    """A source that is down must not raise, and must not be asked again per event."""
    calls: list[str] = []

    def boom(url, **kwargs):
        calls.append(url)
        raise SourceUnavailableError("503")

    monkeypatch.setattr(tickers_module, "request_text", boom)
    down = SecTickerResolver(SETTINGS, cache_path=tmp_path / "missing.json")
    assert down.resolve_cik("0000320193") is None
    assert down.resolve_cik("0001045810") is None
    assert len(calls) == 1


def test_an_unreadable_cache_falls_back_to_fetching(tmp_path, resolver):
    """A truncated or half-written cache file is a slow run, not a crash."""
    resolver.cache_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.cache_path.write_text("{not json", encoding="utf-8")
    assert resolver.resolve_cik("0000320193") == "AAPL"


def test_the_caching_wrapper_swallows_a_delegate_that_raises():
    """No resolver, however badly behaved, may sink a digest run."""

    class Exploding:
        def resolve_cik(self, cik):
            raise RuntimeError("boom")

    wrapped = CachingTickerResolver(Exploding())
    assert wrapped.resolve_cik("0000320193") is None


def test_company_names_are_parsed_from_the_same_sec_file():
    """SEC publishes the legal name alongside the ticker; we used to throw it away.

    FMP returns companyName null often enough that the issuer arrives as its own ticker,
    which is how an article went out headlined "FSBC" instead of Five Star Bancorp. The
    name is free, authoritative, and already in a file we download anyway.
    """
    from whale_agent.enrichment.tickers import parse_company_names

    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1856365, "ticker": "FSBC", "title": "Five Star Bancorp"},
    }
    names = parse_company_names(payload)
    assert names["FSBC"] == "Five Star Bancorp"
    assert names["AAPL"] == "Apple Inc."


def test_company_names_skip_rows_missing_either_field():
    """A name attached to the wrong symbol is worse than no name."""
    from whale_agent.enrichment.tickers import parse_company_names

    payload = [
        {"cik_str": 1, "ticker": "GOOD", "title": "Good Co"},
        {"cik_str": 2, "ticker": "NONAME"},
        {"cik_str": 3, "title": "No Ticker Inc"},
    ]
    names = parse_company_names(payload)
    assert names == {"GOOD": "Good Co"}


def test_issuer_names_are_filled_in_only_where_the_name_was_just_the_symbol():
    """The invariant from market_context, applied to the SEC name map.

    A name the filing itself supplied is authoritative and is never overwritten. Only an
    issuer whose "name" is nothing more than its own ticker gets replaced.
    """
    from tests.conftest import make_event
    from whale_agent.enrichment.tickers import fill_issuer_names

    events = [
        make_event(issuer_name="FSBC", ticker="FSBC"),
        make_event(issuer_name="Apple Inc.", ticker="AAPL"),
        make_event(issuer_name="UNKNOWN", ticker="GOOD"),
        make_event(issuer_name="NOTLISTED", ticker="NOTLISTED"),
    ]
    filled = fill_issuer_names(
        events, {"FSBC": "Five Star Bancorp", "AAPL": "WRONG NAME", "GOOD": "Good Co"}
    )
    assert filled[0].issuer_name == "Five Star Bancorp"
    assert filled[1].issuer_name == "Apple Inc."  # never overwritten
    assert filled[2].issuer_name == "Good Co"  # placeholder is not a real name
    assert filled[3].issuer_name == "NOTLISTED"  # nothing to fill from, left alone


def test_shouting_sec_names_are_cased_for_reading():
    """SEC stores many names uppercase. A headline is prose, not a filing field."""
    from whale_agent.enrichment.tickers import parse_company_names

    payload = [
        {"cik_str": 1, "ticker": "FSBC", "title": "FIVE STAR BANCORP"},
        {"cik_str": 2, "ticker": "TSM", "title": "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"},
        {"cik_str": 3, "ticker": "CHCO", "title": "CITY HOLDING CO"},
    ]
    names = parse_company_names(payload)
    assert names["FSBC"] == "Five Star Bancorp"
    assert names["TSM"] == "Taiwan Semiconductor Manufacturing Co Ltd"
    assert names["CHCO"] == "City Holding Co"


def test_a_name_that_already_has_mixed_case_is_left_exactly_as_filed():
    """Only shouting is corrected. Deliberate capitalisation is someone's actual name."""
    from whale_agent.enrichment.tickers import parse_company_names

    payload = [
        {"cik_str": 1, "ticker": "BYRN", "title": "Byrna Technologies Inc."},
        {"cik_str": 2, "ticker": "TALK", "title": "51Talk Online Education Group"},
        {"cik_str": 3, "ticker": "AMGB", "title": "AMG BBH Asset-Backed Credit Fund, LLC"},
    ]
    names = parse_company_names(payload)
    assert names["BYRN"] == "Byrna Technologies Inc."
    assert names["TALK"] == "51Talk Online Education Group"
    assert names["AMGB"] == "AMG BBH Asset-Backed Credit Fund, LLC"
