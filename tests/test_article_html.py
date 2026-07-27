"""The article page: escaping, provenance, and the sections that must not be buried."""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.analysis.patterns import detect_patterns
from whale_agent.jobs.digest_weekly import (
    NO_THESIS_LINE,
    publish_articles,
    render_weekly,
    weekly_theses,
)
from whale_agent.models.enums import TransactionType
from whale_agent.publishing.article_html import (
    article_filename,
    render_article_html,
    write_article,
)
from whale_agent.summarization.provenance import check_unsourced_numbers
from whale_agent.summarization.render_html import html_to_text
from whale_agent.summarization.thesis import fallback_thesis

ON = date(2026, 7, 25)


def buy(filer: str, issuer: str = "SmallCo", **overrides):
    base = dict(
        filer_name=filer,
        filer_id="F-" + filer.lower().replace(" ", "-"),
        issuer_name=issuer,
        issuer_id="I-" + issuer.lower().replace(" ", "-"),
        transaction_type=TransactionType.OPEN_MARKET_BUY,
        usd_value=10_000_000.0,
        source_url="https://www.sec.gov/Archives/edgar/data/123/000186332826000002.txt",
    )
    base.update(overrides)
    return make_event(**base)


def a_cluster(names=("Alpha Capital", "Beta Partners", "Gamma Advisors")):
    return [buy(name) for name in names]


def test_article_escapes_hostile_filer_names():
    """Names arrive from third-party feeds and land in the page body verbatim."""
    events = a_cluster(('<script>alert("x")</script>', "Beta Partners", "Gamma Advisors"))
    thesis = fallback_thesis(detect_patterns(events)[0], ON)
    page = render_article_html(thesis, events)
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


def test_article_carries_no_figure_the_patterns_own_filings_cannot_account_for():
    """The gate runs against the pattern's events, never the whole week."""
    events = a_cluster()
    pattern = detect_patterns(events)[0]
    page = render_article_html(fallback_thesis(pattern, ON), pattern.events)
    assert check_unsourced_numbers(html_to_text(page), pattern.events) == []


def test_counter_evidence_and_falsifier_are_in_the_body_not_a_footer():
    """They appear before the sourcing block, which is what gives them their weight."""
    events = a_cluster()
    pattern = detect_patterns(events)[0]
    page = render_article_html(fallback_thesis(pattern, ON), pattern.events)
    assert page.index("The case against") < page.index("Sourcing")
    assert "What would show this is wrong" in page
    assert pattern.falsifier[:40] in html_to_text(page)


def test_every_citation_is_listed_with_its_link():
    """A sourcing footer that omits a citation makes the article uncheckable."""
    events = a_cluster()
    thesis = fallback_thesis(detect_patterns(events)[0], ON)
    page = render_article_html(thesis, events)
    for citation in thesis.citations:
        assert citation.label.replace("&", "&amp;") in page
        if citation.url:
            assert citation.url in page


def test_the_page_is_self_contained():
    """No stylesheet, no font file, no script, no REMOTE asset: it survives being saved.

    Images are allowed, but only inlined. The invariant is that the page is one file: a
    saved copy, an attachment and a served copy must all look the same, and a remote
    src leaves a broken box in the first two. So every img on the page has to be a data
    URI, and asserting that is stricter than asserting there are no images at all.
    """
    import re

    events = a_cluster()
    page = render_article_html(fallback_thesis(detect_patterns(events)[0], ON), events)
    assert "<script" not in page.lower()
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
    for src in re.findall(r'<img[^>]+src="([^"]*)"', page):
        assert src.startswith("data:"), (
            f"remote image on a page that must be one file: {src[:60]}"
        )


def test_filenames_are_slug_based_and_stable(tmp_path):
    """Regenerating a thesis overwrites its own page rather than minting a second URL."""
    events = a_cluster()
    thesis = fallback_thesis(detect_patterns(events)[0], ON)
    first = write_article(thesis, tmp_path, events)
    second = write_article(thesis, tmp_path, events)
    assert first == second == tmp_path / article_filename(thesis)
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_a_week_with_no_pattern_produces_no_articles_and_still_renders(tmp_path):
    """The weekly ships on a quiet week, says so plainly, and pads nothing."""
    events = [buy("Solo Capital")]  # one filer: nothing for a detector to find
    theses = weekly_theses(events, ON)
    assert theses == []
    report = render_weekly(events, ON, articles=publish_articles(theses, events, tmp_path))
    assert NO_THESIS_LINE in report
    # The long method block was cut at the owner's instruction. What has to
    # survive is the one sentence saying what this is.
    assert "Awareness tool, not investment advice." in report
    assert list(tmp_path.glob("*.html")) == []


def test_the_weekly_links_to_articles_rather_than_inlining_them(tmp_path):
    """The email carries a title, a claim, and a link; the argument lives on the page."""
    events = a_cluster()
    articles = publish_articles(
        weekly_theses(events, ON), events, tmp_path, base_url="https://example.test/notes"
    )
    assert len(articles) == 1
    thesis, link = articles[0]
    report = render_weekly(events, ON, articles=articles)
    assert link == f"https://example.test/notes/{article_filename(thesis)}"
    assert link in report
    # The evidence and sourcing sections belong to the page, not to the email.
    assert thesis.citations[0].detail not in report
    # The long method block was cut at the owner's instruction. What has to
    # survive is the one sentence saying what this is.
    assert "Awareness tool, not investment advice." in report
