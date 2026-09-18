"""The thesis prompt is almost entirely its filings array, and it is paid for per run.

Measured on a 43 row sector pattern before this cap existed: 33.7KB of user prompt, of
which 31.1KB (92%) was `supporting_filings`. The finding itself and its aggregates are
under 1KB together, so the array is the only thing worth cutting.

What must survive the cut is the model's ability to cite a specific row. The written
notes reference the largest position, a filer that appears several times, and the single
US row in an otherwise Japanese set, and that last one is the trap: it is usually also
the *smallest* row by disclosed value, so a plain top-N by size drops exactly the row
the note was built around.
"""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.analysis.patterns import Pattern
from whale_agent.models.context import EventContext
from whale_agent.models.enums import Jurisdiction, TransactionType
from whale_agent.summarization.thesis_prompts import (
    MAX_SUPPORTING_FILINGS,
    build_user_prompt,
    pattern_payload,
)

ON = date(2026, 7, 25)


def row(i: int, **overrides):
    base = dict(
        filer_name=f"Filer {i}",
        filer_id=f"F{i:03d}",
        issuer_name=f"Issuer {i}",
        issuer_id=f"I{i:03d}",
        jurisdiction=Jurisdiction.JAPAN,
        transaction_type=TransactionType.PASSIVE_13G,
        usd_value=float(1_000_000 * (i + 1)),
        percent_of_company=5.0 + i / 10,
        context=EventContext(market_cap_usd=4.1e8, percent_of_float=12.5, sector="Technology"),
        source_url="https://disclosure2.edinet-fsa.go.jp/x/abc",
    )
    base.update(overrides)
    return make_event(**base)


def a_wide_pattern(n: int = 43) -> Pattern:
    """A week-sized sector pattern: many Japanese rows and one small US one."""
    events = [row(i) for i in range(1, n)]
    events.append(
        row(
            0,
            jurisdiction=Jurisdiction.US,
            transaction_type=TransactionType.OPEN_MARKET_BUY,
            usd_value=498_000.0,
            is_first_time_filer=True,
        )
    )
    return Pattern(
        kind="sector_concentration",
        subject="Technology",
        summary="Technology accounts for the largest share of the window.",
        events=events,
        strength=0.7,
        falsifier="The count could be an artefact of two disclosure regimes.",
        metrics={"issuers": 33.0, "filings": float(n)},
    )


def test_the_filings_array_is_capped():
    payload = pattern_payload(a_wide_pattern())
    assert len(payload["supporting_filings"]) == MAX_SUPPORTING_FILINGS


def test_a_small_pattern_is_sent_whole_and_says_nothing_about_truncation():
    payload = pattern_payload(a_wide_pattern(n=6))
    assert len(payload["supporting_filings"]) == 6
    assert "supporting_filings_note" not in payload


def test_truncation_is_declared_so_the_model_cannot_claim_the_set_is_complete():
    payload = pattern_payload(a_wide_pattern())
    note = payload["supporting_filings_note"]
    assert str(MAX_SUPPORTING_FILINGS) in note and "43" in note


def test_the_aggregates_still_describe_every_filing_not_the_shown_ones():
    """Totals are computed from the full set and must not follow the cut."""
    wide = a_wide_pattern()
    payload = pattern_payload(wide)
    priced = [e.effective_usd for e in wide.events if e.effective_usd is not None]
    assert payload["filings_count"] == len(wide.events)
    assert payload["metrics_display"] == {"filings": "43", "issuers": "33"}
    # $43.0M is the largest row; the total is far above anything the shown subset sums to.
    assert payload["largest_display"] is not None
    from whale_agent.summarization.render import format_usd

    assert payload["total_disclosed_display"] == format_usd(sum(priced))
    assert payload["largest_display"] == format_usd(max(priced))


def test_the_lone_us_row_survives_even_though_it_is_the_smallest():
    """The note the model writes is often *about* that row.

    It cannot cite what it cannot see.
    """
    payload = pattern_payload(a_wide_pattern())
    shown = payload["supporting_filings"]
    assert any(f["jurisdiction"] == "JP" for f in shown)
    us = [f for f in shown if f["jurisdiction"] == "US"]
    assert len(us) == 1
    assert us[0]["is_first_time_filer"] is True


def test_the_largest_filings_survive():
    payload = pattern_payload(a_wide_pattern())
    shown = {f["issuer_name"] for f in payload["supporting_filings"]}
    assert {"Issuer 42", "Issuer 41", "Issuer 40"} <= shown


def test_unused_per_row_fields_are_not_paid_for():
    """No article ever printed a ticker, an event id, or a price-source enum."""
    shown = pattern_payload(a_wide_pattern())["supporting_filings"][0]
    for field in ("event_id", "ticker", "price_source"):
        assert field not in shown


def test_the_prompt_is_a_fraction_of_its_former_size():
    """Guards the saving: the old body ran to 33.7KB on a pattern this shape."""
    assert len(build_user_prompt(a_wide_pattern()).encode()) < 20_000
