"""The weekly runs twice per CI job. The model must only be paid for once.

The workflow renders the pages with --dry-run, deploys them, and only then sends the
email, so that no delivered link can point at a page that does not exist. Both steps
call the same job, so before the cache both steps also called the model once per
pattern and the first set of answers was thrown away.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.jobs import digest_weekly as job
from whale_agent.jobs.digest_weekly import (
    load_cached_theses,
    run_weekly_digest,
    save_theses,
    theses_cache_path,
    weekly_theses,
)
from whale_agent.models.enums import TransactionType
from whale_agent.storage.db import Store

ON = date(2026, 7, 25)


def buy(filer: str, issuer: str = "SmallCo"):
    return make_event(
        filer_name=filer,
        filer_id="F-" + filer.lower().replace(" ", "-"),
        issuer_name=issuer,
        issuer_id="I-" + issuer.lower().replace(" ", "-"),
        transaction_type=TransactionType.OPEN_MARKET_BUY,
        usd_value=10_000_000.0,
        transaction_date=ON,
        disclosure_date=ON,
        source_url="https://www.sec.gov/Archives/edgar/data/123/0001.txt",
    )


def a_cluster():
    return [buy(n) for n in ("Alpha Capital", "Beta Partners", "Gamma Advisors")]


class CountingProvider:
    """Stands in for the API so the test can count what a run would have cost."""

    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return "{}"  # empty object: every field falls back to the assembled article


def _articles(tmp_path: Path) -> Path:
    """An articles directory that owns its parent.

    The cache is a sibling of the articles directory, so passing pytest's `tmp_path`
    directly would put it in the shared base that every test's tmp_path descends from,
    and one test's cache would be visible to the next.
    """
    d = tmp_path / "public"
    d.mkdir(exist_ok=True)
    return d


def test_cached_theses_round_trip_through_disk(tmp_path: Path):
    theses = weekly_theses(a_cluster(), ON)
    assert theses
    save_theses(theses_cache_path(_articles(tmp_path), ON), theses)
    assert load_cached_theses(theses_cache_path(_articles(tmp_path), ON)) == theses


def test_no_cache_on_disk_means_generate(tmp_path: Path):
    """A standalone run, or the first run of a week, is unaffected by the cache."""
    assert load_cached_theses(theses_cache_path(_articles(tmp_path), ON)) is None


def test_a_cache_written_for_another_week_is_not_reused(tmp_path: Path):
    save_theses(theses_cache_path(_articles(tmp_path), ON), weekly_theses(a_cluster(), ON))
    assert load_cached_theses(theses_cache_path(_articles(tmp_path), date(2026, 8, 1))) is None


def test_a_corrupt_cache_falls_back_to_generating(tmp_path: Path):
    path = theses_cache_path(_articles(tmp_path), ON)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_cached_theses(path) is None


def _run(store, provider, tmp_path, deliver=False):
    # This module tests the thesis/research-note cache itself, so it opts back into
    # research notes explicitly; the product default is off (see config.py).
    return run_weekly_digest(
        store,
        Settings(email_provider="none", include_research_notes=True),
        on=ON,
        deliver=deliver,
        articles_dir=tmp_path,
    )


def test_the_second_run_of_a_week_calls_no_model_at_all(tmp_path: Path, monkeypatch):
    """One weekly run, one set of LLM calls, however many times the job is invoked."""
    store = Store(":memory:")
    provider = CountingProvider()
    monkeypatch.setattr(job, "get_provider", lambda *a, **k: provider)
    try:
        store.upsert_many(a_cluster())
        _run(store, provider, tmp_path)
        after_render = provider.calls
        assert after_render >= 1, "the render step is the one that pays for the model"
        _run(store, provider, tmp_path)
        assert provider.calls == after_render
    finally:
        store.close()


def test_the_reused_run_still_publishes_the_same_articles(tmp_path: Path, monkeypatch):
    """The send step must still produce the links, only without regenerating them."""
    store = Store(":memory:")
    monkeypatch.setattr(job, "get_provider", lambda *a, **k: CountingProvider())
    try:
        store.upsert_many(a_cluster())
        first, _ = _run(store, None, tmp_path)
        pages = sorted(p.name for p in tmp_path.glob("*.html"))
        second, _ = _run(store, None, tmp_path)
        assert sorted(p.name for p in tmp_path.glob("*.html")) == pages
        assert "RESEARCH NOTES THIS WEEK" in second
        assert first == second
    finally:
        store.close()


# -- a fallback must never be cached ------------------------------------------
#
# `generate_thesis` returns the deterministic article whenever the model is
# unreachable: no key, transport error, spend cap, a reply that fails the provenance
# gate. Caching that result makes the failure sticky. The render step's transient
# outage becomes the send step's permanent one, and the reader gets
# "29 filers put $8.8B into Global Security Experts Inc. names" for the week even
# though the API recovered seconds later.
#
# Observed for real: a render with unanswered prompts cached three fallbacks, the
# prose was written, and the next render reused the cache and shipped the fallbacks.


def _fallback_and_written(tmp_path):
    from whale_agent.models.thesis import Thesis

    fallback = Thesis(
        title="29 filers put $8.8B into Global Security Experts Inc. names",
        lede="42 filings on Technology landed in the same window.",
        claim="That is the whole finding.",
        trigger_summary="42 filings cleared.",
        evidence=["42 filings"],
        counter_evidence=["a count of listings"],
        falsifier="It repeats next week.",
        what_to_watch="Next week's count.",
        event_ids=["a"],
        pattern_kind="sector_concentration",
        slug="tech",
        published_on=date(2026, 8, 4),
        kind="sector_concentration",
    )
    written = fallback.model_copy(
        update={"title": "What a Technology sector count measures, and what it cannot"}
    )
    return fallback, written


def test_a_run_that_produced_only_fallbacks_writes_no_cache(tmp_path):
    from whale_agent.jobs.digest_weekly import (
        load_cached_theses,
        save_theses,
        theses_cache_path,
    )

    fallback, _ = _fallback_and_written(tmp_path)
    path = theses_cache_path(_articles(tmp_path), date(2026, 8, 4))
    save_theses(path, [fallback], model_wrote_prose=False)
    assert load_cached_theses(path) is None, "a fallback-only run must not become sticky"


def test_a_run_with_model_prose_is_cached(tmp_path):
    from whale_agent.jobs.digest_weekly import (
        load_cached_theses,
        save_theses,
        theses_cache_path,
    )

    _, written = _fallback_and_written(tmp_path)
    path = theses_cache_path(_articles(tmp_path), date(2026, 8, 4))
    save_theses(path, [written], model_wrote_prose=True)
    cached = load_cached_theses(path)
    assert cached is not None
    assert cached[0].title == "What a Technology sector count measures, and what it cannot"


def test_the_cache_is_not_written_inside_the_published_assets_directory(tmp_path):
    """Wrangler uploads dotfiles. Verified: after a deploy the cache was fetchable at
    https://whale-weekly.../.theses-2026-08-04.json with HTTP 200.

    The content is the same prose as the published articles, so nothing secret escaped,
    but an internal cache has no business on the public site, and `.assetsignore` cannot
    fix it here because the assets directory is generated and gitignored, so CI would
    never see the ignore file.
    """
    from whale_agent.jobs.digest_weekly import theses_cache_path

    articles = tmp_path / "public"
    articles.mkdir()
    path = theses_cache_path(articles, date(2026, 8, 4))
    assert articles not in path.parents, f"cache would be published: {path}"
