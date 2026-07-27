"""Headlines carry the actor, the money and the company (the house voice for headlines)."""

from __future__ import annotations

from tests.conftest import make_event
from whale_agent.analysis.patterns import Pattern
from whale_agent.models.enums import FilerType, TransactionType
from whale_agent.summarization.thesis import headline_for


def _p(kind, subject, events, **kw):
    return Pattern(
        kind=kind,
        subject=subject,
        summary=kw.get("summary", "x"),
        events=events,
        strength=0.9,
        falsifier="f",
        metrics=kw.get("metrics", {}),
    )


def test_cluster_headline_names_actor_amount_and_company():
    evs = [
        make_event(
            filer_name=n,
            issuer_name="Five Star Bancorp",
            ticker="FSBC",
            usd_value=1_050_000.0,
            filer_type=FilerType.INSIDER,
        )
        for n in "ABCDEF"
    ]
    h = headline_for(_p("issuer_cluster", "FSBC", evs))
    assert h == "Six insiders put $6.3M into Five Star Bancorp", h


def test_headline_uses_the_company_name_not_the_bare_ticker():
    evs = [
        make_event(
            filer_name=n,
            issuer_name="Five Star Bancorp",
            ticker="FSBC",
            usd_value=3_000_000.0,
            filer_type=FilerType.INSIDER,
        )
        for n in "AB"
    ]
    assert "Five Star Bancorp" in headline_for(_p("issuer_cluster", "FSBC", evs))
    assert "FSBC" not in headline_for(_p("issuer_cluster", "FSBC", evs))


def test_headline_falls_back_to_ticker_when_the_name_was_never_resolved():
    """FMP returns companyName null often enough that this is the common case."""
    evs = [
        make_event(
            filer_name=n,
            issuer_name="FSBC",
            ticker="FSBC",
            usd_value=3_000_000.0,
            filer_type=FilerType.INSIDER,
        )
        for n in "AB"
    ]
    assert "FSBC" in headline_for(_p("issuer_cluster", "FSBC", evs))


def test_funds_are_called_institutions_not_insiders():
    evs = [
        make_event(
            filer_name=n,
            issuer_name="Gloo Holdings",
            ticker="GLOO",
            usd_value=2_250_000.0,
            filer_type=FilerType.FUND,
            transaction_type=TransactionType.FUND_NEW_POSITION,
        )
        for n in "ABCD"
    ]
    h = headline_for(_p("new_position_wave", "GLOO", evs))
    assert "institutions" in h and "insiders" not in h, h


def test_a_single_filer_is_singular():
    evs = [
        make_event(
            filer_name="A",
            issuer_name="Gloo Holdings",
            ticker="GLOO",
            usd_value=19_000_000.0,
            filer_type=FilerType.FUND,
        )
    ]
    h = headline_for(_p("issuer_cluster", "GLOO", evs))
    assert h.startswith("One institution "), h


def test_an_unpriced_pattern_states_no_dollar_figure():
    """None means not learned. A headline must never imply a total we do not have."""
    evs = [
        make_event(
            filer_name=n,
            issuer_name="Quiet Co",
            ticker="QCO",
            usd_value=None,
            filer_type=FilerType.INSIDER,
        )
        for n in "ABC"
    ]
    h = headline_for(_p("issuer_cluster", "QCO", evs))
    assert "$" not in h and "0" not in h, h
    assert "Quiet Co" in h


def test_every_figure_in_the_headline_survives_the_provenance_gate():
    from whale_agent.summarization.provenance import assert_no_hallucinated_numbers

    evs = [
        make_event(
            filer_name=n,
            issuer_name="Five Star Bancorp",
            ticker="FSBC",
            usd_value=1_050_000.0,
            filer_type=FilerType.INSIDER,
        )
        for n in "ABCDEF"
    ]
    assert_no_hallucinated_numbers(headline_for(_p("issuer_cluster", "FSBC", evs)), evs)
