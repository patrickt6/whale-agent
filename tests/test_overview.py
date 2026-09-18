"""The two-week overview table: one glance at every category the product covers."""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.jobs.overview import OVERVIEW_SECTIONS, build_overview
from whale_agent.models.enums import FilerType, Jurisdiction, TransactionType


def _sections(events, on=date(2026, 7, 26)):
    return {s.key: s for s in build_overview(events, on)}


def test_every_section_is_present_even_when_it_has_nothing():
    """An empty section is a finding, not a gap to hide.

    A silently omitted section reads as "nothing happened there". A section shown empty
    reads as "we looked" -- and for three of these four, empty is the honest current
    state, so the reader has to be able to tell the difference.
    """
    sections = _sections([])
    assert [s for s in sections] == [key for key, _ in OVERVIEW_SECTIONS]
    assert all(s.rows == [] for s in sections.values())


def test_insider_section_takes_insider_filings():
    events = [
        make_event(
            filer_name="A",
            issuer_name="Co",
            usd_value=9_000_000.0,
            filer_type=FilerType.INSIDER,
            transaction_type=TransactionType.OPEN_MARKET_BUY,
        ),
    ]
    assert len(_sections(events)["insider"].rows) == 1


def test_congressional_section_takes_congressional_disclosures_whatever_their_size():
    """STOCK Act amounts are lower bounds of a range and almost never clear $5M.

    Gating this section at $5M would empty it permanently and tell the reader Congress
    stopped trading, which is false. The section is size-ranked, not size-gated, and the
    figures carry their estimate flag through to the render.
    """
    events = [
        make_event(
            filer_name="Rep. X",
            issuer_name="Co",
            usd_value=15_000.0,
            source="fmp_congress",
            usd_value_is_estimate=True,
        ),
    ]
    rows = _sections(events)["congressional"].rows
    assert len(rows) == 1
    assert rows[0].is_estimate


def test_institutional_section_takes_stake_and_fund_filings_not_insider_buys():
    events = [
        make_event(
            filer_name="Fund",
            issuer_name="Co",
            usd_value=50_000_000.0,
            filer_type=FilerType.FUND,
            transaction_type=TransactionType.ACTIVIST_13D,
        ),
        make_event(
            filer_name="Person",
            issuer_name="Co",
            usd_value=9_000_000.0,
            filer_type=FilerType.INSIDER,
            transaction_type=TransactionType.OPEN_MARKET_BUY,
        ),
    ]
    sections = _sections(events)
    assert len(sections["institutional"].rows) == 1
    assert sections["institutional"].rows[0].filer == "Fund"


def test_international_section_is_keyed_on_jurisdiction_not_on_source():
    """A US vendor reporting a Taiwanese filing is still an international play."""
    events = [
        make_event(
            filer_name="A",
            issuer_name="TW Co",
            usd_value=9_000_000.0,
            jurisdiction=Jurisdiction.TAIWAN,
        ),
        make_event(
            filer_name="B",
            issuer_name="US Co",
            usd_value=9_000_000.0,
            jurisdiction=Jurisdiction.US,
        ),
    ]
    rows = _sections(events)["international"].rows
    assert len(rows) == 1
    assert rows[0].issuer == "TW Co"


def test_an_event_is_never_counted_in_two_sections():
    """Double-counting inflates the week. Each row belongs to exactly one section."""
    events = [
        make_event(
            filer_name="Fund",
            issuer_name="TW Co",
            usd_value=50_000_000.0,
            filer_type=FilerType.FUND,
            jurisdiction=Jurisdiction.TAIWAN,
            transaction_type=TransactionType.ACTIVIST_13D,
        ),
    ]
    placed = sum(len(s.rows) for s in build_overview(events, date(2026, 7, 26)))
    assert placed == 1


def test_rows_are_ranked_by_size_within_a_section():
    events = [
        make_event(filer_name="small", issuer_name="A", usd_value=6_000_000.0),
        make_event(filer_name="big", issuer_name="B", usd_value=90_000_000.0),
    ]
    rows = _sections(events)["insider"].rows
    assert [r.filer for r in rows] == ["big", "small"]


def test_unpriced_rows_sort_last_and_never_read_as_zero():
    events = [
        make_event(filer_name="unpriced", issuer_name="A", usd_value=None),
        make_event(filer_name="priced", issuer_name="B", usd_value=6_000_000.0),
    ]
    rows = _sections(events)["insider"].rows
    assert [r.filer for r in rows] == ["priced", "unpriced"]
    assert rows[1].amount_label == "not disclosed"


def test_section_totals_only_add_the_rows_that_have_a_figure():
    events = [
        make_event(filer_name="a", issuer_name="A", usd_value=6_000_000.0),
        make_event(filer_name="b", issuer_name="B", usd_value=None),
    ]
    section = _sections(events)["insider"]
    assert section.total_usd == 6_000_000.0
    assert section.priced_count == 1
    assert section.count == 2
