"""Pattern-detection boundary tests: the thresholds are the design, so they are the tests."""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.analysis.patterns import (
    MIN_REPORTABLE_STRENGTH,
    Pattern,
    detect_cross_jurisdiction,
    detect_filer_across_issuers,
    detect_issuer_clusters,
    detect_new_position_wave,
    detect_patterns,
    detect_sector_concentration,
    detect_unusual_concentration,
    filter_weak,
    rank_patterns,
)
from whale_agent.models.context import EventContext
from whale_agent.models.enums import Jurisdiction, TransactionType


def buy(filer: str, issuer: str, **overrides):
    """A gated open-market buy by `filer` into `issuer`, with distinct ids for both."""
    base = dict(
        filer_name=filer,
        filer_id="F-" + filer.lower().replace(" ", "-"),
        issuer_name=issuer,
        issuer_id="I-" + issuer.lower().replace(" ", "-"),
        transaction_type=TransactionType.OPEN_MARKET_BUY,
        usd_value=10_000_000.0,
    )
    base.update(overrides)
    return make_event(**base)


# -- issuer clusters ----------------------------------------------------------------


def test_two_filers_at_one_issuer_is_not_a_cluster():
    """A pair is not a cluster: two insiders released from one blackout are routine."""
    events = [buy("Filer A", "Acme"), buy("Filer B", "Acme")]
    assert detect_issuer_clusters(events) == []


def test_three_filers_at_one_issuer_is_a_cluster():
    """Three independent filers into one name clears the threshold."""
    events = [buy("Filer A", "Acme"), buy("Filer B", "Acme"), buy("Filer C", "Acme")]
    patterns = detect_issuer_clusters(events)
    assert len(patterns) == 1
    assert patterns[0].kind == "issuer_cluster"
    assert patterns[0].metrics["distinct_filers"] == 3


def test_one_filer_filing_twice_is_not_a_cluster_of_two():
    """Filings are counted per distinct filer, so one person cannot be a crowd."""
    events = [
        buy("Filer A", "Acme", transaction_date=date(2026, 7, 20)),
        buy("Filer A", "Acme", transaction_date=date(2026, 7, 22)),
        buy("Filer A", "Acme", transaction_date=date(2026, 7, 23)),
    ]
    assert detect_issuer_clusters(events) == []


def test_compensation_grants_do_not_form_a_cluster():
    """Only buy-side events accumulate; a payroll calendar is not a thesis."""
    events = [
        buy("Filer A", "Acme", transaction_type=TransactionType.GRANT),
        buy("Filer B", "Acme", transaction_type=TransactionType.GRANT),
        buy("Filer C", "Acme", transaction_type=TransactionType.GRANT),
    ]
    assert detect_issuer_clusters(events) == []


def test_cluster_summary_reports_only_computed_figures():
    """The summary states counts and a total both derivable from the attached events."""
    events = [buy("Filer A", "Acme"), buy("Filer B", "Acme"), buy("Filer C", "Acme")]
    summary = detect_issuer_clusters(events)[0].summary
    assert "3 distinct filers" in summary
    assert "$30.0M" in summary


# -- sector concentration -----------------------------------------------------------


def test_sector_pattern_needs_distinct_issuers_not_repeat_filings():
    """Three filings into the same company are one company, not a sector rotation."""
    events = [
        buy("Filer A", "Acme", context=EventContext(sector="Energy")),
        buy("Filer B", "Acme", context=EventContext(sector="Energy")),
        buy("Filer C", "Acme", context=EventContext(sector="Energy")),
    ]
    assert detect_sector_concentration(events) == []


def test_three_distinct_issuers_in_one_sector_is_a_sector_pattern():
    """Distinct issuers in one sector clear the threshold."""
    events = [
        buy("Filer A", "Acme", context=EventContext(sector="Energy")),
        buy("Filer B", "Borax", context=EventContext(sector="Energy")),
        buy("Filer C", "Cedar", context=EventContext(sector="Energy")),
    ]
    patterns = detect_sector_concentration(events)
    assert len(patterns) == 1
    assert patterns[0].subject == "Energy"


def test_events_without_a_known_sector_are_skipped_not_grouped():
    """`sector=None` means unknown, so those rows form no group of their own."""
    events = [buy(f"Filer {i}", f"Co {i}") for i in range(5)]
    assert all(e.context.sector is None for e in events)
    assert detect_sector_concentration(events) == []


def test_unknown_sector_events_do_not_pad_a_real_sector_group():
    """A real sector group counts only the issuers actually known to be in it."""
    events = [
        buy("Filer A", "Acme", context=EventContext(sector="Energy")),
        buy("Filer B", "Borax", context=EventContext(sector="Energy")),
        buy("Filer C", "Cedar"),
    ]
    assert detect_sector_concentration(events) == []


# -- filer across issuers -----------------------------------------------------------


def test_one_filer_in_two_companies_is_not_a_deployment():
    """Two names is ordinary portfolio maintenance."""
    events = [buy("Big Fund", "Acme"), buy("Big Fund", "Borax")]
    assert detect_filer_across_issuers(events) == []


def test_one_filer_in_three_companies_is_reported():
    """Three simultaneous names clears the threshold."""
    events = [buy("Big Fund", n) for n in ("Acme", "Borax", "Cedar")]
    patterns = detect_filer_across_issuers(events)
    assert len(patterns) == 1
    assert patterns[0].metrics["distinct_issuers"] == 3


# -- cross jurisdiction -------------------------------------------------------------


def test_one_issuer_in_two_jurisdictions_is_cross_jurisdictional():
    """Two separate regimes surfacing the same issuer is the pattern itself."""
    events = [
        buy("Filer A", "Acme", jurisdiction=Jurisdiction.US),
        buy("Filer B", "Acme", jurisdiction=Jurisdiction.JAPAN),
    ]
    patterns = detect_cross_jurisdiction(events)
    assert len(patterns) == 1
    assert patterns[0].metrics["jurisdictions"] == 2


def test_one_issuer_in_a_single_jurisdiction_is_not_cross_jurisdictional():
    """Several filings from one regime are just several filings."""
    events = [buy("Filer A", "Acme"), buy("Filer B", "Acme")]
    assert detect_cross_jurisdiction(events) == []


# -- unusual concentration ----------------------------------------------------------


def test_activity_far_above_an_issuers_own_baseline_is_flagged():
    """Filings well above the issuer's trailing norm are reported."""
    events = [buy(f"Filer {i}", "Acme") for i in range(6)]
    patterns = detect_unusual_concentration(events, baseline=lambda _key: 1.0)
    assert len(patterns) == 1
    assert patterns[0].metrics["ratio"] == 6.0


def test_activity_within_an_issuers_baseline_is_not_flagged():
    """A busy issuer behaving normally is not an anomaly."""
    events = [buy(f"Filer {i}", "Acme") for i in range(4)]
    assert detect_unusual_concentration(events, baseline=lambda _key: 3.0) == []


def test_an_issuer_with_no_baseline_is_never_called_unusual():
    """No history means no claim: unknown must not be read as a norm of zero."""
    events = [buy(f"Filer {i}", "Acme") for i in range(6)]
    assert detect_unusual_concentration(events, baseline=lambda _key: None) == []


def test_a_baseline_too_small_to_trust_is_not_used_as_a_denominator():
    """A near-zero norm makes any ratio enormous, so it is refused rather than divided."""
    events = [buy(f"Filer {i}", "Acme") for i in range(4)]
    assert detect_unusual_concentration(events, baseline=lambda _key: 0.1) == []


# -- new position wave --------------------------------------------------------------


def test_three_institutions_opening_new_positions_is_a_wave():
    """Explicit new-position flags from three distinct filers clear the threshold."""
    events = [
        buy(f"Fund {i}", "Acme", context=EventContext(is_new_position=True)) for i in range(3)
    ]
    patterns = detect_new_position_wave(events)
    assert len(patterns) == 1
    assert patterns[0].metrics["distinct_filers"] == 3


def test_unknown_new_position_status_does_not_count_as_new():
    """`is_new_position=None` is unknown; only an explicit True is an open."""
    events = [buy(f"Fund {i}", "Acme") for i in range(4)]
    assert detect_new_position_wave(events) == []


def test_adds_to_existing_positions_are_not_a_new_position_wave():
    """An explicit False is an add, and adds are continuous and mostly mechanical."""
    events = [
        buy(f"Fund {i}", "Acme", context=EventContext(is_new_position=False)) for i in range(4)
    ]
    assert detect_new_position_wave(events) == []


# -- selection ----------------------------------------------------------------------


def test_an_empty_window_yields_no_patterns():
    """Zero events must return nothing rather than raise."""
    assert detect_patterns([]) == []
    assert detect_issuer_clusters([]) == []
    assert detect_sector_concentration([]) == []
    assert detect_cross_jurisdiction([]) == []
    assert detect_new_position_wave([]) == []
    assert detect_unusual_concentration([], baseline=lambda _key: 1.0) == []


def test_a_better_supported_pattern_of_the_same_kind_ranks_first():
    """Strength grows with evidence beyond the threshold, and ranking follows it."""
    events = [buy(f"Filer {i}", "Acme") for i in range(6)]
    events += [buy(f"Other {i}", "Borax") for i in range(3)]
    ranked = rank_patterns(detect_issuer_clusters(events))
    assert [p.subject for p in ranked] == ["Acme", "Borax"]
    assert ranked[0].strength > ranked[1].strength


def test_ranking_is_stable_for_identical_input():
    """Identical windows must not reorder between runs; the output feeds a prompt."""
    events = [buy(f"Filer {i}", "Acme") for i in range(4)]
    events += [
        buy(f"Fund {i}", "Borax", context=EventContext(is_new_position=True)) for i in range(4)
    ]
    first = [(p.kind, p.subject) for p in detect_patterns(events)]
    second = [(p.kind, p.subject) for p in detect_patterns(events)]
    assert first == second


def test_top_n_truncates_after_ranking():
    """`top_n` keeps the strongest patterns, not the first ones detected."""
    events = [buy(f"Filer {i}", "Acme") for i in range(6)]
    events += [buy(f"Other {i}", "Borax") for i in range(3)]
    top = detect_patterns(events, top_n=1)
    assert len(top) == 1
    assert top[0].subject == "Acme"


def test_weak_patterns_never_reach_the_model():
    """A bare-threshold pass on a loose kind is filtered out before the prose stage."""
    weak = Pattern(
        kind="sector_concentration",
        subject="Energy",
        summary="3 distinct Energy companies drew disclosed purchases in this window.",
        events=[],
        strength=MIN_REPORTABLE_STRENGTH - 0.01,
        falsifier="Sector size.",
    )
    assert filter_weak([weak]) == []


def test_a_bare_threshold_sector_group_is_filtered_from_the_full_run():
    """Exactly three issuers in one sector is too weak to build a thesis on."""
    events = [
        buy("Filer A", "Acme", context=EventContext(sector="Energy")),
        buy("Filer B", "Borax", context=EventContext(sector="Energy")),
        buy("Filer C", "Cedar", context=EventContext(sector="Energy")),
    ]
    assert detect_sector_concentration(events) != []  # detected...
    assert [p.kind for p in detect_patterns(events)] == []  # ...but not reported


def test_every_returned_pattern_carries_a_falsifier():
    """A pattern with no stated innocent explanation must never reach the model."""
    events = [
        buy(f"Filer {i}", "Acme", context=EventContext(sector="Energy", is_new_position=True))
        for i in range(5)
    ]
    events += [
        buy(f"Other {i}", "Borax", context=EventContext(sector="Energy")) for i in range(3)
    ]
    events += [
        buy("Big Fund", n, context=EventContext(sector="Energy"))
        for n in ("Cedar", "Dune", "Elm")
    ]
    events += [buy("Tokyo Filer", "Acme", jurisdiction=Jurisdiction.JAPAN)]
    patterns = detect_patterns(events, baseline=lambda _key: 1.0)
    assert len(patterns) >= 4
    kinds = {p.kind for p in patterns}
    assert "issuer_cluster" in kinds
    for p in patterns:
        assert p.falsifier.strip()
        assert p.events
        assert p.summary.strip()


def test_pattern_events_are_the_rows_the_claim_rests_on():
    """Every pattern exposes the ids of its supporting events for downstream citation."""
    events = [buy(f"Filer {i}", "Acme") for i in range(3)]
    pattern = detect_issuer_clusters(events)[0]
    assert sorted(pattern.event_ids) == sorted(e.event_id for e in events)


# -- the threshold belongs on the group, not the filing ---------------------------
# Measured on 16 days of live filings: gating events at $5M each before detection left
# zero clusters, because a cluster is a count phenomenon and the gate is a dollar filter.


def test_many_tiny_filings_do_not_make_a_pattern():
    """31 filers buying $13K each is not conviction, however many of them there are."""
    from whale_agent.analysis.patterns import detect_patterns

    events = [
        make_event(
            filer_name=f"Filer {i}",
            filer_id=f"f{i}",
            issuer_name="TinyTotal Corp",
            usd_value=13_000.0,
        )
        for i in range(31)
    ]
    assert detect_patterns(events, baseline=lambda k: None) == []


def test_a_few_substantial_filings_do_make_a_pattern():
    """Four filers at $2.3M each clears in aggregate though none clears the $5M gate."""
    from whale_agent.analysis.patterns import detect_patterns

    events = [
        make_event(
            filer_name=f"Filer {i}",
            filer_id=f"f{i}",
            issuer_name="Gloo Holdings",
            usd_value=2_300_000.0,
        )
        for i in range(4)
    ]
    patterns = detect_patterns(events, baseline=lambda k: None)
    assert [p.kind for p in patterns] == ["issuer_cluster"]


def test_aggregate_counts_only_priced_filings():
    """An unpriced row must not be treated as zero when summing a group."""
    from whale_agent.analysis.patterns import aggregate_usd, detect_issuer_clusters

    events = [
        make_event(filer_name="A", filer_id="a", issuer_name="Co", usd_value=6_000_000.0),
        make_event(filer_name="B", filer_id="b", issuer_name="Co", usd_value=None),
        make_event(filer_name="C", filer_id="c", issuer_name="Co", usd_value=None),
    ]
    clusters = detect_issuer_clusters(events)
    assert clusters and aggregate_usd(clusters[0]) == 6_000_000.0


# -- a sector count is shaped by which markets are ingested at all --------------
#
# The pipeline reads US Form 4 insider transactions and Japanese threshold reports.
# Those are different disclosure regimes with different reporting floors, so any sector
# grouping that totals dollars across both is dominated by Japan by construction. The
# pattern must say so itself: prose is optional and the deterministic article ships
# whenever the model is unreachable, which is exactly when a false claim would go out
# unchallenged.


def _sector_event(ticker, issuer, sector, jurisdiction, usd):
    from tests.conftest import make_event
    from whale_agent.models.enums import Jurisdiction

    ev = make_event(
        ticker=ticker,
        issuer_name=issuer,
        issuer_id=ticker,
        filer_name=f"Filer {issuer}",
        usd_value=float(usd),
        jurisdiction=Jurisdiction(jurisdiction),
    )
    ev.context.sector = sector
    return ev


def test_a_one_country_sector_pattern_says_which_country_it_is():
    from whale_agent.analysis.patterns import detect_sector_concentration

    events = [
        _sector_event(f"{i}.T", f"JP Issuer {i}", "Technology", "JP", 50_000_000)
        for i in range(4)
    ]
    patterns = detect_sector_concentration(events)
    assert patterns, "four distinct issuers in one sector should form a pattern"
    text = patterns[0].falsifier
    # The code, not the enum's repr: "Jurisdiction.JAPAN" leaked into reader-facing copy.
    assert "JP" in text
    assert "Jurisdiction." not in text
    assert "coverage" in text.lower()


def test_a_genuinely_mixed_sector_is_not_given_the_coverage_caveat():
    """The caveat has to be earned, or it becomes boilerplate nobody reads."""
    from whale_agent.analysis.patterns import detect_sector_concentration

    events = [
        _sector_event("1.T", "JP One", "Technology", "JP", 50_000_000),
        _sector_event("2.T", "JP Two", "Technology", "JP", 50_000_000),
        _sector_event("AAA", "US One", "Technology", "US", 50_000_000),
        _sector_event("BBB", "US Two", "Technology", "US", 50_000_000),
    ]
    patterns = detect_sector_concentration(events)
    assert patterns
    assert "coverage" not in patterns[0].falsifier.lower()
