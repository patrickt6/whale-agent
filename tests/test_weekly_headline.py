"""The subject line, in TBPN's register.

The reference set, from docs/example-tbpn-headline.md:

    Leopold Stays in the Game, Big Tech Earnings, OpenAI Slashes GPT-5.6 Prices
    Stripe's $53B PayPal Offer, OpenAI's First Device, TBPN's New Business Ideas

Short clauses, comma-separated, a named subject in each, figures inline, no connectives.
These tests pin the shape and, more importantly, pin that nothing enters the line that
the report cannot re-derive below it.
"""

from __future__ import annotations

from datetime import date

from whale_agent.summarization.weekly_headline import (
    HeadlineFacts,
    build_headline,
    clauses_from,
)

ON = date(2026, 8, 3)


def test_a_full_week_reads_like_a_rundown():
    facts = HeadlineFacts(
        fund_name="Duquesne Family Office",
        fund_form="SCHEDULE 13G",
        fund_count=1,
        cluster_issuer="Five Star Bancorp Inc.",
        cluster_filers=6,
        cluster_usd=6_300_000,
        cleared=109,
    )
    line = build_headline(facts, ON)
    assert line == (
        "Duquesne Discloses a Stake, Six Insiders Put $6M Into Five Star Bancorp, "
        "109 Moves Clear the Bar"
    )


def test_the_fund_filing_leads_because_it_is_the_freshest_fact():
    facts = HeadlineFacts(
        fund_name="Situational Awareness LP",
        fund_form="4",
        fund_count=1,
        cluster_issuer="Beyond Air",
        cluster_filers=3,
    )
    assert build_headline(facts, ON).startswith("Situational Awareness Reports a Trade")


def test_corporate_suffixes_are_dropped():
    facts = HeadlineFacts(cluster_issuer="Genco Shipping & Trading Ltd", cluster_filers=2)
    assert "Ltd" not in build_headline(facts, ON)
    assert "Genco Shipping & Trading" in build_headline(facts, ON)


def test_manager_names_are_shortened_the_way_a_headline_would():
    facts = HeadlineFacts(fund_name="Pershing Square Capital", fund_form="SCHEDULE 13D")
    assert build_headline(facts, ON) == "Pershing Square Discloses a Stake"


def test_small_counts_are_spelled_out():
    facts = HeadlineFacts(cluster_issuer="SmallCo", cluster_filers=6, cluster_usd=0)
    assert "Six Insiders Back SmallCo" in build_headline(facts, ON)


def test_a_large_count_stays_numeric():
    facts = HeadlineFacts(cluster_issuer="SmallCo", cluster_filers=14, cluster_usd=0)
    assert "14 Insiders" in build_headline(facts, ON)


def test_never_more_than_four_clauses():
    facts = HeadlineFacts(
        fund_name="Duquesne",
        fund_form="SCHEDULE 13G",
        fund_count=2,
        cluster_issuer="Five Star Bancorp",
        cluster_filers=6,
        cluster_usd=6_300_000,
        largest_issuer="Navios Maritime",
        largest_usd=846_500_000,
        cleared=109,
    )
    assert build_headline(facts, ON).count(",") <= 3


def test_an_empty_week_falls_back_to_the_dated_form():
    """No facts means no headline. Inventing one is the failure this cannot have."""
    assert build_headline(HeadlineFacts(), ON) == "Whale Weekly, 2026-08-03"


def test_a_single_fact_is_a_valid_headline():
    facts = HeadlineFacts(cleared=109)
    assert build_headline(facts, ON) == "109 Moves Clear the Bar"


def test_no_clause_invents_a_figure_that_was_not_supplied():
    """Every number in the line must be traceable to the facts it was built from."""
    import re

    facts = HeadlineFacts(
        fund_name="Duquesne",
        fund_form="SCHEDULE 13G",
        cluster_issuer="Five Star Bancorp",
        cluster_filers=6,
        cluster_usd=6_300_000,
        cleared=109,
    )
    line = build_headline(facts, ON)
    numbers = set(re.findall(r"\d+", line))
    permitted = {"6", "109"}  # the filer count and the cleared count
    assert numbers <= permitted, numbers - permitted


def test_clauses_are_short_enough_to_survive_an_inbox():
    facts = HeadlineFacts(
        fund_name="Duquesne Family Office",
        fund_form="SCHEDULE 13G",
        cluster_issuer="Five Star Bancorp Inc.",
        cluster_filers=6,
        cluster_usd=6_300_000,
    )
    for clause in clauses_from(facts):
        assert len(clause.split()) <= 8, clause
