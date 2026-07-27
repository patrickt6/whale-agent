"""End-to-end: prose in the digest, and the provenance gate as the last line of defence.

`test_llm_prose.py` covers the per-sentence filter. These cover what happens when
something gets past it: the digest falls back to the deterministic render rather than
shipping an unsourced number, and delivery still happens either way.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.jobs.pipeline import render_checked, run_daily_digest
from whale_agent.storage.db import Store
from whale_agent.summarization.prose import DigestProse


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _ranked(store):
    from whale_agent.jobs.pipeline import build_ranked

    events = [make_event(filer_name="Warren Buffett", usd_value=12_400_000.0)]
    return build_ranked(events, store, Settings(), on=date(2026, 7, 25))


def test_prose_replaces_the_template_line(store):
    ranked = _ranked(store)
    prose = DigestProse(
        headline="One notable buy.",
        why_it_matters={ranked[0].event_id: "A recognizable name adding to a position."},
    )
    digest = render_checked(ranked, date(2026, 7, 25), prose)
    assert "One notable buy." in digest
    assert "A recognizable name adding to a position." in digest


def test_unsourced_number_forces_the_deterministic_render(store):
    ranked = _ranked(store)
    prose = DigestProse(headline="The position is now worth $88.8M.")
    digest = render_checked(ranked, date(2026, 7, 25), prose)
    # All prose is dropped, not just the offending line: at that point the model's
    # output is not trustworthy enough to keep any of it.
    assert "88.8M" not in digest
    assert "$12.4M" in digest


def test_routine_events_always_carry_a_caveat_even_without_prose(store):
    events = [make_event(filer_name="Someone", usd_value=12_400_000.0, is_routine=True)]
    from whale_agent.jobs.pipeline import build_ranked

    ranked = build_ranked(events, store, Settings(), on=date(2026, 7, 25))
    digest = render_checked(ranked, date(2026, 7, 25))
    assert "Skeptic's note: this looks like a routine" in digest


def test_digest_ships_when_the_llm_is_off(store):
    digest, results = run_daily_digest(
        store,
        Settings(),  # no LLM key, no email
        on=date(2026, 7, 25),
        events=[make_event(usd_value=12_400_000.0)],
        use_llm=True,  # requested, but unconfigured
        deliver=True,
    )
    assert "WHALE DIGEST" in digest
    assert results[0].ok is False  # email skipped, cleanly


def test_delivery_outcome_is_recorded_for_the_dead_mans_switch(store):
    run_daily_digest(
        store,
        Settings(),
        on=date(2026, 7, 25),
        events=[make_event(usd_value=12_400_000.0)],
        use_llm=False,
        deliver=True,
    )
    # The attempt is logged even though it failed, and it does not clear the switch.
    assert store.digest_sent(date(2026, 7, 25), "daily") is False
