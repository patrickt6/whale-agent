"""The HTML email body.

Two things carry real risk here and both are tested: that filer names from third-party
feeds cannot inject markup, and that the rendered email satisfies the same numeric
provenance gate as the text digest. Everything else is layout.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.jobs.pipeline import build_digest_html, build_ranked
from whale_agent.models.enums import Jurisdiction, PriceSource, TransactionType
from whale_agent.storage.db import Store
from whale_agent.summarization.prose import DigestProse
from whale_agent.summarization.provenance import check_unsourced_numbers
from whale_agent.summarization.render_html import (
    ACCUMULATE,
    DISTRIBUTE,
    html_to_text,
    provenance_label,
    render_digest_html,
)

ON = date(2026, 7, 26)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _ranked(store, events):
    return build_ranked(events, store, Settings(), on=ON)


# -- safety ---------------------------------------------------------------------
def test_hostile_filer_name_cannot_inject_markup(store):
    hostile = '<script>alert("x")</script>'
    ranked = _ranked(store, [make_event(filer_name=hostile, usd_value=9_000_000.0)])
    markup = render_digest_html(ranked, ON)
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_hostile_source_url_cannot_break_out_of_the_href(store):
    ranked = _ranked(
        store,
        [make_event(usd_value=9_000_000.0, source_url='" onmouseover="evil()')],
    )
    markup = render_digest_html(ranked, ON)
    assert 'onmouseover="evil()"' not in markup
    assert "&quot;" in markup


# -- provenance gate --------------------------------------------------------------
def test_rendered_email_passes_the_provenance_gate(store):
    ranked = _ranked(
        store,
        [
            make_event(filer_name="Berkshire Hathaway", usd_value=412_000_000.0),
            make_event(
                filer_name="Example Activist Partners",
                usd_value=98_000_000.0,
                percent_of_company=9.3,
            ),
        ],
    )
    markup = render_digest_html(ranked, ON)
    assert check_unsourced_numbers(html_to_text(markup), ranked) == []


def test_numeric_ticker_does_not_trip_the_gate(store):
    """Taiwan lists 2330.TW; a bare 2330 reads like a magnitude unless allowlisted."""
    ranked = _ranked(
        store,
        [
            make_event(
                filer_name="劉德音",
                issuer_name="台積電",
                ticker="2330.TW",
                jurisdiction=Jurisdiction.TAIWAN,
                transaction_type=TransactionType.PASSIVE_13G,
                usd_value=None,
                implied_usd_value=61_500_000.0,
                percent_of_company=6.1,
            )
        ],
    )
    markup = render_digest_html(ranked, ON)
    assert check_unsourced_numbers(html_to_text(markup), ranked) == []
    assert "2330.TW" in markup


def test_rank_adjacent_to_a_b_name_does_not_read_as_billions(store):
    """Regression: "01 Berkshire" once tokenized as "01 B" and failed the gate."""
    ranked = _ranked(
        store, [make_event(filer_name="Berkshire Hathaway", usd_value=9_000_000.0)]
    )
    assert check_unsourced_numbers(html_to_text(render_digest_html(ranked, ON)), ranked) == []


def test_fabricated_prose_number_drops_all_prose_from_the_email(store):
    ranked = _ranked(store, [make_event(usd_value=9_000_000.0)])
    prose = DigestProse(headline="The stake is now worth $77.7M.")
    markup = build_digest_html(ranked, ON, prose)
    assert "77.7M" not in markup
    assert "$9.0M" in markup


# -- the signature: provenance labels ----------------------------------------------
@pytest.mark.parametrize(
    "source, estimate, expected",
    [
        (PriceSource.FILING_STATED, False, "Stated in the filing"),
        (PriceSource.CLOSE_ON_DATE, True, "Estimated from trade-date close"),
        (PriceSource.MOST_RECENT_CLOSE, True, "Estimated from last close"),
        (PriceSource.NOT_PRICED, False, "Value as disclosed"),
        (PriceSource.NOT_PRICED, True, "Estimated from disclosure"),
    ],
)
def test_provenance_label(source, estimate, expected):
    event = make_event(price_source=source, usd_value_is_estimate=estimate)
    assert provenance_label(event) == expected


def test_provenance_label_for_an_unvalued_event():
    assert provenance_label(make_event(usd_value=None)) == "Not disclosed"


def test_provenance_labels_never_contain_a_number():
    """The label explains a figure; it must not become one."""
    for source in PriceSource:
        label = provenance_label(make_event(price_source=source, usd_value=1.0))
        assert not any(ch.isdigit() for ch in label)


# -- layout ------------------------------------------------------------------------
def test_buys_and_sells_are_coloured_differently(store):
    buy = _ranked(store, [make_event(usd_value=9_000_000.0)])
    sell = _ranked(
        store,
        [
            make_event(
                filer_name="Seller",
                usd_value=9_000_000.0,
                transaction_type=TransactionType.OPEN_MARKET_SELL,
            )
        ],
    )
    assert ACCUMULATE in render_digest_html(buy, ON)
    assert DISTRIBUTE in render_digest_html(sell, ON)


def test_coverage_notes_appear(store):
    markup = render_digest_html(
        _ranked(store, [make_event(usd_value=9_000_000.0)]),
        ON,
        coverage_notes=["Japan EDINET not included (not configured)"],
    )
    assert "Japan EDINET not included" in markup


def test_empty_day_still_renders_a_complete_email(store):
    markup = render_digest_html([], ON, coverage_notes=["FMP not included"])
    assert "Nothing cleared the $5M threshold." in markup
    assert "FMP not included" in markup
    # Assert on the rendered text: the source wraps this sentence across lines.
    assert "not investment advice" in html_to_text(markup)


def test_prose_replaces_the_template_line(store):
    ranked = _ranked(store, [make_event(usd_value=9_000_000.0)])
    prose = DigestProse(
        headline="A quiet session.",
        why_it_matters={ranked[0].event_id: "A recognizable buyer stepping in."},
    )
    markup = render_digest_html(ranked, ON, prose)
    assert "A quiet session." in markup
    assert "A recognizable buyer stepping in." in markup


def test_routine_events_carry_a_caveat_without_prose(store):
    ranked = _ranked(store, [make_event(usd_value=9_000_000.0, is_routine=True)])
    assert "outine, calendar-driven trade" in render_digest_html(ranked, ON)


def test_email_is_self_contained(store):
    """A strict client blocking remote content must see exactly what everyone else sees."""
    markup = render_digest_html(_ranked(store, [make_event(usd_value=9_000_000.0)]), ON)
    assert "<img" not in markup
    assert "http://" not in markup.replace("http://www.w3.org", "")
    for remote in ("<link", "@import", "src="):
        assert remote not in markup


def test_html_to_text_strips_style_blocks():
    text = html_to_text("<style>.a{color:#123456}</style><p>Hello 5</p>")
    assert "123456" not in text
    assert "Hello 5" in text


# -- regression: identifiers are not magnitudes ---------------------------------
# The first live run against FMP crashed here. Vendor feeds attach a source URL
# containing the filer's CIK and the filing's accession number, and those digits are
# long enough to read as dollar amounts.


def test_source_url_identifiers_do_not_trip_the_gate(store):
    from whale_agent.summarization.render import render_digest

    ranked = _ranked(
        store,
        [
            make_event(
                filer_name="Some Filer",
                usd_value=9_000_000.0,
                issuer_id="0001863328",
                filer_id="1516513",
                source_url=(
                    "https://www.sec.gov/Archives/edgar/data/1863328/"
                    "000186332826000002/xslF345X05/form4.xml"
                ),
            )
        ],
    )
    assert check_unsourced_numbers(render_digest(ranked, ON), ranked) == []
    assert check_unsourced_numbers(html_to_text(render_digest_html(ranked, ON)), ranked) == []


def test_a_fabricated_figure_still_fails_even_next_to_a_url(store):
    """Stripping URLs must not blind the gate to a real invention beside one."""
    ranked = _ranked(store, [make_event(usd_value=9_000_000.0)])
    text = (
        "1. Filer bought $9.0M. See https://www.sec.gov/Archives/edgar/data/123/456 "
        "and note the $88.8M position."
    )
    assert check_unsourced_numbers(text, ranked) == ["$88.8M"]


# -- summary bar and email-client hardening --------------------------------------
def test_summary_line_aggregates_pass_the_gate(store):
    """The header states a count and a total. Both are computed, so both must be
    allowlisted or the gate rejects the email it just built."""
    ranked = _ranked(
        store,
        [
            make_event(filer_name="A", usd_value=12_000_000.0),
            make_event(filer_name="B", usd_value=8_000_000.0),
        ],
    )
    markup = render_digest_html(ranked, ON)
    # The header is a strip of value and label cells, so check value then label.
    text = html_to_text(markup)
    assert "2 Disclosures" in text
    assert "$20.0M Total disclosed" in text
    assert "$12.0M Largest" in text
    assert check_unsourced_numbers(html_to_text(markup), ranked) == []


def test_summary_line_handles_an_empty_period():
    from whale_agent.summarization.render_html import summary_line

    assert summary_line([]) == "Nothing cleared the threshold"


def test_every_text_element_states_its_own_background(store):
    """Gmail ignores prefers-color-scheme and force-inverts using its own algorithm.
    Stating background and colour together on each element is the only defence, so a
    bare colour declaration with no background beside it is a bug."""
    markup = render_digest_html(_ranked(store, [make_event(usd_value=9_000_000.0)]), ON)
    for style in re.findall(r'style="([^"]*color:[^"]*)"', markup):
        if "background" in style or "text-transform" in style or "font-weight" in style:
            continue
        assert "color:" not in style or "background" in style, style


def test_outlook_gets_a_conditional_font_stack(store):
    markup = render_digest_html(_ranked(store, [make_event(usd_value=9_000_000.0)]), ON)
    assert "<!--[if mso]>" in markup
    assert "Segoe UI" in markup


def test_html_to_text_strips_mso_conditional_comments(store):
    """The MSO block is a comment; its CSS must not reach the provenance scanner."""
    ranked = _ranked(store, [make_event(usd_value=9_000_000.0)])
    assert "[if mso]" not in html_to_text(render_digest_html(ranked, ON))


def test_html_action_labels_are_not_shouted(store):
    """The plain-text digest uses caps for emphasis; a styled email should not."""
    ranked = _ranked(store, [make_event(usd_value=9_000_000.0)])
    markup = render_digest_html(ranked, ON)
    assert "Open-market buy" in markup
    assert "OPEN-MARKET BUY" not in markup


def test_section_headings_avoid_internal_tier_jargon(store):
    ranked = _ranked(store, [make_event(usd_value=9_000_000.0)])
    markup = render_digest_html(ranked, ON)
    assert "Notable this period" in markup
    assert "TIER 2" not in markup


# -- the Why line must name the terms the scorer actually used -------------------
def test_why_line_cites_enrichment_reasons(store):
    """Enrichment that never reaches the reader is enrichment we paid for and hid."""
    from whale_agent.models.context import EventContext
    from whale_agent.summarization.render import why_it_matters

    event = make_event(
        usd_value=6_800_000.0,
        context=EventContext(
            market_cap_usd=210_000_000.0,
            percent_of_float=3.2,
            filer_prior_filings=0,
        ),
    )
    why = why_it_matters(event)
    assert "3.2% of float" in why
    assert "small company at $210.0M" in why
    assert "never seen this filer before" in why


def test_why_line_says_so_when_there_is_nothing_to_say(store):
    """A bare threshold pass should admit it rather than manufacture a reason."""
    from whale_agent.summarization.render import why_it_matters

    why = why_it_matters(make_event(usd_value=6_000_000.0))
    assert "nothing else to distinguish it" in why


def test_why_line_stays_short(store):
    """Five reasons is a shrug; the line is truncated to the strongest few."""
    from whale_agent.models.context import EventContext
    from whale_agent.models.enums import TransactionType as TT
    from whale_agent.summarization.render import why_it_matters

    event = make_event(
        usd_value=50_000_000.0,
        transaction_type=TT.ACTIVIST_13D,
        cluster_size=4,
        context=EventContext(
            market_cap_usd=180_000_000.0,
            percent_of_float=9.0,
            filer_prior_filings=0,
            is_new_position=True,
        ),
    )
    assert why_it_matters(event).count(";") <= 2


def test_enrichment_figures_pass_the_provenance_gate(store):
    """Market cap and percent-of-float are cited as reasons, so they must be sourced."""
    from whale_agent.models.context import EventContext

    ranked = _ranked(
        store,
        [
            make_event(
                usd_value=6_800_000.0,
                context=EventContext(
                    market_cap_usd=210_000_000.0,
                    percent_of_float=3.2,
                    filer_prior_filings=0,
                ),
            )
        ],
    )
    markup = render_digest_html(ranked, ON)
    assert check_unsourced_numbers(html_to_text(markup), ranked) == []


def test_html_market_stakes_marks_institutions_and_states_its_coverage():
    """The HTML block is built separately from the text one and drifted from it.

    It carried neither the institution label nor the real filing count, so the emailed
    copy still claimed to cover "every 13D/G" while the plaintext copy had been fixed.
    """
    from datetime import date as _d

    from whale_agent.ingestion.fund_watchlist import MarketStake, StakeDetail
    from whale_agent.summarization.render_weekly_html import _market_stakes_block

    def stake(holder, pct, codes):
        return MarketStake(
            filed=_d(2026, 8, 3),
            form="SCHEDULE 13G",
            holders=[holder],
            detail=StakeDetail(
                issuer="Acme", percent=pct, shares=1, holders=[holder], person_types=codes
            ),
            url="https://example.invalid/f/",
        )

    html = _market_stakes_block(
        [
            stake("Farallon Capital Management", 9.4, ["IA"]),
            stake("Richard E. Uihlein", 49.3, ["IN"]),
        ],
        _d(2026, 8, 4),
        _d(2026, 7, 28),
        examined=1149,
    )
    # Grouped rather than labelled inline: the fund is under Institutions, the natural
    # person under the second list, and the header states its true coverage.
    assert "Institutions" in html
    assert html.index("Farallon Capital Management") < html.index("Other filers")
    assert html.index("Other filers") < html.index("Richard E. Uihlein")
    assert "1,149" in html


def test_resale_block_renders_a_registration_row():
    """Pins the HTML block against a NameError that the job swallows.

    `digest_weekly` catches layout failures and falls back to plain-text conversion, so
    a broken block ships a degraded email with exit code 0 and one WARNING line. The
    whole suite passed while this block raised `name 'rows' is not defined`.
    """
    from datetime import date as _d

    from whale_agent.ingestion.resale_watch import ResaleRegistration
    from whale_agent.summarization.render_weekly_html import _resale_block

    reg = ResaleRegistration(
        whale_name="Situational Awareness",
        issuer="SharonAI Holdings Inc.",
        symbol="",
        form="S-1",
        filed=_d(2026, 7, 31),
        shares_registered=7_274_842,
        shares_held=7_563_029,
        url="https://example.invalid/forms-1.htm",
    )
    html = _resale_block([reg], _d(2026, 8, 4), _d(2026, 7, 28))
    assert "Situational Awareness" in html
    assert "example.invalid" in html


def test_every_weekly_html_block_survives_being_called():
    """Smoke-call each block with empty input: a NameError anywhere is a degraded email."""
    from datetime import date as _d

    import whale_agent.summarization.render_weekly_html as m

    on, since = _d(2026, 8, 4), _d(2026, 7, 28)
    assert m._resale_block([], on, since)
    assert m._market_stakes_block([], on, since)
    assert m._fund_moves_block([], on, since)
