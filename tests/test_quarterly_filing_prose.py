"""The per-filing 13F report-page section: prose covering overall portfolio change,
largest moves, and news context for a manager's quarterly filing -- matching the
established style in tracked_filings_html.py (no lists, one anchored div per filing,
provenance on every figure)."""

from __future__ import annotations

from datetime import date

from whale_agent.enrichment.news_context import NewsItem
from whale_agent.ingestion.fund_watchlist import Holding
from whale_agent.ingestion.quarterly_filings import recent_quarterly_filings
from whale_agent.publishing.tracked_filings_html import (
    filing_anchor,
    render_tracked_filings_page,
)
from whale_agent.storage.db import Store

ON = date(2026, 8, 17)
CIK = "0001067983"


def _seeded_store():
    store = Store(":memory:")
    prior = [
        Holding("NVIDIA CORPORATION", 1_000_000_000, 500_000, cusip="67066G104"),
        Holding("ORACLE CORP", 800_000_000, 200_000, cusip="68389X105"),
    ]
    current = [
        Holding("NVIDIA CORPORATION", 1_600_000_000, 700_000, cusip="67066G104"),
        Holding("BROADCOM INC", 900_000_000, 50_000, cusip="11135F101"),
    ]
    store.record_13f_holdings(
        CIK,
        date(2025, 12, 31),
        "0001067983-26-000010",
        "https://sec.gov/prior-index.htm",
        prior,
        filed_date=date(2026, 2, 10),
    )
    store.record_13f_holdings(
        CIK,
        date(2026, 3, 31),
        "0001067983-26-000020",
        "https://sec.gov/current-index.htm",
        current,
        filed_date=date(2026, 8, 14),
    )
    return store


def test_quarterly_section_has_no_lists_and_carries_the_accession_anchor():
    store = _seeded_store()
    filings = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK},
    )
    # Excludes the index/landing page: its table-of-contents is intentionally a real
    # `<ul class="menu">` now (the reader's "no lists" complaint was about the filing
    # prose, not the site's own navigation chrome).
    pages = render_tracked_filings_page([], [], ON, store=store, quarterly=filings)
    html = "\n".join(v for k, v in pages.items() if "-index." not in k)
    assert "<ul" not in html
    assert "<li" not in html
    assert f'id="{filing_anchor(filings[0].accession)}"' in html


def test_quarterly_section_states_both_report_periods_and_both_source_urls():
    store = _seeded_store()
    filings = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK},
    )
    html = "\n".join(
        render_tracked_filings_page([], [], ON, store=store, quarterly=filings).values()
    )
    assert "2026-03-31" in html
    assert "2025-12-31" in html
    assert "https://sec.gov/current-index.htm" in html
    assert "https://sec.gov/prior-index.htm" in html


def test_quarterly_section_with_no_prior_says_so_plainly_and_never_says_new_portfolio():
    store = Store(":memory:")
    holdings = [Holding("NVIDIA CORPORATION", 1_600_000_000, 700_000, cusip="67066G104")]
    store.record_13f_holdings(
        CIK,
        date(2026, 3, 31),
        "0001067983-26-000020",
        "https://sec.gov/current-index.htm",
        holdings,
        filed_date=date(2026, 8, 14),
    )
    filings = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK},
    )
    html = "\n".join(
        render_tracked_filings_page([], [], ON, store=store, quarterly=filings).values()
    )
    assert "no prior" in html.lower() or "no earlier" in html.lower()
    assert "new portfolio" not in html.lower()


def test_quarterly_section_never_calls_a_put_ownership_or_a_bearish_bet():
    store = Store(":memory:")
    prior = [Holding("NVIDIA CORPORATION", 1_000_000_000, 500_000, cusip="67066G104")]
    current = [
        Holding("NVIDIA CORPORATION", 1_000_000_000, 500_000, cusip="67066G104"),
        Holding("TESLA INC", 400_000_000, None, cusip="88160R101", option_type="PUT"),
    ]
    store.record_13f_holdings(
        CIK,
        date(2025, 12, 31),
        "0001067983-26-000010",
        "https://sec.gov/prior-index.htm",
        prior,
        filed_date=date(2026, 2, 10),
    )
    store.record_13f_holdings(
        CIK,
        date(2026, 3, 31),
        "0001067983-26-000020",
        "https://sec.gov/current-index.htm",
        current,
        filed_date=date(2026, 8, 14),
    )
    filings = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK},
    )
    html = "\n".join(
        render_tracked_filings_page([], [], ON, store=store, quarterly=filings).values()
    )
    assert "bearish bet" not in html.lower()
    assert "owns tesla" not in html.lower()


def test_quarterly_moves_render_as_a_real_table_not_flowing_prose():
    store = _seeded_store()
    filings = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK},
    )
    html = "\n".join(
        render_tracked_filings_page([], [], ON, store=store, quarterly=filings).values()
    )
    assert "<table" in html
    assert "<th" in html
    assert "<td" in html
    assert "Opened" in html
    assert "Added to" in html or "Trimmed" in html or "No longer reported" in html


def test_no_longer_reported_caveat_appears_once_per_section_not_once_per_name():
    """Regression for the reader-reported defect: on a real render, the sentence
    fragment about a 13F's silence on a name appeared 133 times on one page, once
    per no-longer-reported holding, instead of once per section. Build a filing with
    three closed names and assert the caveat sentence appears exactly once while all
    three names still appear (each tagged 'No longer reported')."""
    store = Store(":memory:")
    prior = [
        Holding("NVIDIA CORPORATION", 1_000_000_000, 500_000, cusip="67066G104"),
        Holding("ORACLE CORP", 800_000_000, 200_000, cusip="68389X105"),
        Holding("VANECK ETF TRUST", 600_000_000, 100_000, cusip="92189F100"),
        Holding("APPLE INC", 400_000_000, 50_000, cusip="037833100"),
    ]
    current = [
        Holding("BROADCOM INC", 900_000_000, 50_000, cusip="11135F101"),
    ]
    store.record_13f_holdings(
        CIK,
        date(2025, 12, 31),
        "0001067983-26-000010",
        "https://sec.gov/prior-index.htm",
        prior,
        filed_date=date(2026, 2, 10),
    )
    store.record_13f_holdings(
        CIK,
        date(2026, 3, 31),
        "0001067983-26-000020",
        "https://sec.gov/current-index.htm",
        current,
        filed_date=date(2026, 8, 14),
    )
    filings = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK},
    )
    html = "\n".join(
        render_tracked_filings_page([], [], ON, store=store, quarterly=filings).values()
    )

    assert html.count(">No longer reported<") == 4
    assert "NVIDIA CORPORATION" in html
    assert "ORACLE CORP" in html
    assert "VANECK ETF TRUST" in html
    assert "APPLE INC" in html
    assert html.count("A 13F's silence on a name does not itself prove it was sold") == 1


def test_quarterly_news_context_uses_the_same_lookup_pattern_and_never_states_causation():
    store = _seeded_store()
    filings = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK},
    )

    def fetcher(*, ticker=None, cik=None, around):
        return [
            NewsItem(
                headline="Broadcom Beats Estimates",
                publisher="Reuters",
                published=date(2026, 8, 13),
                url="https://reuters.example/broadcom",
                source="fmp_stock_news",
            )
        ]

    html = "\n".join(
        render_tracked_filings_page(
            [], [], ON, store=store, quarterly=filings, news_fetcher=fetcher
        ).values()
    )
    assert "Broadcom Beats Estimates" in html
    assert "because" not in html.lower()
