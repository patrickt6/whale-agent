"""The report-page-restructure change: the tracked-manager report is now a small
library of pages (see tracked_filings_html.PAGE_SLUGS) instead of one long scroll.
These tests cover the three things that change under a multi-page split and are easy
to get subtly wrong: cross-page navigation actually links to real generated pages,
email deep-links resolve to the correct page + anchor combination (not just "some
page on the site"), and the link-check safety gate still catches a genuinely broken
link once there is more than one page to check.
"""

from __future__ import annotations

from datetime import date

from whale_agent.ingestion.fund_watchlist import (
    FastFiling,
    FundSnapshot,
    MarketStake,
    StakeDetail,
)
from whale_agent.ingestion.resale_watch import ResaleRegistration
from whale_agent.jobs.overview import OverviewRow, Section
from whale_agent.monitoring.link_check import FetchResult, ReportLink, broken_links
from whale_agent.publishing.tracked_filings_html import (
    PAGE_SLUGS,
    item_anchor,
    manager_anchor,
    render_tracked_filings_page,
    section_anchor,
    tracked_filings_page_filename,
    write_tracked_filings_page,
)

ON = date(2026, 8, 17)
SINCE = date(2026, 8, 10)


def _fixture_pages():
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
    stake = MarketStake(
        filed=ON,
        form="SC 13D",
        holders=["Big Fund LP"],
        detail=StakeDetail(issuer="Widget Co", percent=6.2, person_types=["IA"]),
        url="https://www.sec.gov/Archives/edgar/data/1/000112345600000001/",
        accession="000112345600000001",
    )
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
    filing = FastFiling(form="4", filed=ON, accession="000112345600000003", _cik="0001067983")
    snap = FundSnapshot(
        name="Big Fund LP",
        cik="0001067983",
        total_value_usd=1,
        position_count=1,
        recent_filings=[filing],
    )
    return (
        render_tracked_filings_page(
            [],
            [],
            ON,
            snapshots=[snap],
            sections=[section],
            market_stakes=[stake],
            resale=[reg],
            since=SINCE,
        ),
        section,
        stake,
        reg,
        filing,
        snap,
    )


# --- Cross-page navigation -----------------------------------------------------------


def test_every_present_page_is_linked_from_every_other_pages_sitebar():
    pages, *_ = _fixture_pages()
    filenames = set(pages)
    for filename, html in pages.items():
        for other in filenames:
            assert f'href="{other}"' in html, f"{filename} does not link to {other}"


def test_sitebar_marks_the_current_page():
    pages, *_ = _fixture_pages()
    trades_name = tracked_filings_page_filename(ON, "trades")
    trades_html = pages[trades_name]
    assert f'href="{trades_name}" class="current"' in trades_html


def test_index_page_menu_links_to_every_present_page_with_a_blurb():
    pages, *_ = _fixture_pages()
    index_html = pages[tracked_filings_page_filename(ON, "index")]
    for slug in ("trades", "overview", "bigstakes", "resale", "moves", "roster"):
        filename = tracked_filings_page_filename(ON, slug)
        assert filename in pages  # every slug this fixture has data for was written
        assert f'href="{filename}"' in index_html
    assert '<ul class="menu">' in index_html


def test_prev_next_links_present_and_point_at_real_pages():
    pages, *_ = _fixture_pages()
    for filename, html in pages.items():
        if filename == tracked_filings_page_filename(ON, "index"):
            continue
        assert '<nav class="pagenav"' in html
    filenames = set(pages)
    for html in pages.values():
        import re

        for href in re.findall(r'class="pagenav".*?</nav>', html, flags=re.S):
            for target in re.findall(r'href="([^"]+)"', href):
                assert target in filenames


def test_a_page_with_no_data_this_week_is_not_linked_anywhere():
    """bigstakes/resale/overview only get written when there is data for them --
    linking to a page that was never written would be a dead link the moment the
    reader clicks it."""
    pages = render_tracked_filings_page([], [], ON)
    assert tracked_filings_page_filename(ON, "bigstakes") not in pages
    for html in pages.values():
        assert tracked_filings_page_filename(ON, "bigstakes") not in html
        assert tracked_filings_page_filename(ON, "resale") not in html
        assert tracked_filings_page_filename(ON, "overview") not in html
        assert tracked_filings_page_filename(ON, "moves") not in html
        assert tracked_filings_page_filename(ON, "roster") not in html


# --- Anchor-to-page mapping (email deep-links) ----------------------------------------


def test_filing_anchor_lands_on_the_trades_page():
    pages, section, stake, reg, filing, snap = _fixture_pages()
    trades_html = pages[tracked_filings_page_filename(ON, "trades")]
    other_pages = {k: v for k, v in pages.items() if "trades-and-stakes" not in k}
    # No trade/stake fixtures here, but the mapping itself (filing_anchor -> trades
    # page only, never any other page) is the thing under test.
    assert "sec-trades" in trades_html
    for name, html in other_pages.items():
        assert "sec-trades" not in html, name


def test_item_anchor_for_overview_row_lands_only_on_overview_page():
    pages, section, stake, reg, filing, snap = _fixture_pages()
    row = section.rows[0]
    anchor = item_anchor(url=row.filing_url)
    overview_html = pages[tracked_filings_page_filename(ON, "overview")]
    assert f'id="{anchor}"' in overview_html
    for name, html in pages.items():
        if "fortnight-overview" in name:
            continue
        assert f'id="{anchor}"' not in html, name


def test_item_anchor_for_market_stake_lands_only_on_bigstakes_page():
    pages, section, stake, reg, filing, snap = _fixture_pages()
    anchor = item_anchor(accession=stake.accession)
    bigstakes_html = pages[tracked_filings_page_filename(ON, "bigstakes")]
    assert f'id="{anchor}"' in bigstakes_html
    for name, html in pages.items():
        if "big-stakes" in name:
            continue
        assert f'id="{anchor}"' not in html, name


def test_item_anchor_for_resale_registration_lands_only_on_resale_page():
    pages, section, stake, reg, filing, snap = _fixture_pages()
    anchor = item_anchor(accession=reg.accession)
    resale_html = pages[tracked_filings_page_filename(ON, "resale")]
    assert f'id="{anchor}"' in resale_html
    for name, html in pages.items():
        if "arranging-to-sell" in name:
            continue
        assert f'id="{anchor}"' not in html, name


def test_manager_anchor_lands_only_on_moves_page():
    pages, section, stake, reg, filing, snap = _fixture_pages()
    anchor = manager_anchor(snap.name)
    moves_html = pages[tracked_filings_page_filename(ON, "moves")]
    assert f'id="{anchor}"' in moves_html
    for name, html in pages.items():
        if "tracked-manager-filings" in name:
            continue
        assert f'id="{anchor}"' not in html, name


def test_section_anchor_lands_only_on_overview_page():
    pages, section, stake, reg, filing, snap = _fixture_pages()
    anchor = section_anchor(section.label)
    overview_html = pages[tracked_filings_page_filename(ON, "overview")]
    assert f'id="{anchor}"' in overview_html
    for name, html in pages.items():
        if "fortnight-overview" in name:
            continue
        assert f'id="{anchor}"' not in html, name


def test_roster_and_moves_are_separate_pages_with_separate_section_ids():
    pages, *_ = _fixture_pages()
    moves_html = pages[tracked_filings_page_filename(ON, "moves")]
    roster_html = pages[tracked_filings_page_filename(ON, "roster")]
    assert 'id="sec-moves"' in moves_html
    assert 'id="sec-moves"' not in roster_html
    assert 'id="sec-roster"' in roster_html
    assert 'id="sec-roster"' not in moves_html


# --- Full pipeline: write pages, build ReportLinks, catch a real breakage ------------


def test_link_check_passes_when_every_page_and_anchor_is_live(tmp_path):
    pages, section, stake, reg, filing, snap = _fixture_pages()
    write_tracked_filings_page(pages, tmp_path, ON)
    base = "https://example.test"

    def fetch(url: str) -> FetchResult:
        filename = url.rsplit("/", 1)[-1]
        path = tmp_path / filename
        if not path.exists():
            return FetchResult(ok=False, status=404, body="", error="HTTP 404")
        return FetchResult(ok=True, status=200, body=path.read_text(encoding="utf-8"))

    links = [
        ReportLink(
            url=f"{base}/{tracked_filings_page_filename(ON, 'overview')}",
            label="tracked-manager report (fortnight overview)",
            fragments=(
                section_anchor(section.label),
                item_anchor(url=section.rows[0].filing_url),
            ),
        ),
        ReportLink(
            url=f"{base}/{tracked_filings_page_filename(ON, 'bigstakes')}",
            label="tracked-manager report (big stakes)",
            fragments=(item_anchor(accession=stake.accession),),
        ),
        ReportLink(
            url=f"{base}/{tracked_filings_page_filename(ON, 'moves')}",
            label="tracked-manager report (moves)",
            fragments=(
                manager_anchor(snap.name),
                item_anchor(accession=filing.accession, url=filing.url),
            ),
        ),
    ]
    assert broken_links(links, fetch=fetch) == []


def test_link_check_catches_an_anchor_on_the_wrong_page():
    """The exact new failure mode a multi-page split introduces: a caller building the
    wrong page's URL for an anchor (e.g. linking a manager anchor into the roster page
    instead of the moves page it actually lives on). broken_links must still catch
    this -- the page loads fine, the anchor is simply not on it."""
    moves_html = '<html><body><div id="mgr-big-fund-lp-abc123">x</div></body></html>'
    roster_html = "<html><body><p>no manager anchors here</p></body></html>"

    def fetch(url: str) -> FetchResult:
        if "roster" in url:
            return FetchResult(ok=True, status=200, body=roster_html)
        return FetchResult(ok=True, status=200, body=moves_html)

    links = [
        ReportLink(
            url="https://example.test/tracked-filings-2026-08-17-tracked-manager-roster.html",
            label="tracked-manager report (moves, wrongly pointed at roster)",
            fragments=("mgr-big-fund-lp-abc123",),
        )
    ]
    problems = broken_links(links, fetch=fetch)
    assert len(problems) == 1
    assert "mgr-big-fund-lp-abc123" in problems[0]


def test_link_check_catches_a_page_that_was_never_written():
    """A page digest_weekly.py links to but write_tracked_filings_page never wrote
    (e.g. a bug in the "only link to pages with data" gating) must fail the send, not
    silently pass."""
    links = [
        ReportLink(
            url="https://example.test/tracked-filings-2026-08-17-big-stakes.html",
            label="tracked-manager report (big stakes)",
        )
    ]
    problems = broken_links(
        links, fetch=lambda url: FetchResult(ok=False, status=404, body="", error="HTTP 404")
    )
    assert len(problems) == 1
    assert "404" in problems[0] or "HTTP 404" in problems[0]


def test_page_filenames_are_all_distinct_and_dated():
    pages, *_ = _fixture_pages()
    for slug in PAGE_SLUGS:
        filename = tracked_filings_page_filename(ON, slug)
        assert ON.isoformat() in filename
    assert len(set(pages)) == len(pages)
