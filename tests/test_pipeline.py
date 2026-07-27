"""End-to-end deterministic pipeline + numeric-provenance gate tests."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import make_event
from whale_agent.jobs.pipeline import run_digest
from whale_agent.models.enums import TransactionType
from whale_agent.scoring.score import rank_events
from whale_agent.storage.db import Store
from whale_agent.summarization.provenance import check_unsourced_numbers
from whale_agent.summarization.render import render_digest

TODAY = date(2026, 7, 25)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_pipeline_produces_digest_and_persists(store):
    events = [
        make_event(
            filer_name="Warren E. Buffett",
            usd_value=8_000_000,
            transaction_type=TransactionType.OPEN_MARKET_BUY,
        ),
        make_event(filer_name="Small Fry", usd_value=1_000_000),  # below gate
    ]
    digest = run_digest(events, store, on=TODAY)
    assert "WHALE DIGEST" in digest
    assert "Warren E. Buffett" in digest
    assert "Small Fry" not in digest  # dropped by gate
    assert store.count() == 2  # both persisted, only one shown


def test_pipeline_is_idempotent(store):
    events = [make_event(filer_name="Repeat Filer", usd_value=8_000_000)]
    run_digest(events, store, on=TODAY)
    run_digest(events, store, on=TODAY)
    assert store.count() == 1


def test_empty_digest_when_nothing_clears_gate(store):
    events = [make_event(usd_value=100_000)]
    digest = run_digest(events, store, on=TODAY)
    assert "No disclosed moves cleared" in digest


def test_provenance_gate_passes_on_deterministic_render():
    events = rank_events([make_event(usd_value=8_000_000)], today=TODAY)
    text = render_digest(events, TODAY)
    assert check_unsourced_numbers(text, events) == []


def test_provenance_gate_catches_injected_hallucination():
    events = rank_events([make_event(usd_value=8_000_000)], today=TODAY)
    text = render_digest(events, TODAY) + "\nBonus: a fabricated $999.9M figure."
    bad = check_unsourced_numbers(text, events)
    assert any("999" in b for b in bad)
