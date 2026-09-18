"""Macro backdrop: dated, labelled honestly, and only linked to a narrative when it fits."""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.enrichment.macro import (
    BANK_CREDIT,
    POLICY_RATE,
    TEN_YEAR_YIELD,
    MacroObservation,
)
from whale_agent.summarization.macro_context import (
    backdrop_lines,
    macro_note_for,
    sectors_in,
)


def _obs(series, value, as_of, proxy=False, units="percent", source="treasury"):
    return MacroObservation(
        series=series, value=value, as_of=as_of, units=units, source=source, is_proxy=proxy
    )


def test_every_figure_is_printed_with_the_date_it_is_for():
    """A six week old figure printed as current is a wrong number in a checkable document."""
    snapshot = {POLICY_RATE: _obs(POLICY_RATE, 3.706, date(2026, 6, 30))}
    line = backdrop_lines(snapshot, date(2026, 7, 26))[0]
    assert "2026-06-30" in line
    assert "3.7" in line


def test_a_proxy_series_says_that_it_is_a_proxy():
    """Treasury bill average interest is not the fed funds target and must not read as it."""
    snapshot = {POLICY_RATE: _obs(POLICY_RATE, 3.706, date(2026, 6, 30), proxy=True)}
    line = backdrop_lines(snapshot, date(2026, 7, 26))[0]
    assert "proxy" in line.lower()
    assert "fed funds target" not in line.lower()


def test_a_stale_observation_is_flagged_as_stale():
    snapshot = {TEN_YEAR_YIELD: _obs(TEN_YEAR_YIELD, 3.28, date(2026, 4, 1))}
    line = backdrop_lines(snapshot, date(2026, 7, 26))[0]
    assert "days old" in line


def test_an_empty_snapshot_produces_no_lines_rather_than_placeholders():
    """Unknown is absent. It is never a zero and never a stale figure from last run."""
    assert backdrop_lines({}, date(2026, 7, 26)) == []


def test_no_em_dashes_anywhere_in_the_backdrop():
    snapshot = {
        POLICY_RATE: _obs(POLICY_RATE, 3.706, date(2026, 6, 30), proxy=True),
        BANK_CREDIT: _obs(BANK_CREDIT, 12345.0, date(2026, 7, 1), units="USD billions"),
    }
    text = " ".join(backdrop_lines(snapshot, date(2026, 7, 26)))
    assert "—" not in text and "–" not in text


def test_sectors_are_read_off_the_events_and_unknown_is_not_a_sector():
    events = [
        make_event(filer_name="a", context={"sector": "Financial Services"}),
        make_event(filer_name="b", context={"sector": None}),
        make_event(filer_name="c"),
    ]
    assert sectors_in(events) == {"Financial Services"}


def test_bank_credit_is_keyed_to_a_financials_cluster():
    events = [make_event(filer_name="a", context={"sector": "Financial Services"})]
    snapshot = {
        BANK_CREDIT: _obs(BANK_CREDIT, 12345.0, date(2026, 7, 1), units="USD billions")
    }
    note = macro_note_for(events, snapshot, date(2026, 7, 26))
    assert note and "credit" in note.lower()


def test_bank_credit_is_not_keyed_to_an_unrelated_sector():
    """The link has to be defensible. Bank credit says nothing about a biotech cluster."""
    events = [make_event(filer_name="a", context={"sector": "Healthcare"})]
    snapshot = {
        BANK_CREDIT: _obs(BANK_CREDIT, 12345.0, date(2026, 7, 1), units="USD billions")
    }
    assert macro_note_for(events, snapshot, date(2026, 7, 26)) is None


def test_no_sector_means_no_sector_claim():
    """3% of stored rows carry a sector. Silence beats a guess."""
    events = [make_event(filer_name="a")]
    snapshot = {
        BANK_CREDIT: _obs(BANK_CREDIT, 12345.0, date(2026, 7, 1), units="USD billions")
    }
    assert macro_note_for(events, snapshot, date(2026, 7, 26)) is None


def test_the_note_never_asserts_causation():
    events = [make_event(filer_name="a", context={"sector": "Financial Services"})]
    snapshot = {
        BANK_CREDIT: _obs(BANK_CREDIT, 12345.0, date(2026, 7, 1), units="USD billions")
    }
    note = macro_note_for(events, snapshot, date(2026, 7, 26)).lower()
    for banned in ("because", "caused", "driven by", "explains", "due to"):
        assert banned not in note


def test_a_thesis_gets_the_backdrop_only_when_its_sector_supports_it():
    """The article wiring, not just the helper: a note about banks may carry bank credit."""
    from datetime import date as _d

    from whale_agent.jobs.digest_weekly import attach_macro_notes
    from whale_agent.models.thesis import Citation, Thesis

    bank = make_event(filer_name="a", context={"sector": "Financial Services"})
    bio = make_event(filer_name="b", context={"sector": "Healthcare"})
    snapshot = {BANK_CREDIT: _obs(BANK_CREDIT, 12345.0, _d(2026, 7, 1), units="USD billions")}

    def _thesis(slug, events):
        return Thesis(
            slug=slug,
            title="t",
            published_on=_d(2026, 7, 26),
            kind="pattern",
            lede="l",
            claim="c",
            trigger_summary="s",
            evidence=["e"],
            falsifier="f",
            citations=[Citation(label="x", source="y")],
            event_ids=[e.event_id for e in events],
        )

    out = attach_macro_notes(
        [_thesis("bank", [bank]), _thesis("bio", [bio])],
        [bank, bio],
        snapshot,
        _d(2026, 7, 26),
    )
    assert any("credit" in n.lower() for n in out[0].context_notes)
    assert not any("credit" in n.lower() for n in out[1].context_notes)
