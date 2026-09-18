"""Provenanced news/context lookup (enrichment/news_context.py).

No network anywhere: `fetch_fmp_news`/`fetch_sec_8k` are monkeypatched at the module
level for the `context_for` glue tests, and everything else exercises the pure
parse/filter/dedupe functions against fixture-shaped dicts.
"""

from __future__ import annotations

from datetime import date

from whale_agent.config import Settings
from whale_agent.enrichment import news_context as nc
from whale_agent.enrichment.news_context import (
    NewsItem,
    context_for,
    parse_8k_filings,
    parse_fmp_news,
)

SETTINGS = Settings(sec_user_agent="whale-agent test@example.com", fmp_api_key="test-key")


# -- parse_fmp_news -----------------------------------------------------------


def test_fmp_rows_for_the_requested_symbol_are_kept():
    rows = [
        {
            "symbol": "AAPL",
            "publishedDate": "2026-08-06 15:46:50",
            "publisher": "Reuters",
            "title": "Apple announces buyback",
            "url": "https://example.com/a",
        }
    ]
    out = parse_fmp_news(rows, symbol="AAPL", source="fmp_stock_news")
    assert len(out) == 1
    assert out[0] == NewsItem(
        headline="Apple announces buyback",
        publisher="Reuters",
        published=date(2026, 8, 6),
        url="https://example.com/a",
        source="fmp_stock_news",
    )


def test_fmp_rows_for_a_different_symbol_are_dropped_even_if_returned():
    """Defensive re-check: this codebase already caught FMP silently ignoring one
    filter (insider `from`/`to`), so a row's own `symbol` field is trusted over the
    request parameter that supposedly produced it."""
    rows = [
        {
            "symbol": "MSFT",
            "publishedDate": "2026-08-06 15:46:50",
            "publisher": "Reuters",
            "title": "Microsoft news, not Apple",
            "url": "https://example.com/b",
        }
    ]
    assert parse_fmp_news(rows, symbol="AAPL", source="fmp_stock_news") == []


def test_fmp_row_missing_title_url_or_date_is_dropped_not_guessed():
    rows = [
        {"symbol": "AAPL", "publishedDate": "2026-08-06", "publisher": "X", "url": ""},
        {"symbol": "AAPL", "publishedDate": "", "publisher": "X", "title": "T", "url": "u"},
    ]
    assert parse_fmp_news(rows, symbol="AAPL", source="fmp_stock_news") == []


def test_fmp_row_with_no_publisher_falls_back_to_site_then_unknown():
    rows = [
        {
            "symbol": "AAPL",
            "publishedDate": "2026-08-06",
            "site": "reuters.com",
            "title": "T",
            "url": "u",
        },
        {
            "symbol": "AAPL",
            "publishedDate": "2026-08-06",
            "title": "T2",
            "url": "u2",
        },
    ]
    out = parse_fmp_news(rows, symbol="AAPL", source="fmp_stock_news")
    assert out[0].publisher == "reuters.com"
    assert out[1].publisher == "unknown publisher"


# -- parse_8k_filings -----------------------------------------------------------


def test_8k_rows_get_a_headline_built_from_their_own_item_codes():
    recent = {
        "form": ["8-K", "10-Q", "8-K"],
        "filingDate": ["2026-08-05", "2026-08-01", "2026-06-23"],
        "accessionNumber": ["0001853145-26-000028", "x", "0001140361-26-026089"],
        "items": ["2.02,9.01", "", "5.07"],
    }
    out = parse_8k_filings(recent, cik="0001853145")
    assert len(out) == 2  # the 10-Q is excluded
    assert out[0].headline == (
        "8-K: Results of Operations and Financial Condition; Financial Statements and Exhibits"
    )
    assert out[0].source == "sec_8k"
    assert out[0].publisher == "SEC EDGAR"
    assert out[0].published == date(2026, 8, 5)
    assert out[0].url == (
        "https://www.sec.gov/Archives/edgar/data/1853145/000185314526000028/"
        "0001853145-26-000028-index.htm"
    )
    assert out[1].headline == "8-K: Submission of Matters to a Vote of Security Holders"


def test_8k_row_with_no_items_field_still_gets_a_generic_headline():
    recent = {
        "form": ["8-K"],
        "filingDate": ["2026-01-01"],
        "accessionNumber": ["0001-26-000001"],
    }
    out = parse_8k_filings(recent, cik="1")
    assert out[0].headline == "Form 8-K filed"


def test_8k_row_missing_accession_or_date_is_dropped():
    recent = {
        "form": ["8-K", "8-K"],
        "filingDate": ["", "2026-01-01"],
        "accessionNumber": ["0001-26-000001", ""],
        "items": ["1.01", "1.01"],
    }
    assert parse_8k_filings(recent, cik="1") == []


# -- context_for glue -----------------------------------------------------------


def test_context_for_returns_empty_list_not_none_when_nothing_found(monkeypatch):
    monkeypatch.setattr(nc, "fetch_fmp_news", lambda *a, **k: [])
    monkeypatch.setattr(nc, "fetch_sec_8k", lambda *a, **k: [])
    out = context_for(ticker="ZZZZ", cik="1", around=date(2026, 8, 10), settings=SETTINGS)
    assert out == []


def test_context_for_skips_the_source_whose_identifier_is_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(nc, "fetch_fmp_news", lambda *a, **k: calls.append("fmp") or [])
    monkeypatch.setattr(nc, "fetch_sec_8k", lambda *a, **k: calls.append("sec") or [])
    context_for(ticker="AAPL", cik=None, around=date(2026, 8, 10), settings=SETTINGS)
    assert calls == ["fmp"]
    calls.clear()
    context_for(ticker=None, cik="320193", around=date(2026, 8, 10), settings=SETTINGS)
    assert calls == ["sec"]


def test_context_for_dedupes_identical_headline_and_url_across_sources(monkeypatch):
    item = NewsItem(
        headline="Same story",
        publisher="X",
        published=date(2026, 8, 10),
        url="https://example.com/x",
        source="fmp_stock_news",
    )
    monkeypatch.setattr(nc, "fetch_fmp_news", lambda *a, **k: [item])
    monkeypatch.setattr(nc, "fetch_sec_8k", lambda *a, **k: [item])
    out = context_for(ticker="AAPL", cik="320193", around=date(2026, 8, 10), settings=SETTINGS)
    assert out == [item]


def test_context_for_filters_sec_items_to_the_window_but_trusts_fmp_server_side_scoping(
    monkeypatch,
):
    """FMP is asked with from/to and its rows are trusted as already scoped (verified
    live: see the module docstring). SEC's 8-K list is NOT date-scoped by the fetch
    itself -- it is the issuer's whole recent filing history -- so context_for applies
    the window locally."""
    in_window = NewsItem("in", "SEC EDGAR", date(2026, 8, 10), "u1", "sec_8k")
    out_of_window = NewsItem("out", "SEC EDGAR", date(2020, 1, 1), "u2", "sec_8k")
    monkeypatch.setattr(nc, "fetch_fmp_news", lambda *a, **k: [])
    monkeypatch.setattr(nc, "fetch_sec_8k", lambda *a, **k: [in_window, out_of_window])
    out = context_for(
        ticker=None, cik="320193", around=date(2026, 8, 10), window_days=3, settings=SETTINGS
    )
    assert out == [in_window]


def test_context_for_sorts_newest_first(monkeypatch):
    older = NewsItem("old", "X", date(2026, 8, 1), "u1", "fmp_stock_news")
    newer = NewsItem("new", "X", date(2026, 8, 9), "u2", "fmp_stock_news")
    monkeypatch.setattr(nc, "fetch_fmp_news", lambda *a, **k: [older, newer])
    monkeypatch.setattr(nc, "fetch_sec_8k", lambda *a, **k: [])
    out = context_for(ticker="AAPL", around=date(2026, 8, 10), settings=SETTINGS)
    assert out == [newer, older]
