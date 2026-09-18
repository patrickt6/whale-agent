"""Composite scoring + tiering tests."""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.models.enums import Tier, TransactionType
from whale_agent.scoring.score import assign_tier, compute_score, rank_events

TODAY = date(2026, 7, 25)
SETTINGS = Settings()


def test_open_market_buy_outranks_scheduled_sale_of_same_size():
    buy = make_event(transaction_type=TransactionType.OPEN_MARKET_BUY, usd_value=10_000_000)
    sale = make_event(transaction_type=TransactionType.SCHEDULED_SALE, usd_value=10_000_000)
    assert compute_score(buy, TODAY) > compute_score(sale, TODAY)


def test_famous_filer_small_opportunistic_beats_routine_giant():
    # $5M first-time buy by a famous CEO in a small co beats a $200M routine rebalance.
    famous = make_event(
        filer_name="Jane Q. Example",
        transaction_type=TransactionType.OPEN_MARKET_BUY,
        usd_value=5_000_000,
        is_first_time_filer=True,
    )
    giant_routine = make_event(
        filer_name="Some Index Fund",
        transaction_type=TransactionType.FUND_ADD_POSITION,
        usd_value=200_000_000,
        is_routine=True,
    )
    assert compute_score(famous, TODAY) > compute_score(giant_routine, TODAY)


def test_routine_penalty_lowers_score():
    opportunistic = make_event(is_routine=False)
    routine = make_event(is_routine=True)
    assert compute_score(routine, TODAY) < compute_score(opportunistic, TODAY)


def test_cluster_buys_boost_score():
    solo = make_event(cluster_size=1)
    cluster = make_event(cluster_size=4)
    assert compute_score(cluster, TODAY) > compute_score(solo, TODAY)


def test_tier1_for_huge_event():
    ev = make_event(usd_value=150_000_000)
    assert assign_tier(ev, SETTINGS) == Tier.INSTANT


def test_tier1_for_first_time_activist_13d():
    ev = make_event(
        transaction_type=TransactionType.ACTIVIST_13D,
        usd_value=20_000_000,
        is_first_time_filer=True,
    )
    assert assign_tier(ev, SETTINGS) == Tier.INSTANT


def test_below_gate_is_tier_below():
    ev = make_event(usd_value=1_000_000)
    assert assign_tier(ev, SETTINGS) == Tier.BELOW


def test_rank_drops_below_gate_and_sorts_desc():
    events = [
        make_event(filer_name="A", usd_value=1_000_000),  # below gate, dropped
        make_event(filer_name="B", usd_value=8_000_000),
        make_event(filer_name="C", usd_value=50_000_000, is_first_time_filer=True),
    ]
    ranked = rank_events(events, SETTINGS, TODAY)
    names = [e.filer_name for e in ranked]
    assert "A" not in names
    assert ranked == sorted(ranked, key=lambda e: e.score, reverse=True)


def test_ranking_is_not_pure_dollar_order():
    small_hot = make_event(
        filer_name="Jane Q. Example",
        usd_value=6_000_000,
        transaction_type=TransactionType.OPEN_MARKET_BUY,
        is_first_time_filer=True,
    )
    big_cold = make_event(
        filer_name="Anon Fund",
        usd_value=120_000_000,
        transaction_type=TransactionType.SCHEDULED_SALE,
    )
    ranked = rank_events([big_cold, small_hot], SETTINGS, TODAY)
    assert ranked[0].filer_name == "Jane Q. Example"


# -- what the model is supposed to surface ---------------------------------------
# These test ORDERING between realistic pairs, not arithmetic. A weight can be retuned
# without breaking them; the product claim they encode cannot.


def test_a_small_cap_buy_outranks_a_mega_cap_buy_of_the_same_dollar_size():
    """The same $6M means more in a $200M company than in a $3T one."""
    small = make_event(filer_name="Unknown CEO", usd_value=6_000_000)
    small.context.market_cap_usd = 200_000_000
    mega = make_event(filer_name="Other CEO", usd_value=6_000_000)
    mega.context.market_cap_usd = 3_000_000_000_000
    assert compute_score(small, TODAY) > compute_score(mega, TODAY)


def test_market_cap_being_unknown_is_not_treated_as_a_penalty():
    """A Taiwanese filing FMP does not cover must not rank below a known mega cap."""
    unknown = make_event(filer_name="Unknown Co", usd_value=6_000_000)
    mega = make_event(filer_name="Mega Co", usd_value=6_000_000)
    mega.context.market_cap_usd = 3_000_000_000_000
    assert compute_score(unknown, TODAY) > compute_score(mega, TODAY)


def test_a_bigger_percent_of_float_outranks_a_bigger_dollar_amount():
    """Percent of float is the better conviction measure, so it beats raw dollars."""
    concentrated = make_event(filer_name="Focused Fund", usd_value=8_000_000)
    concentrated.context.percent_of_float = 6.0
    diffuse = make_event(filer_name="Broad Fund", usd_value=20_000_000)
    diffuse.context.percent_of_float = 0.1
    assert compute_score(concentrated, TODAY) > compute_score(diffuse, TODAY)


def test_a_vanguard_13g_ranks_below_a_comparable_filing_from_an_unknown_fund():
    """An index manager's 13G is a fund-flow mechanic, not a decision."""
    vanguard = make_event(
        filer_name="Vanguard Group Inc",
        transaction_type=TransactionType.PASSIVE_13G,
        usd_value=50_000_000,
    )
    unknown = make_event(
        filer_name="Kestrel Ridge Partners",
        transaction_type=TransactionType.PASSIVE_13G,
        usd_value=50_000_000,
    )
    assert compute_score(vanguard, TODAY) < compute_score(unknown, TODAY)


def test_blackrock_is_demoted_rather_than_promoted_relative_to_baseline():
    """The old registry boosted BlackRock 1.6x; passive scale is not a reason to read on."""
    blackrock = make_event(
        filer_name="BlackRock Inc.",
        transaction_type=TransactionType.PASSIVE_13G,
        usd_value=80_000_000,
    )
    anonymous = make_event(
        filer_name="Anon Capital",
        transaction_type=TransactionType.PASSIVE_13G,
        usd_value=80_000_000,
    )
    assert compute_score(blackrock, TODAY) < compute_score(anonymous, TODAY)


def test_fame_alone_no_longer_beats_a_better_situation():
    """Berkshire doing something ordinary must not outrank an unknown doing something real."""
    berkshire = make_event(filer_name="Berkshire Hathaway Inc", usd_value=20_000_000)
    berkshire.context.market_cap_usd = 400_000_000_000
    unknown_ceo = make_event(filer_name="J. Okonkwo", usd_value=6_000_000)
    unknown_ceo.context.market_cap_usd = 180_000_000
    unknown_ceo.context.percent_of_float = 3.0
    assert compute_score(unknown_ceo, TODAY) > compute_score(berkshire, TODAY)


def test_a_filers_first_appearance_in_18_months_outranks_their_routine_twentieth():
    """Breaking a long silence is a decision; the twentieth filing is a subscription."""
    returning = make_event(filer_name="Dormant Fund", usd_value=10_000_000)
    returning.context.filer_prior_filings = 1
    returning.context.filer_days_since_last = 600
    returning.context.filer_typical_usd = 10_000_000
    routine = make_event(filer_name="Frequent Fund", usd_value=10_000_000)
    routine.context.filer_prior_filings = 20
    routine.context.filer_days_since_last = 14
    routine.context.filer_typical_usd = 10_000_000
    assert compute_score(returning, TODAY) > compute_score(routine, TODAY)


def test_a_position_far_above_a_filers_own_median_outranks_their_usual_size():
    """Unusual-for-this-filer is measured against their own history, not the market's."""
    outsized = make_event(filer_name="Steady Fund", usd_value=50_000_000)
    outsized.context.filer_prior_filings = 12
    outsized.context.filer_typical_usd = 5_000_000
    usual = make_event(filer_name="Steady Fund", usd_value=50_000_000)
    usual.context.filer_prior_filings = 12
    usual.context.filer_typical_usd = 50_000_000
    assert compute_score(outsized, TODAY) > compute_score(usual, TODAY)


def test_an_unfamiliar_filer_outranks_one_we_see_constantly():
    """Unfamiliarity is measured from our own store, not looked up in a registry."""
    unfamiliar = make_event(filer_name="Never Seen LP", usd_value=12_000_000)
    unfamiliar.context.filer_prior_filings = 0
    familiar = make_event(filer_name="Weekly Filer LP", usd_value=12_000_000)
    familiar.context.filer_prior_filings = 40
    assert compute_score(unfamiliar, TODAY) > compute_score(familiar, TODAY)


def test_scoring_an_event_with_a_completely_empty_context_still_works():
    """Every non-US event starts here; an unenriched event must score, not crash."""
    bare = make_event(filer_name="Taiwan Filer", usd_value=9_000_000)
    assert bare.context.market_cap_usd is None
    score = compute_score(bare, TODAY)
    assert score > 0


def test_none_context_fields_are_neutral_not_zero():
    """A missing field must leave the score exactly where a neutral multiplier would."""
    bare = make_event(filer_name="Neutral Filer", usd_value=9_000_000)
    filled = make_event(filer_name="Neutral Filer", usd_value=9_000_000)
    filled.context.market_cap_usd = 50_000_000_000  # the large-cap band, multiplier 1.0
    assert compute_score(bare, TODAY) == compute_score(filled, TODAY)


def test_the_first_time_bonus_is_vetoed_by_contradicting_history():
    """A cold-start flag cannot survive a store that has actually seen the filer."""
    flagged = make_event(filer_name="Claimed Debut", is_first_time_filer=True)
    flagged.context.filer_prior_filings = 7
    honest = make_event(filer_name="Claimed Debut", is_first_time_filer=False)
    honest.context.filer_prior_filings = 7
    assert compute_score(flagged, TODAY) == compute_score(honest, TODAY)


def test_ranking_a_realistic_mixed_set_leads_with_the_small_cap_not_the_index_fund():
    """End to end: the digest's top row should be the decision, not the largest number."""
    ceo_buy = make_event(
        filer_name="A. Rivera",
        usd_value=6_000_000,
        transaction_type=TransactionType.OPEN_MARKET_BUY,
    )
    ceo_buy.context.market_cap_usd = 210_000_000
    ceo_buy.context.percent_of_float = 3.2
    ceo_buy.context.filer_prior_filings = 0

    vanguard = make_event(
        filer_name="Vanguard Group Inc",
        usd_value=400_000_000,
        transaction_type=TransactionType.PASSIVE_13G,
    )
    vanguard.context.market_cap_usd = 900_000_000_000
    vanguard.context.filer_prior_filings = 130

    ranked = rank_events([vanguard, ceo_buy], SETTINGS, TODAY)
    assert ranked[0].filer_name == "A. Rivera"
    assert ranked[-1].filer_name == "Vanguard Group Inc"


def test_an_uncorroborated_first_time_flag_earns_no_bonus():
    """Cold start: the flag alone cannot inflate a score, only measured history can."""
    claimed = make_event(filer_name="Cold Start", is_first_time_filer=True)
    plain = make_event(filer_name="Cold Start", is_first_time_filer=False)
    assert compute_score(claimed, TODAY) == compute_score(plain, TODAY)

    corroborated = make_event(filer_name="Cold Start", is_first_time_filer=True)
    corroborated.context.filer_prior_filings = 0
    assert compute_score(corroborated, TODAY) > compute_score(plain, TODAY)


# -- plausibility: figures that are traceable and still false --------------------
# Sixteen days of live FMP data produced a $1.6 quadrillion "purchase" because the
# vendor's price field sometimes holds a transaction total instead of a unit price.
# The provenance gate passes such a figure -- it does trace to a filing field.


def test_a_transaction_total_in_the_price_field_is_rejected():
    """The observed FMP failure: shares x total-value, giving an absurd position."""
    from whale_agent.enrichment.plausibility import check_event

    verdict = check_event(
        make_event(share_count=3_523.0, price_used=2_110_482.0, usd_value=7_435_228_086.0)
    )
    assert not verdict
    assert any("transaction total" in r for r in verdict.reasons)


def test_a_position_larger_than_the_company_is_rejected():
    """You cannot buy more of a company than exists; this needs market-cap enrichment."""
    from whale_agent.enrichment.plausibility import check_event
    from whale_agent.models.context import EventContext

    verdict = check_event(
        make_event(
            usd_value=5_000_000_000.0, context=EventContext(market_cap_usd=94_300_000.0)
        )
    )
    assert not verdict
    assert any("market capitalisation" in r for r in verdict.reasons)


def test_a_position_near_the_whole_company_is_allowed():
    """A take-private really can approach 100%; the check must not reject those."""
    from whale_agent.enrichment.plausibility import check_event
    from whale_agent.models.context import EventContext

    assert check_event(
        make_event(usd_value=90_000_000.0, context=EventContext(market_cap_usd=94_300_000.0))
    )


def test_placeholder_issuer_names_are_rejected():
    from whale_agent.enrichment.plausibility import check_event

    assert not check_event(make_event(issuer_name="NONE"))


def test_ordinary_events_survive_screening():
    """The screen must not cost us real filings."""
    from whale_agent.enrichment.plausibility import screen

    kept, rejected = screen(
        [make_event(share_count=65_000.0, price_used=83.05, usd_value=5_398_250.0)]
    )
    assert len(kept) == 1 and not rejected


def test_zero_and_negative_inputs_are_rejected():
    from whale_agent.enrichment.plausibility import check_event

    assert not check_event(make_event(share_count=0.0))
    assert not check_event(make_event(price_used=-1.0))


def test_a_filer_who_trades_the_same_month_every_year_is_marked_routine():
    """The plan's version of this test asserted `or True` and could not fail.

    `classify_routine` takes a filer's dates and returns a bool; it does not take or
    return events. `mark_routine` is the piece that was missing.
    """
    from datetime import date as _d

    from whale_agent.enrichment.routine_classifier import mark_routine

    class _Store:
        def __init__(self, dates):
            self._dates = dates

        def filer_observations(self, key, before=None):
            return [{"disclosure_date": d, "usd_value": 1} for d in self._dates]

    def _ev():
        # A fresh event each time: `mark_routine` mutates in place, like its neighbours.
        return make_event(filer_name="Calendar Carl", disclosure_date=_d(2026, 3, 12))

    every_march = [_d(y, 3, 12) for y in (2022, 2023, 2024, 2025)]
    assert mark_routine(_Store(every_march), [_ev()], _d(2026, 3, 12))[0].is_routine

    scattered = [_d(2022, 3, 12), _d(2023, 7, 1), _d(2024, 11, 4)]
    assert not mark_routine(_Store(scattered), [_ev()], _d(2026, 3, 12))[0].is_routine


def test_the_evidence_block_matches_whether_the_classifier_runs():
    """The copy and the pipeline must not disagree again."""
    import inspect

    from whale_agent.jobs import pipeline
    from whale_agent.jobs.digest_weekly import HOW_TO_READ

    wired = "mark_routine" in inspect.getsource(pipeline)
    claims = "not yet" not in HOW_TO_READ
    assert wired == claims, (
        "HOW_TO_READ says the split is applied but nothing applies it, or the reverse"
    )
