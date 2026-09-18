"""The 13F headline in the weekly email: leads the email, ranked by move size
against the manager's own book, stays inside the 90,000-byte budget even at the
35-manager deadline-clustering scale that shipped silently in 2026-08."""

from __future__ import annotations

from datetime import date

from whale_agent.ingestion.fund_watchlist import Holding
from whale_agent.ingestion.quarterly_filings import recent_quarterly_filings
from whale_agent.publishing.tracked_filings_html import filing_anchor
from whale_agent.storage.db import Store
from whale_agent.summarization.render_weekly_html import render_weekly_html

ON = date(2026, 8, 17)


def _filings_for(n: int) -> list:
    store = Store(":memory:")
    watchlist = {}
    for i in range(n):
        cik = f"{i:010d}"
        watchlist[f"Fund {i}"] = cik
        prior = [
            Holding("NVIDIA CORPORATION", 1_000_000_000 + i, 500_000, cusip="67066G104"),
        ]
        current = [
            Holding("NVIDIA CORPORATION", 1_600_000_000 + i, 700_000, cusip="67066G104"),
            Holding(f"NEWCO {i}", 200_000_000 + i, 10_000, cusip=f"{i:09d}"),
        ]
        store.record_13f_holdings(
            cik,
            date(2025, 12, 31),
            f"000{i:04d}-25-000111",
            f"https://sec.gov/prior-{i}.htm",
            prior,
            filed_date=date(2026, 2, 10),
        )
        store.record_13f_holdings(
            cik,
            date(2026, 3, 31),
            f"000{i:04d}-26-000222",
            f"https://sec.gov/current-{i}.htm",
            current,
            filed_date=date(2026, 8, 14),
        )
    return recent_quarterly_filings(
        store, date(2026, 8, 10), date(2026, 8, 16), watchlist=watchlist
    )


def test_quarterly_headline_appears_and_links_into_the_anchor():
    filings = _filings_for(3)
    html = render_weekly_html(
        on=ON,
        cleared=5,
        recorded=10,
        articles=[],
        sections=[],
        how_to_read="",
        quarterly_filings=filings,
        tracked_report_link="https://example.com/tracked-filings-2026-08-17",
    )
    assert "13F filings this week" in html
    assert f"#{filing_anchor(filings[0].accession)}" in html
    assert "Fund 0" in html or "Fund 1" in html


def test_no_quarterly_filings_renders_nothing_extra():
    html = render_weekly_html(
        on=ON,
        cleared=5,
        recorded=10,
        articles=[],
        sections=[],
        how_to_read="",
        quarterly_filings=[],
    )
    assert "13F filings this week" not in html


def test_email_stays_under_90kb_with_35_managers_filing_in_one_week():
    """The exact scale of the 2026-08-14 deadline miss: 35 managers, all filing in
    the same window."""
    filings = _filings_for(35)
    html = render_weekly_html(
        on=ON,
        cleared=40,
        recorded=90,
        articles=[],
        sections=[],
        how_to_read="",
        quarterly_filings=filings,
        tracked_report_link="https://example.com/tracked-filings-2026-08-17",
    )
    size = len(html.encode("utf-8"))
    assert size < 90_000, f"email is {size} bytes, over the 90,000 budget"
