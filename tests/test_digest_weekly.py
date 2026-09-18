"""Weekly aggregation and the permanent evidence block."""

from __future__ import annotations

from datetime import UTC, date

from tests.conftest import make_event
from whale_agent.jobs.digest_weekly import (
    cross_jurisdiction,
    most_bought,
    render_weekly,
)
from whale_agent.jobs.overview import build_overview
from whale_agent.models.annotation import ContextAnnotation
from whale_agent.models.enums import Jurisdiction, TransactionType


def test_most_bought_ranks_by_independent_filers_not_dollars():
    events = [
        make_event(filer_name="A", issuer_name="SmallCo", usd_value=6_000_000.0),
        make_event(filer_name="B", issuer_name="SmallCo", usd_value=6_000_000.0),
        make_event(filer_name="C", issuer_name="SmallCo", usd_value=6_000_000.0),
        make_event(filer_name="D", issuer_name="BigCo", usd_value=500_000_000.0),
    ]
    ranked = most_bought(events)
    # Three unrelated buyers is a different observation from one very large buyer.
    assert ranked[0][0] == "SmallCo"
    assert ranked[0][1] == 3


def test_most_bought_counts_each_filer_once():
    events = [
        make_event(filer_name="A", issuer_name="Co", usd_value=6_000_000.0),
        make_event(filer_name="A", issuer_name="Co", usd_value=7_000_000.0),
    ]
    assert most_bought(events)[0][1] == 1


def test_most_bought_ignores_sells():
    events = [
        make_event(
            filer_name="A",
            issuer_name="Co",
            transaction_type=TransactionType.OPEN_MARKET_SELL,
            usd_value=9_000_000.0,
        )
    ]
    assert most_bought(events) == []


def test_cross_jurisdiction_detection():
    events = [
        make_event(issuer_name="GlobalCo", jurisdiction=Jurisdiction.US),
        make_event(issuer_name="GlobalCo", jurisdiction=Jurisdiction.TAIWAN),
        make_event(issuer_name="LocalCo", jurisdiction=Jurisdiction.US),
    ]
    result = dict(cross_jurisdiction(events))
    assert result == {"GlobalCo": ["TW", "US"]}


def test_the_disclaimer_survives_even_though_the_long_block_did_not():
    report = render_weekly([], date(2026, 7, 25))
    # The long method block was cut at the owner's instruction. What has to
    # survive is the one sentence saying what this is.
    assert "Awareness tool, not investment advice." in report
    # The academic caveats went with the block. They are not gone from the product: the
    # research they cite still shapes the scoring and is stated in the module docstrings.
    # What the reader gets is the one line that says what this is.
    assert "not investment advice" in report


def test_annotations_are_labelled_as_context_not_events():
    report = render_weekly(
        [make_event(usd_value=6_000_000.0)],
        date(2026, 7, 25),
        annotations=[
            ContextAnnotation(
                source="quiver_lobbying",
                kind="lobbying",
                ticker="LMT",
                issuer_name="Lockheed Martin",
                as_of=date(2026, 6, 30),
                amount_usd=3_120_000.0,
                label="lobbying spend",
            )
        ],
    )
    assert "slow context, not events" in report
    assert "Lockheed Martin" in report


def test_header_does_not_claim_sub_threshold_rows_cleared_the_gate():
    """The bug: the header counted every row in the window and called them all whales.

    The window deliberately holds sub-threshold rows, because a cluster is gated on its
    aggregate and four $2M buys into one issuer are the finding. Counting those as
    "cleared the threshold" told the reader 2,884 whales on a week that had far fewer,
    which is the one kind of error this product cannot make.
    """
    events = [
        make_event(filer_name="A", issuer_name="Co", usd_value=9_000_000.0),
        make_event(filer_name="B", issuer_name="Co", usd_value=2_000_000.0),
        make_event(filer_name="C", issuer_name="Co", usd_value=2_000_000.0),
    ]
    header = render_weekly(events, date(2026, 7, 26)).splitlines()[1]
    assert "1 " in header and "cleared" in header
    # The other two are still reported, but never as threshold-clearing.
    assert "3" in header
    assert "3 disclosed moves cleared" not in header


def test_placeholder_issuer_names_never_reach_the_most_bought_table():
    """A vendor null became the literal issuer "NONE" in a reader-facing ranking."""
    events = [
        make_event(filer_name="A", issuer_name="NONE", usd_value=20_000_000.0),
        make_event(filer_name="B", issuer_name="NONE", usd_value=20_000_000.0),
        make_event(filer_name="C", issuer_name="RealCo", usd_value=6_000_000.0),
    ]
    names = [row[0] for row in most_bought(events)]
    assert "NONE" not in names
    assert "RealCo" in names


def test_the_weekly_screens_stored_events_for_plausibility():
    """The gate ran in the daily pipeline; the weekly read straight past it.

    Implausible rows are deliberately kept in storage rather than deleted, so any reader
    of the store has to screen them again. The weekly did not, which put a $1.1
    quadrillion insider row into a reader-facing table -- traceable to a vendor field,
    and false.
    """
    from whale_agent.jobs.digest_weekly import screen_for_report

    good = make_event(filer_name="A", issuer_name="Co", usd_value=9_000_000.0)
    absurd = make_event(
        filer_name="B",
        issuer_name="Big",
        share_count=40_000_000.0,
        price_used=40_000_000.0,
        usd_value=1_600_000_000_000_000.0,
    )
    kept = screen_for_report([good, absurd])
    assert [e.filer_name for e in kept] == ["A"]


def test_the_weekly_html_actually_renders():
    """A pin against the layout failing silently.

    run_weekly_digest wraps the HTML build in a try/except so a layout error costs the
    look and never the send. That is right, and it also means a completely broken
    renderer produces a green suite and a plainly formatted email nobody notices. This
    calls the renderer directly, which is the only way the failure is loud.
    """
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    markup = render_weekly_html(
        on=date(2026, 7, 26),
        cleared=54,
        recorded=1843,
        articles=[],
        sections=build_overview(
            [make_event(filer_name="A", usd_value=9_000_000.0)], date(2026, 7, 26)
        ),
        how_to_read="- evidence line",
        no_thesis_line="nothing this week",
        macro_lines=["Policy rate: 3.71%, as of 2026-06-30."],
        macro_sourcing="Treasury Fiscal Data.",
    )
    assert markup.startswith("<!doctype html>")
    assert "WHALE" in markup and "Insider filings" in markup
    assert "Policy rate" in markup
    # The hero is hotlinked in mail, never inlined: Gmail strips data URIs on images.
    assert 'img src="https://' in markup
    assert "data:image" not in markup
    assert "—" not in markup and "–" not in markup


def test_a_quiet_non_core_source_is_disclosed_in_the_weekly(tmp_path):
    """A source that stopped answering is a gap in the report, and is said out loud."""
    from datetime import datetime, timedelta

    from whale_agent.config import Settings
    from whale_agent.jobs.digest_weekly import run_weekly_digest
    from whale_agent.storage.db import Store

    store = Store(":memory:")
    try:
        now = datetime.now(UTC)
        store.record_source_run("sec_form4", ok=True, at=now - timedelta(hours=1))
        store.record_source_run("taiwan_mops", ok=True, at=now - timedelta(hours=96))
        settings = Settings(email_provider="none")
        report, _ = run_weekly_digest(store, settings, deliver=False, articles_dir=tmp_path)
        assert "taiwan_mops" in report
        assert "Coverage note" in report
    finally:
        store.close()


def test_weekly_send_is_blocked_when_report_links_are_not_yet_reachable(tmp_path):
    """The recurring failure: pages rendered but not yet published, email about to go
    out anyway. The send must refuse and say why, rather than deliver dead links."""
    from whale_agent.config import Settings
    from whale_agent.jobs.digest_weekly import run_weekly_digest
    from whale_agent.monitoring.link_check import FetchResult
    from whale_agent.storage.db import Store

    store = Store(":memory:")
    try:
        settings = Settings(email_provider="none")

        def never_reachable(url: str) -> FetchResult:
            return FetchResult(ok=False, status=404, body="", error="HTTP 404")

        report, results = run_weekly_digest(
            store,
            settings,
            deliver=True,
            articles_dir=tmp_path,
            article_base_url="https://example.test",
            link_fetcher=never_reachable,
        )
        assert len(results) == 1
        assert results[0].channel == "link-check"
        assert results[0].ok is False
        assert "not reachable" in results[0].detail or "not live" in results[0].detail
        assert "example.test" in results[0].detail
    finally:
        store.close()


def test_weekly_send_proceeds_when_report_links_are_reachable(tmp_path):
    """Once the pages actually resolve, the gate gets out of the way -- the failure
    mode is refusing forever, not just refusing once publishing has happened."""
    from whale_agent.config import Settings
    from whale_agent.jobs.digest_weekly import run_weekly_digest
    from whale_agent.monitoring.link_check import FetchResult
    from whale_agent.storage.db import Store

    store = Store(":memory:")
    try:
        settings = Settings(email_provider="none")

        def always_reachable(url: str) -> FetchResult:
            return FetchResult(ok=True, status=200, body="<html>ok</html>")

        report, results = run_weekly_digest(
            store,
            settings,
            deliver=True,
            articles_dir=tmp_path,
            article_base_url="https://example.test",
            link_fetcher=always_reachable,
        )
        assert len(results) == 1
        # Gate got out of the way; the only reason the send itself did not go through
        # is the "none" email provider used to keep this test off the network.
        assert results[0].channel != "link-check"
    finally:
        store.close()


def test_international_section_is_hidden_from_the_email_by_default(tmp_path):
    """Foreign filers are out of the email by default. Default is off, so a plain
    run must not show the
    "International plays" section in the email HTML, even though a Japanese event was
    recorded."""
    from whale_agent.config import Settings
    from whale_agent.jobs.digest_weekly import run_weekly_digest
    from whale_agent.storage.db import Store

    store = Store(":memory:")
    try:
        store.upsert_many(
            [
                make_event(
                    filer_name="Nomura Securities",
                    jurisdiction=Jurisdiction.JAPAN,
                    issuer_name="Daikin",
                )
            ]
        )
        settings = Settings(email_provider="none")
        html_out = tmp_path / "weekly.html"
        run_weekly_digest(
            store,
            settings,
            deliver=False,
            articles_dir=tmp_path,
            html_out=str(html_out),
        )
        markup = html_out.read_text(encoding="utf-8")
        assert "International plays" not in markup
    finally:
        store.close()


def test_research_notes_are_absent_from_the_email_by_default(tmp_path):
    """Research notes are out of the email by default. Default is off, so a plain
    run must not show the RESEARCH NOTES
    section or call the model, even when the events would otherwise cluster into one."""
    from whale_agent.config import Settings
    from whale_agent.jobs.digest_weekly import run_weekly_digest
    from whale_agent.storage.db import Store

    store = Store(":memory:")
    try:
        store.upsert_many(
            [
                make_event(filer_name=name, issuer_name="Widget Co")
                for name in ("Alpha Capital", "Beta Partners", "Gamma Advisors")
            ]
        )
        settings = Settings(email_provider="none")
        html_out = tmp_path / "weekly.html"
        run_weekly_digest(
            store,
            settings,
            deliver=False,
            articles_dir=tmp_path,
            html_out=str(html_out),
        )
        markup = html_out.read_text(encoding="utf-8")
        assert "RESEARCH NOTES" not in markup
    finally:
        store.close()


def test_international_section_appears_in_the_email_when_explicitly_enabled(tmp_path):
    """WHALE_INCLUDE_INTERNATIONAL is a clean on/off switch, not a deletion -- flipping
    it back on must restore the section exactly as before."""
    from whale_agent.config import Settings
    from whale_agent.jobs.digest_weekly import run_weekly_digest
    from whale_agent.storage.db import Store

    store = Store(":memory:")
    try:
        store.upsert_many(
            [
                make_event(
                    filer_name="Nomura Securities",
                    jurisdiction=Jurisdiction.JAPAN,
                    issuer_name="Daikin",
                )
            ]
        )
        settings = Settings(email_provider="none", include_international=True)
        html_out = tmp_path / "weekly.html"
        run_weekly_digest(
            store,
            settings,
            deliver=False,
            articles_dir=tmp_path,
            html_out=str(html_out),
        )
        markup = html_out.read_text(encoding="utf-8")
        assert "International plays" in markup
    finally:
        store.close()


def test_dry_run_never_checks_links_or_sends(tmp_path):
    """--dry-run must never publish or send -- so the link gate must never fire either;
    a fetcher that gets called at all fails the test."""
    from whale_agent.config import Settings
    from whale_agent.jobs.digest_weekly import run_weekly_digest
    from whale_agent.storage.db import Store

    store = Store(":memory:")
    try:
        settings = Settings(email_provider="none")

        def exploding_fetch(url: str):
            raise AssertionError("dry-run must never check reachability")

        report, results = run_weekly_digest(
            store,
            settings,
            deliver=False,
            articles_dir=tmp_path,
            article_base_url="https://example.test",
            link_fetcher=exploding_fetch,
        )
        assert results == []
    finally:
        store.close()
