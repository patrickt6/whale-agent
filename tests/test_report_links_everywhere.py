"""Every row the email links into the report must have a matching anchor there.

The reader's repeatedly-raised requirement: every item in the weekly email carries its
own link to the section of the report covering that specific filing. Commit 1c13d19
regressed this to zero per-item links; these tests lock in that every row type the
weekly renders -- not just the top-5 trades -- both gets an "Our report" link when a
report link is available, and that the anchor it points at actually exists on the
report page.
"""

from __future__ import annotations

from datetime import date

from whale_agent.ingestion.fund_watchlist import (
    FastFiling,
    FundSnapshot,
    Holding,
    MarketStake,
    StakeDetail,
)
from whale_agent.ingestion.resale_watch import ResaleRegistration
from whale_agent.jobs.overview import OverviewRow, Section
from whale_agent.publishing.tracked_filings_html import (
    item_anchor,
    manager_anchor,
    render_tracked_filings_page,
    section_anchor,
)
from whale_agent.summarization.render_weekly_html import (
    _fund_moves_block,
    _market_stakes_block,
    _resale_block,
    _section_table,
    _tracked_funds_block,
)

ON = date(2026, 8, 17)
SINCE = date(2026, 8, 10)
REPORT = "https://whale-weekly.example.workers.dev/tracked-filings-2026-08-17"


def _snap(**kw):
    base = dict(
        name="Situational Awareness LP",
        cik="0002045724",
        latest_13f_filed=date(2026, 5, 18),
        total_value_usd=3_855_771_552,
        position_count=26,
        top_holdings=[Holding("BLOOM ENERGY CORP", 878_707_930, 6_485_408)],
    )
    base.update(kw)
    return FundSnapshot(**base)


def test_overview_row_link_anchor_exists_on_the_report_page():
    row = OverviewRow(
        filer="Deep Cut Filer",
        filer_role=None,
        issuer="Deep Cut Issuer",
        ticker="DC",
        what="open market purchase",
        when=ON,
        amount_usd=1_000_000.0,
        amount_label="$1M",
        is_estimate=False,
        filing_url="https://sec.gov/x/y",
        jurisdiction="US",
    )
    section = Section(key="insider", label="Insider filings", rows=[row])

    email_html = _section_table(section, report_link=REPORT)
    anchor = item_anchor(url=row.filing_url)
    assert f"{REPORT}#{anchor}" in email_html
    # One link per row in the email, not two: the SEC citation moved to the report
    # page rather than being paid for twice (see _row_links).
    assert row.filing_url not in email_html

    page_html = "\n".join(render_tracked_filings_page([], [], ON, sections=[section]).values())
    assert f'id="{anchor}"' in page_html
    # The report page keeps the primary-source citation next to the same row.
    assert row.filing_url in page_html


def test_market_stake_link_anchor_exists_on_the_report_page():
    stake = MarketStake(
        filed=ON,
        form="SC 13D",
        holders=["Big Fund LP"],
        detail=StakeDetail(issuer="Widget Co", percent=6.2, person_types=["IA"]),
        url="https://www.sec.gov/Archives/edgar/data/1/000112345600000001/",
        accession="000112345600000001",
    )
    email_html = _market_stakes_block([stake], ON, SINCE, report_link=REPORT)
    anchor = item_anchor(accession=stake.accession)
    assert f"{REPORT}#{anchor}" in email_html
    assert stake.url not in email_html

    page_html = "\n".join(
        render_tracked_filings_page([], [], ON, market_stakes=[stake]).values()
    )
    assert f'id="{anchor}"' in page_html
    assert stake.url in page_html


def test_resale_registration_link_anchor_exists_on_the_report_page():
    reg = ResaleRegistration(
        whale_name="Big Fund LP",
        issuer="Widget Co",
        symbol="WDG",
        form="S-1",
        filed=ON,
        shares_registered=1000,
        shares_held=2000,
        url="https://www.sec.gov/Archives/edgar/data/1/000112345600000002/doc.htm",
        accession="000112345600000002",
    )
    email_html = _resale_block([reg], ON, SINCE, report_link=REPORT)
    anchor = item_anchor(accession=reg.accession)
    assert f"{REPORT}#{anchor}" in email_html
    assert reg.url not in email_html

    page_html = "\n".join(render_tracked_filings_page([], [], ON, resale=[reg]).values())
    assert f'id="{anchor}"' in page_html
    assert reg.url in page_html


def test_fund_moves_filing_link_anchor_exists_on_the_report_page():
    filing = FastFiling(
        form="4",
        filed=ON,
        accession="000112345600000003",
        _cik="0001067983",
    )
    snap = _snap(recent_filings=[filing])

    email_html = _fund_moves_block([snap], ON, SINCE, report_link=REPORT)
    anchor = item_anchor(accession=filing.accession)
    assert f"{REPORT}#{anchor}" in email_html
    assert filing.url not in email_html

    page_html = "\n".join(
        render_tracked_filings_page([], [], ON, snapshots=[snap], since=SINCE).values()
    )
    assert f'id="{anchor}"' in page_html
    assert filing.url in page_html


def test_no_report_link_means_no_our_report_link_anywhere():
    """Without a report link (tracked funds off, or the appendix page failed to
    build), rows must still render -- with only the SEC link, never a half-formed
    anchor URL and never a crash."""
    row = OverviewRow(
        filer="F",
        filer_role=None,
        issuer="I",
        ticker="",
        what="w",
        when=ON,
        amount_usd=None,
        amount_label="not disclosed",
        is_estimate=False,
        filing_url="https://sec.gov/z",
        jurisdiction="US",
    )
    section = Section(key="insider", label="Insider filings", rows=[row])
    html = _section_table(section, report_link="")
    assert "Our report" not in html
    assert "sec.gov" in html


# --- Truncation notes ("N more X this week. See the full report.") -----------------
#
# The reader's feedback flagged three of these -- Carlyle's per-manager
# overflow, the insider-filings section overflow, and the tracked-manager roster
# overflow -- as plain text with no link. The same "See the full report" / "Full
# roster in the report" phrasing is shared by every capped section in this module
# (see EMAIL_*_LIMIT in render_weekly_html.py), so every one of them is covered here,
# not just the three he circled.


def _row(**kw):
    base = dict(
        filer="Filer",
        filer_role=None,
        issuer="Issuer",
        ticker="TIC",
        what="open market purchase",
        when=ON,
        amount_usd=1_000_000.0,
        amount_label="$1M",
        is_estimate=False,
        filing_url="https://sec.gov/row",
        jurisdiction="US",
    )
    base.update(kw)
    return OverviewRow(**base)


def test_section_overflow_note_links_to_its_category_on_the_report_page():
    rows = [_row(filer=f"Filer {i}", filing_url=f"https://sec.gov/row{i}") for i in range(3)]
    section = Section(key="insider", label="Insider filings", rows=rows)

    email_html = _section_table(section, limit=1, report_link=REPORT)
    anchor = section_anchor(section.label)
    assert "See the full report" in email_html
    assert f'<a href="{REPORT}#{anchor}"' in email_html

    page_html = "\n".join(render_tracked_filings_page([], [], ON, sections=[section]).values())
    assert f'id="{anchor}"' in page_html


def test_section_overflow_note_is_plain_text_without_a_report_link():
    rows = [_row(filer=f"Filer {i}", filing_url=f"https://sec.gov/row{i}") for i in range(3)]
    section = Section(key="insider", label="Insider filings", rows=rows)
    email_html = _section_table(section, limit=1, report_link="")
    more_line = email_html.rsplit("</table>", 1)[-1]  # the "more" div, after all rows
    assert "See the full report" in more_line
    assert "<a href" not in more_line


def test_roster_overflow_note_links_to_the_report_roster_section():
    snaps = [_snap(name=f"Manager {i}") for i in range(3)]
    email_html = _tracked_funds_block(snaps, limit=1, report_link=REPORT)
    assert "Full roster in the report" in email_html
    assert f'<a href="{REPORT}#sec-roster"' in email_html

    page_html = "\n".join(
        render_tracked_filings_page([], [], ON, snapshots=snaps, since=SINCE).values()
    )
    assert 'id="sec-roster"' in page_html


def test_roster_overflow_note_is_plain_text_without_a_report_link():
    snaps = [_snap(name=f"Manager {i}") for i in range(3)]
    email_html = _tracked_funds_block(snaps, limit=1, report_link="")
    assert "Full roster in the report" in email_html
    assert "<a href" not in email_html


def test_resale_overflow_note_links_to_the_report_resale_section():
    regs = [
        ResaleRegistration(
            whale_name="Big Fund LP",
            issuer="Widget Co",
            symbol="WDG",
            form="S-1",
            filed=ON,
            shares_registered=1000,
            shares_held=2000,
            url=f"https://www.sec.gov/Archives/edgar/data/1/00011234560000000{i}/doc.htm",
            accession=f"00011234560000000{i}",
        )
        for i in range(3)
    ]
    email_html = _resale_block(regs, ON, SINCE, report_link=REPORT, limit=1)
    assert "See the full report" in email_html
    assert f'<a href="{REPORT}#sec-resale"' in email_html

    page_html = "\n".join(render_tracked_filings_page([], [], ON, resale=regs).values())
    assert 'id="sec-resale"' in page_html


def test_fund_moves_per_manager_overflow_note_links_to_that_managers_block():
    """The Carlyle Group case from the reader's markup: a manager with more filings
    than `filings_per_manager` allows must link its own overflow note to that
    manager's own block on the report page, not the generic top of the page."""
    filings = [
        FastFiling(form="4", filed=ON, accession=f"00011234560000001{i}", _cik="0001067983")
        for i in range(3)
    ]
    snap = _snap(name="Carlyle Group", recent_filings=filings)

    email_html = _fund_moves_block(
        [snap], ON, SINCE, report_link=REPORT, filings_per_manager=1
    )
    anchor = manager_anchor(snap.name)
    assert "more filing(s) from" in email_html
    assert f'<a href="{REPORT}#{anchor}"' in email_html

    page_html = "\n".join(
        render_tracked_filings_page([], [], ON, snapshots=[snap], since=SINCE).values()
    )
    assert f'id="{anchor}"' in page_html


def test_fund_moves_per_manager_overflow_note_is_plain_text_without_a_report_link():
    filings = [
        FastFiling(form="4", filed=ON, accession=f"00011234560000002{i}", _cik="0001067983")
        for i in range(3)
    ]
    snap = _snap(name="Carlyle Group", recent_filings=filings)
    email_html = _fund_moves_block([snap], ON, SINCE, report_link="", filings_per_manager=1)
    more_line = email_html.rsplit("</table>", 1)[-1]  # the "more" div, after all rows
    assert "more filing(s) from" in more_line
    assert "<a href" not in more_line


def test_fund_moves_overall_overflow_note_links_to_the_report_moves_section():
    snaps = [
        _snap(
            name=f"Manager {i}",
            recent_filings=[
                FastFiling(
                    form="4",
                    filed=ON,
                    accession=f"00011234560000003{i}",
                    _cik="0001067983",
                )
            ],
        )
        for i in range(3)
    ]
    email_html = _fund_moves_block(snaps, ON, SINCE, report_link=REPORT, limit=1)
    assert "more tracked managers filed this week" in email_html
    assert f'<a href="{REPORT}#sec-moves"' in email_html

    page_html = "\n".join(
        render_tracked_filings_page([], [], ON, snapshots=snaps, since=SINCE).values()
    )
    assert 'id="sec-moves"' in page_html
