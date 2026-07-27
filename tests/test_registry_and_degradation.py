"""Source registry and graceful degradation.

The claim under test: one broken or unconfigured adapter must not
block the digest, and the reader must be told which sources are missing rather than
being left to assume a quiet day.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.errors import NotConfiguredError, SourceUnavailableError
from whale_agent.ingestion.registry import SourceSpec, build_sources, enabled_sources
from whale_agent.jobs.pipeline import collect_events, run_daily_digest
from whale_agent.storage.db import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _spec(name, run, enabled=True, reason=""):
    return SourceSpec(name, name.replace("_", " ").title(), enabled, run, reason)


def test_free_sources_are_enabled_with_no_credentials_at_all():
    specs = {s.name: s for s in build_sources(Settings())}
    assert specs["sec_form4"].enabled is True
    assert specs["taiwan_mops"].enabled is True
    assert specs["dataroma"].enabled is True
    # Paid and key-gated sources stay off, with a reason a human can act on.
    assert specs["fmp_insider"].enabled is False
    assert "FMP_API_KEY" in specs["fmp_insider"].disabled_reason
    assert specs["quiver_congress"].enabled is False
    assert specs["japan_edinet"].enabled is False


def test_vendors_turn_on_when_keys_are_present():
    settings = Settings(fmp_api_key="k", quiver_api_key="k", japan_edinet_api_key="k")
    specs = {s.name: s for s in build_sources(settings)}
    assert specs["fmp_insider"].enabled is True
    assert specs["quiver_congress"].enabled is True
    assert specs["japan_edinet"].enabled is True


def test_explicit_off_switch_beats_a_present_key():
    settings = Settings(fmp_api_key="k", enable_fmp=False)
    specs = {s.name: s for s in build_sources(settings)}
    assert specs["fmp_insider"].enabled is False
    assert "WHALE_ENABLE_FMP" in specs["fmp_insider"].disabled_reason


def test_sources_allowlist_narrows_the_run():
    settings = Settings(sources=["sec_form4"])
    enabled = {s.name for s in enabled_sources(build_sources(settings))}
    assert enabled == {"sec_form4"}


def test_a_crashing_source_does_not_stop_the_others(store):
    good = make_event(filer_name="Good Source Filer")

    def explode():
        raise SourceUnavailableError("HTTP 503 from vendor")

    result = collect_events(
        [_spec("broken", explode), _spec("working", lambda: [good])], store
    )
    assert result.events == [good]
    assert "broken" in result.failures
    assert any("Broken unavailable today" in note for note in result.coverage_notes)


def test_unconfigured_source_is_reported_as_off_not_broken(store):
    def missing_key():
        raise NotConfiguredError("Settings.fmp_api_key is not set")

    result = collect_events([_spec("fmp", missing_key)], store)
    assert result.failures == {}  # not a failure: it is switched off
    assert any("not configured" in note for note in result.coverage_notes)


def test_disabled_source_is_named_in_the_coverage_notes(store):
    result = collect_events(
        [
            _spec(
                "japan_edinet",
                lambda: [],
                enabled=False,
                reason="JAPAN_EDINET_API_KEY not set",
            )
        ]
    )
    assert result.events == []
    assert "JAPAN_EDINET_API_KEY not set" in result.coverage_notes[0]


def test_source_run_outcomes_are_recorded_for_the_watchdog(store):
    def explode():
        raise SourceUnavailableError("boom")

    collect_events([_spec("ok_source", lambda: []), _spec("bad_source", explode)], store)
    runs = store.source_runs()
    assert runs["ok_source"]["last_success_at"] is not None
    assert runs["bad_source"]["last_success_at"] is None
    assert "boom" in runs["bad_source"]["last_error"]


def test_coverage_notes_reach_the_rendered_digest(store):
    def explode():
        raise SourceUnavailableError("down")

    digest, _ = run_daily_digest(
        store,
        Settings(),
        on=date(2026, 7, 25),
        specs=[_spec("japan_edinet", explode), _spec("us", lambda: [make_event()])],
        deliver=False,
        use_llm=False,
    )
    assert "Coverage note:" in digest
    assert "unavailable today" in digest


def test_digest_still_renders_when_every_source_fails(store):
    def explode():
        raise SourceUnavailableError("all down")

    digest, _ = run_daily_digest(
        store,
        Settings(),
        on=date(2026, 7, 25),
        specs=[_spec("a", explode), _spec("b", explode)],
        deliver=False,
        use_llm=False,
    )
    assert "No disclosed moves cleared the $5M threshold today." in digest
    assert "Coverage note:" in digest
