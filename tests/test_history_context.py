"""WA-15: per-row history context (prior position, repeat count, first-seen filer)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pytest

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.delivery.email import find_egress_violations
from whale_agent.enrichment.history_context import (
    build_history_context,
    build_history_contexts,
    history_line,
)
from whale_agent.jobs.pipeline import build_digest_html, build_ranked, run_daily_digest
from whale_agent.models.enums import TransactionType
from whale_agent.scoring.score import rank_events
from whale_agent.storage.db import Store
from whale_agent.summarization.provenance import find_unsourced_numbers
from whale_agent.summarization.render import render_digest
from whale_agent.summarization.render_html import html_to_text

ON = date(2026, 7, 25)


@dataclass
class _Holding:
    key: str
    issuer: str
    cusip: str
    option_type: str
    value_usd: int
    shares: int


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "history.db"))
    yield s
    s.close()


def _seed(store: Store, *events):
    store.upsert_many(events)
    store.record_filers(events)


def test_prior_event_found(store):
    old = make_event(
        filer_name="Repeat Buyer",
        share_count=120_000,
        transaction_date=date(2026, 5, 13),
        disclosure_date=date(2026, 5, 14),
    )
    _seed(store, old)
    today = make_event(filer_name="Repeat Buyer", share_count=50_000)
    _seed(store, today)
    ctx = build_history_context(store, today, ON)
    assert ctx.prior_date == date(2026, 5, 14)
    assert ctx.prior_shares == 120_000
    assert ctx.prior_source == "filing"
    assert history_line(ctx).startswith("Earlier filing: 120,000 shares on 2026-05-14.")


def test_prior_13f_found(store):
    ev = make_event(filer_name="Fund", filer_id="0001234567", issuer_name="TestCo")
    store.record_13f_holdings(
        "0001234567",
        date(2026, 3, 31),
        "acc-1",
        "https://www.sec.gov/x",
        [_Holding("CUSIP1", "TESTCO", "CUSIP1", "", 9_000_000, 120_000)],
    )
    _seed(store, ev)
    ctx = build_history_context(store, ev, ON)
    assert ctx.prior_source == "13F"
    assert "Held before: 120,000 shares on 2026-03-31 (13F)." in history_line(ctx)


def test_prior_not_found(store):
    ev = make_event(filer_name="Newcomer")
    _seed(store, ev)
    ctx = build_history_context(store, ev, ON)
    assert not ctx.has_prior
    assert ctx.repeat_count == 0
    assert "First time this filer appears for this issuer" in history_line(ctx)


def test_repeat_count_same_direction_in_90_days(store):
    def buy(d, tt=TransactionType.OPEN_MARKET_BUY, shares=1.0):
        return make_event(
            filer_name="Streak",
            transaction_type=tt,
            transaction_date=d,
            disclosure_date=d,
            share_count=shares,
        )

    _seed(
        store,
        buy(date(2026, 7, 1), shares=1),
        buy(date(2026, 6, 1), shares=2),
        buy(date(2026, 3, 1), shares=3),  # outside 90 days
        buy(date(2026, 7, 10), TransactionType.OPEN_MARKET_SELL, 4),  # other direction
    )
    today = buy(ON, shares=5)
    _seed(store, today)
    ctx = build_history_context(store, today, ON)
    assert ctx.repeat_count == 2
    assert "in the last 90 days: 2." in history_line(ctx)


def test_first_seen_flag(store):
    veteran = make_event(
        filer_name="Veteran",
        transaction_date=date(2026, 1, 1),
        disclosure_date=date(2026, 1, 2),
    )
    _seed(store, veteran)
    new = make_event(filer_name="Debut")
    again = make_event(filer_name="Veteran", share_count=7)
    _seed(store, new, again)
    ctxs = build_history_contexts(store, [new, again], ON)
    assert ctxs[new.event_id].first_seen == ON
    assert ctxs[again.event_id].first_seen is None
    assert "New filer, first seen 2026-07-25." in history_line(ctxs[new.event_id])


def _settings(on: bool) -> Settings:
    return replace(Settings(), email_provider="none", history_context=on, sources=[])


def _history_events():
    return [
        make_event(
            filer_name="Holder",
            usd_value=8_000_000,
            share_count=33_333,
            transaction_date=date(2026, 5, 13),
            disclosure_date=date(2026, 5, 14),
        ),
        make_event(filer_name="Holder", usd_value=12_345_678, share_count=44_444),
    ]


def test_egress_and_provenance_pass_with_history(store):
    old, new = _history_events()
    _seed(store, old)
    digest, _ = run_daily_digest(
        store, _settings(True), on=ON, events=[new], use_llm=False, deliver=False
    )
    assert "History: Earlier filing: 33,333 shares on 2026-05-14." in digest
    ranked = rank_events([store.get(new.event_id)], today=ON)
    history = build_history_contexts(store, ranked, ON)
    html = build_digest_html(ranked, ON, history=history)
    assert "History:" in html
    assert find_unsourced_numbers(digest, ranked, history) == []
    assert find_unsourced_numbers(html_to_text(html), ranked, history) == []
    assert find_egress_violations(digest, html, events=ranked) == []


def test_history_figure_without_allowed_set_is_caught(store):
    old, new = _history_events()
    old = old.model_copy(update={"share_count": 7_654_321})
    _seed(store, old, new)
    ranked = rank_events([new], today=ON)
    history = build_history_contexts(store, ranked, ON)
    text = render_digest(ranked, ON, history=history)
    assert "7,654,321" in text
    assert find_unsourced_numbers(text, ranked) != []
    assert find_unsourced_numbers(text, ranked, history) == []


def test_setting_off_is_byte_identical(tmp_path):
    old, new = _history_events()
    digests = []
    for name, flag in (("off", False), ("baseline", None)):
        s = Store(str(tmp_path / f"{name}.db"))
        _seed(s, old)
        if flag is False:
            digest, _ = run_daily_digest(
                s, _settings(False), on=ON, events=[new], use_llm=False, deliver=False
            )
        else:
            ranked = build_ranked([new], s, _settings(False), on=ON)
            # The pre-feature call shape: no history argument at all.
            digest = render_digest(ranked, ON, None, None)
            assert build_digest_html(ranked, ON) == build_digest_html(ranked, ON, history=None)
        digests.append(digest)
        s.close()
    assert "History:" not in digests[0]
    assert digests[0] == digests[1]


def test_config_switch(monkeypatch):
    assert Settings.from_env().history_context is True
    monkeypatch.setenv("WHALE_HISTORY_CONTEXT", "0")
    assert Settings.from_env().history_context is False
