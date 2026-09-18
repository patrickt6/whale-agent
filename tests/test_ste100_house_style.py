"""House rule: every prose sentence a reader sees follows ASD-STE100
Simplified Technical English -- see docs/DECISIONS.md "STE100 for all reader-facing
prose". This is not a full STE100 compliance engine; it is the same kind of
mechanical, conservative catch as
`test_no_em_dash_en_dash_or_curly_quotes_anywhere_on_the_page` in
`test_tracked_filings_prose.py`, extended to three more checks that can be caught
by regex without a grammar model:

  1. Sentence length: a rendered <p> block's sentences must not run past ~25 words.
     Long, comma-spliced sentences are the most common violation this codebase's
     generated prose produces.
  2. High-confidence passive voice: "was/were/is/are/been/being <verb>ed/en by"
     constructions, e.g. "the shares were sold by the fund". This pattern alone is
     intentionally narrow -- passive voice without an explicit "by ..." agent is
     common in English and not reliably distinguishable from active voice by
     regex, so it is not flagged.
  3. Banned vague qualifiers: quite, fairly, very, a lot of, somewhat, rather.

What this deliberately does NOT catch: passive voice without a "by" clause,
noun-cluster stacking, non-simple tenses, "-ing" nouns/adjectives, or anything
requiring actual parsing. Those are real STE100 rules this check cannot enforce
mechanically; a human editor still has to catch them. See docs/DECISIONS.md for
the known gaps left after this pass.

Scope: this test scans only the text inside <p>...</p> blocks of the rendered
tracked-filings report page (the same fixture the em-dash test builds), because
that is where this module's generated sentences live. Headings, table cells and
nav links are deliberately excluded -- they are short labels concatenated by a
naive tag-stripper, not sentences, and flagging them would be a false positive
the STE100 rule was never meant to catch.
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import date

from whale_agent.enrichment.news_context import NewsItem
from whale_agent.ingestion.fund_watchlist import (
    FastFiling,
    FundSnapshot,
    PooledStake,
    PooledTrade,
    StakeDetail,
    Trade,
)
from whale_agent.jobs.overview import OverviewRow, Section
from whale_agent.publishing.tracked_filings_html import render_tracked_filings_page

ON = date(2026, 8, 17)

MAX_SENTENCE_WORDS = 25

_PASSIVE_RE = re.compile(r"\b(was|were|is|are|been|being)\s+\w+(ed|en)\s+by\b", re.I)
_QUALIFIER_RE = re.compile(r"\b(quite|fairly|very|a lot of|somewhat|rather)\b", re.I)


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


def _full_page_html() -> str:
    """Every prose-generating section on the report page, in one render call --
    mirrors the fixture `test_no_em_dash_en_dash_or_curly_quotes_anywhere_on_the_page`
    builds, so both house-style checks exercise the same surface."""

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

    # The report is now a small library of pages, one per section (see
    # tracked_filings_html.PAGE_SLUGS); joined back together here since this test only
    # cares about the prose sentences somewhere on the site, not which specific page
    # they land on.
    pages = render_tracked_filings_page(
        [trade],
        [stake],
        ON,
        snapshots=[snap1, snap2, roster_stale],
        sections=[section],
        since=date(2026, 8, 11),
        news_fetcher=fake_news_fetcher,
    )
    return "\n".join(pages.values())


def _prose_sentences(html_out: str) -> list[str]:
    """Text of every sentence inside a <p>...</p> block, tags and entities
    stripped. Restricted to <p> blocks (not the whole page) so headings and table
    cells -- which run together without sentence punctuation once tags are
    stripped -- are not misread as one long sentence."""
    sentences: list[str] = []
    for para in re.findall(r"<p[^>]*>(.*?)</p>", html_out, flags=re.S):
        text = re.sub(r"<[^>]+>", " ", para)
        text = htmllib.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        sentences.extend(s for s in re.split(r"(?<=[.!?])\s+", text) if s)
    return sentences


def test_generated_prose_sentences_are_not_too_long():
    sentences = _prose_sentences(_full_page_html())
    assert sentences, "fixture produced no prose to check"
    too_long = [s for s in sentences if len(s.split()) > MAX_SENTENCE_WORDS]
    assert not too_long, (
        f"STE100: sentence(s) over {MAX_SENTENCE_WORDS} words in rendered prose: {too_long}"
    )


def test_generated_prose_avoids_high_confidence_passive_voice():
    sentences = _prose_sentences(_full_page_html())
    hits = [s for s in sentences if _PASSIVE_RE.search(s)]
    assert not hits, (
        "STE100: passive 'was/were/is/are ... by' construction in rendered prose "
        f"(prefer active voice): {hits}"
    )


def test_generated_prose_avoids_banned_vague_qualifiers():
    sentences = _prose_sentences(_full_page_html())
    hits = [s for s in sentences if _QUALIFIER_RE.search(s)]
    assert not hits, f"STE100: banned vague qualifier in rendered prose: {hits}"
