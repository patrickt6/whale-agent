"""Each report-page section must read as a written note about that specific filing --
prose covering the filing itself, context, prior position, when, who, and any other
news -- not a restatement of the email row. See jobs/digest_weekly.py's caller and
ingestion/position_history.py + enrichment/news_context.py, the two data layers this
prose is built from.
"""

from __future__ import annotations

from datetime import date

from whale_agent.enrichment.news_context import NewsItem
from whale_agent.ingestion.fund_watchlist import PooledStake, PooledTrade
from whale_agent.publishing.tracked_filings_html import (
    filing_anchor,
    render_tracked_filings_page,
)
from whale_agent.storage.db import Store

ON = date(2026, 8, 17)


def _content_html(pages: dict) -> str:
    """Every page's html except the index/landing page, joined. The index page is
    intentionally a real navigation menu (`<ul class="menu">`) now -- the reader's
    "no lists" requirement was always about the filing PROSE reading as flowing
    paragraphs, not about the site's own table-of-contents chrome."""
    return "\n".join(html for name, html in pages.items() if "-index." not in name)


def _trade(**kw):
    base = dict(
        manager="Big Fund LP",
        manager_cik="0001067983",
        direction="sold",
        issuer="Corebridge Financial",
        symbol="CRBG",
        shares=14_500_000,
        price=33.87,
        dollar_value=14_500_000 * 33.87,
        filed=ON,
        form="4",
        accession="0001067983-26-000042",
        url="https://www.sec.gov/x/index.htm",
        portfolio_value_usd=5_000_000_000,
        percent_of_portfolio=9.8,
    )
    base.update(kw)
    return PooledTrade(**base)


def _stake(**kw):
    base = dict(
        manager="Big Fund LP",
        manager_cik="0001067983",
        issuer="Widget Co",
        percent=6.2,
        shares=1_000_000,
        filed=ON,
        form="SC 13D",
        is_new=True,
        accession="0001067983-26-000099",
        url="https://www.sec.gov/x2/index.htm",
    )
    base.update(kw)
    return PooledStake(**base)


def test_no_lists_anywhere_in_the_report_page():
    trade = _trade()
    html = _content_html(render_tracked_filings_page([trade], [], ON))
    assert "<ul" not in html
    assert "<li" not in html
    assert "<table" not in html


def test_new_position_reads_differently_from_no_prior_history():
    """A manager with a real 13F history that shows the issuer absent last quarter
    (status "new") must not read the same as one with no history on file at all."""
    store = Store(":memory:")
    from whale_agent.ingestion.fund_watchlist import Holding

    store.record_13f_holdings(
        "0001067983",
        date(2026, 6, 30),
        "0001067983-26-000010",
        "https://sec.gov/prior",
        [Holding(issuer="OTHER CO", value_usd=1000, shares=10, cusip="000000000")],
        filed_date=date(2026, 7, 1),
    )
    store.record_13f_holdings(
        "0001067983",
        date(2026, 3, 31),
        "0001067983-26-000005",
        "https://sec.gov/older",
        [Holding(issuer="OTHER CO", value_usd=900, shares=9, cusip="000000000")],
        filed_date=date(2026, 4, 1),
    )
    trade = _trade(direction="bought", issuer="Corebridge Financial")
    html_with_history = "\n".join(
        render_tracked_filings_page([trade], [], ON, store=store).values()
    )

    html_no_store = "\n".join(render_tracked_filings_page([trade], [], ON).values())

    assert html_with_history != html_no_store
    assert "no 13f" in html_no_store.lower() or "not available" in html_no_store.lower()


def test_trim_with_related_news_quotes_and_links_the_headline():
    def fake_news_fetcher(*, ticker=None, cik=None, around):
        return [
            NewsItem(
                headline="Corebridge Reports Q2 Loss",
                publisher="Reuters",
                published=date(2026, 8, 14),
                url="https://reuters.example/x",
                source="fmp_stock_news",
            )
        ]

    trade = _trade(direction="sold")
    html = "\n".join(
        render_tracked_filings_page([trade], [], ON, news_fetcher=fake_news_fetcher).values()
    )
    assert "Corebridge Reports Q2 Loss" in html
    assert "https://reuters.example/x" in html
    assert "Reuters" in html
    # Never assert causation between the news and the trade.
    assert "because" not in html.lower()


def test_no_news_found_says_so_plainly():
    def empty_news_fetcher(*, ticker=None, cik=None, around):
        return []

    trade = _trade()
    html = "\n".join(
        render_tracked_filings_page([trade], [], ON, news_fetcher=empty_news_fetcher).values()
    )
    assert "no news" in html.lower() or "nothing turned up" in html.lower()


def test_stake_never_states_a_dollar_value():
    stake = _stake()
    html = "\n".join(render_tracked_filings_page([], [stake], ON).values())
    # 13D/G carries no price -- must never fabricate a dollar figure for it.
    assert (
        "$" not in html.split(f'id="{filing_anchor(stake.accession)}"')[1].split("</div>")[0]
    )


def test_anchors_still_present_for_every_trade_and_stake():
    trade = _trade()
    stake = _stake()
    html = "\n".join(render_tracked_filings_page([trade], [stake], ON).values())
    assert f'id="{filing_anchor(trade.accession)}"' in html
    assert f'id="{filing_anchor(stake.accession)}"' in html


def test_insider_filing_row_becomes_prose_with_estimate_explanation():
    from whale_agent.jobs.overview import OverviewRow, Section

    row = OverviewRow(
        filer="Frazier Life Sciences XI, L.P.",
        filer_role="10% owner",
        issuer="Attovia Therapeutics, Inc.",
        ticker="ATTO",
        what="other",
        when=date(2026, 8, 6),
        amount_usd=331_200_000,
        amount_label="$331.2M",
        is_estimate=True,
        filing_url="https://www.sec.gov/x3/index.htm",
        jurisdiction="US",
    )
    section = Section(key="insider", label="Insider filings", rows=[row])
    html = _content_html(render_tracked_filings_page([], [], ON, sections=[section]))
    assert "<li" not in html
    assert "<table" not in html
    assert "Frazier Life Sciences" in html
    assert "10% owner" in html
    assert "Attovia Therapeutics" in html
    assert "estimate" in html.lower()
    assert "reference" in html.lower()


def test_form4_block_flags_tax_withholding_not_a_sale():
    from whale_agent.ingestion.fund_watchlist import FastFiling, FundSnapshot, Trade

    trade = Trade(
        issuer="Amanat Acquisition Corp",
        symbol="AMAN",
        on=date(2026, 8, 13),
        code="F",
        shares=1_200,
        price=10.50,
        acquired=False,
    )
    filing = FastFiling(
        form="4",
        filed=date(2026, 8, 14),
        accession="0000093751-26-000567",
        trades=[trade],
        _cik="0000093751",
    )
    snap = FundSnapshot(name="State Street", cik="0000093751", recent_filings=[filing])
    html = _content_html(
        render_tracked_filings_page([], [], ON, snapshots=[snap], since=date(2026, 8, 10))
    )
    assert "<li" not in html
    assert "<table" not in html
    assert "<p" in html
    body = html.split(f'id="{filing_anchor("0000093751-26-000567")}"')[1]
    assert "withheld for tax" in body.lower()
    assert "sold" not in body.lower().split("</div>")[0]


def test_13g_block_states_no_dollar_value():
    from whale_agent.ingestion.fund_watchlist import FastFiling, FundSnapshot, StakeDetail

    detail = StakeDetail(
        issuer="Amanat Acquisition Corp",
        percent=9.4,
        shares=732_867,
        holders=["Sculptor Capital"],
        person_types=["IA"],
    )
    filing = FastFiling(
        form="SCHEDULE 13G/A",
        filed=date(2026, 8, 14),
        accession="0001054587-26-000010",
        detail=detail,
        _cik="0001054587",
    )
    snap = FundSnapshot(name="Sculptor Capital", cik="0001054587", recent_filings=[filing])
    html = "\n".join(
        render_tracked_filings_page(
            [], [], ON, snapshots=[snap], since=date(2026, 8, 10)
        ).values()
    )
    assert "<table" not in html
    assert f'id="{filing_anchor(filing.accession)}"' in html
    body = html.split(f'id="{filing_anchor(filing.accession)}"')[1].split("</div>")[0]
    assert "$" not in body


def test_roster_flags_unusually_old_13f():
    from whale_agent.ingestion.fund_watchlist import FundSnapshot

    stale = FundSnapshot(
        name="BlackRock",
        cik="0001086364",
        position_count=3710,
        total_value_usd=98_753_590_000,
        period_end=date(2016, 12, 31),
        latest_13f_url="https://www.sec.gov/x4/index.htm",
    )
    html = "\n".join(render_tracked_filings_page([], [], ON, snapshots=[stale]).values())
    assert "unusually old" in html.lower()
    assert "2016-12-31" in html


def test_roster_recent_filing_reads_plainly_without_stale_warning():
    from whale_agent.ingestion.fund_watchlist import FundSnapshot

    fresh = FundSnapshot(
        name="Vanguard Group",
        cik="0000102909",
        position_count=4329,
        total_value_usd=6_897_676_080_637,
        period_end=date(2025, 12, 31),
        latest_13f_url="https://www.sec.gov/x5/index.htm",
    )
    html = "\n".join(render_tracked_filings_page([], [], ON, snapshots=[fresh]).values())
    assert "unusually old" not in html.lower()
    assert "Vanguard Group" in html
    assert "4,329" in html


def test_no_em_dash_en_dash_or_curly_quotes_anywhere_on_the_page():
    """House rule: the report must not read as machine-generated. No em/en
    dashes (literal or entity), and no curly quotes, in any prose block -- including
    the older _trade_prose/_stake_prose/_quarterly_filing_prose helpers this test
    exercises via a fixture that touches every section on the page."""
    from whale_agent.ingestion.fund_watchlist import (
        FastFiling,
        FundSnapshot,
        StakeDetail,
        Trade,
    )
    from whale_agent.jobs.overview import OverviewRow, Section

    def fake_news_fetcher(*, ticker=None, cik=None, around):
        return [
            NewsItem(
                headline="Corebridge Reports Q2 Loss",
                publisher="Reuters",
                published=date(2026, 8, 14),
                url="https://reuters.example/x",
                source="fmp_stock_news",
            )
        ]

    trade = _trade(direction="sold")
    stake = _stake()

    row = OverviewRow(
        filer="Frazier Life Sciences XI, L.P.",
        filer_role="10% owner",
        issuer="Attovia Therapeutics, Inc.",
        ticker="ATTO",
        what="other",
        when=date(2026, 8, 6),
        amount_usd=331_200_000,
        amount_label="$331.2M",
        is_estimate=True,
        filing_url="https://www.sec.gov/x3/index.htm",
        jurisdiction="US",
    )
    section = Section(key="insider", label="Insider filings", rows=[row])

    trade4 = Trade(
        issuer="Amanat Acquisition Corp",
        symbol="AMAN",
        on=date(2026, 8, 13),
        code="F",
        shares=1_200,
        price=10.50,
        acquired=False,
    )
    f4 = FastFiling(
        form="4",
        filed=date(2026, 8, 14),
        accession="0000093751-26-000567",
        trades=[trade4],
        _cik="0000093751",
    )
    detail = StakeDetail(
        issuer="Amanat Acquisition Corp",
        percent=9.4,
        shares=732_867,
        holders=["Sculptor Capital"],
        person_types=["IA"],
    )
    f13g = FastFiling(
        form="SCHEDULE 13G/A",
        filed=date(2026, 8, 14),
        accession="0001054587-26-000010",
        detail=detail,
        _cik="0001054587",
    )
    snap1 = FundSnapshot(name="State Street", cik="0000093751", recent_filings=[f4])
    snap2 = FundSnapshot(name="Sculptor Capital", cik="0001054587", recent_filings=[f13g])
    roster_stale = FundSnapshot(
        name="BlackRock",
        cik="0001086364",
        position_count=3710,
        total_value_usd=98_753_590_000,
        period_end=date(2016, 12, 31),
        latest_13f_url="https://www.sec.gov/x2/index.htm",
    )

    html = "\n".join(
        render_tracked_filings_page(
            [trade],
            [stake],
            ON,
            snapshots=[snap1, snap2, roster_stale],
            sections=[section],
            since=date(2026, 8, 11),
            news_fetcher=fake_news_fetcher,
        ).values()
    )

    for bad in ("—", "–", "&mdash;", "&ndash;"):
        assert bad not in html, f"found {bad!r} in rendered page"
    for bad in ("‘", "’", "“", "”"):
        assert bad not in html, f"found curly quote {bad!r} in rendered page"
    assert "&ldquo;" not in html and "&rdquo;" not in html
    assert " -- " not in html


def test_report_page_has_exactly_one_style_block_and_no_external_resources():
    trade = _trade()
    stake = _stake()
    pages = render_tracked_filings_page([trade], [stake], ON)

    # Each PAGE is a complete, well-formed document: exactly one <style> block of
    # its own, not one shared across the whole (now multi-page) report.
    for filename, page_html in pages.items():
        assert page_html.count("<style") == 1, filename
        assert page_html.count("</style>") == 1, filename

    html = "\n".join(pages.values())

    # Anchors the email deep-links into must survive the styling pass.
    assert f'id="{filing_anchor(trade.accession)}"' in html
    assert f'id="{filing_anchor(stake.accession)}"' in html

    # Self-contained static page: no external fonts, CDNs, or image hosts.
    forbidden = [
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "cdn.",
        "<link ",
        "@import",
        "http://",
    ]
    lowered = html.lower()
    for needle in forbidden:
        assert needle not in lowered, f"unexpected external resource marker: {needle!r}"

    # https:// only shows up as legitimate SEC/news source hrefs inside <a> tags, not
    # as a resource load (script/link/img src).
    assert 'src="http' not in lowered
    assert "<script" not in lowered
