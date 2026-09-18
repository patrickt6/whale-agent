"""The profile filter stage: deterministic, order-preserving, and a no-op by default."""

from __future__ import annotations

from dataclasses import replace

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.models.enums import Jurisdiction, TransactionType
from whale_agent.scoring.filters import apply_filters


def test_empty_filters_are_a_no_op():
    events = [make_event(filer_name="A"), make_event(filer_name="B")]
    assert apply_filters(events, Settings()) == events


def test_jurisdiction_filter():
    us = make_event(filer_name="A", jurisdiction=Jurisdiction.US)
    tw = make_event(filer_name="B", jurisdiction=Jurisdiction.TAIWAN)
    settings = replace(Settings(), filter_jurisdictions=["US"])
    assert apply_filters([us, tw], settings) == [us]


def test_jurisdiction_filter_is_case_insensitive():
    us = make_event(jurisdiction=Jurisdiction.US)
    settings = replace(Settings(), filter_jurisdictions=["us"])
    assert apply_filters([us], settings) == [us]


def test_event_type_filter():
    buy = make_event(filer_name="A", transaction_type=TransactionType.OPEN_MARKET_BUY)
    sell = make_event(filer_name="B", transaction_type=TransactionType.OPEN_MARKET_SELL)
    settings = replace(Settings(), filter_event_types=["open_market_buy"])
    assert apply_filters([buy, sell], settings) == [buy]


def test_filer_role_filter_matches_via_role_for(monkeypatch):
    director = make_event(filer_name="A", filer_role="Director")
    unrelated = make_event(filer_name="B", filer_role="Ten Percent Owner")
    settings = replace(Settings(), filter_filer_roles=["director"])
    kept = apply_filters([director, unrelated], settings)
    assert kept == [director]


def test_filer_role_filter_matches_compound_role_by_substring():
    cfo = make_event(filer_name="A", filer_role="Director, Chief Financial Officer")
    settings = replace(Settings(), filter_filer_roles=["director"])
    assert apply_filters([cfo], settings) == [cfo]


def test_exclude_filer_roles_drops_matches():
    owner = make_event(filer_name="A", filer_role="10% Owner")
    director = make_event(filer_name="B", filer_role="Director")
    settings = replace(Settings(), filter_exclude_roles=["10% owner"])
    assert apply_filters([owner, director], settings) == [director]


def test_exclude_filer_roles_keeps_events_with_unknown_role():
    """No role known is not treated as a match for an exclude list -- unknown is not
    known-bad."""
    unknown = make_event(filer_role=None)
    settings = replace(Settings(), filter_exclude_roles=["10% owner"])
    assert apply_filters([unknown], settings) == [unknown]


def test_filters_compose_with_and():
    match = make_event(
        filer_name="A",
        jurisdiction=Jurisdiction.US,
        transaction_type=TransactionType.OPEN_MARKET_BUY,
    )
    wrong_type = make_event(
        filer_name="B",
        jurisdiction=Jurisdiction.US,
        transaction_type=TransactionType.OPEN_MARKET_SELL,
    )
    settings = replace(
        Settings(),
        filter_jurisdictions=["US"],
        filter_event_types=["open_market_buy"],
    )
    assert apply_filters([match, wrong_type], settings) == [match]


def test_filters_preserve_order():
    events = [make_event(filer_name=f"F{i}") for i in range(5)]
    settings = replace(Settings(), filter_jurisdictions=["US"])
    assert apply_filters(events, settings) == events
