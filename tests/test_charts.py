"""Header charts: drawn from event fields, or not drawn at all."""

from __future__ import annotations

import re

from tests.conftest import make_event
from whale_agent.models.context import EventContext
from whale_agent.models.event import TransactionType
from whale_agent.publishing.charts import choose_chart_kind, render_chart_svg
from whale_agent.summarization.provenance import check_unsourced_numbers
from whale_agent.summarization.render_html import (
    ACCUMULATE,
    DISTRIBUTE,
    html_to_text,
)


def test_float_share_is_preferred_over_dollars_when_enrichment_supplied_it():
    """Percent of float is the honest denominator, so it wins when it is available."""
    events = [
        make_event(filer_name="A", context=EventContext(percent_of_float=2.1)),
        make_event(filer_name="B", context=EventContext(percent_of_float=1.4)),
    ]
    assert choose_chart_kind(events) == "position_vs_float"


def test_dollar_values_are_charted_when_float_is_unknown():
    """Without vendor float coverage the chart falls back to the filings' own figures."""
    events = [make_event(filer_name="A"), make_event(filer_name="B")]
    assert choose_chart_kind(events) == "disclosed_value_by_filing"


def test_no_chart_is_produced_when_the_data_is_absent():
    """A header with nothing to show is omitted rather than drawn empty."""
    events = [
        make_event(filer_name="A", usd_value=None),
        make_event(filer_name="B", usd_value=None),
    ]
    assert choose_chart_kind(events) is None
    assert render_chart_svg(events) is None


def test_a_single_row_is_not_a_chart():
    """One bar is a number with a rectangle around it, so the header is dropped."""
    assert render_chart_svg([make_event(filer_name="A")]) is None


def test_unpriced_filings_are_omitted_rather_than_drawn_as_zero():
    """A filing with no disclosed value is not a filing worth nothing."""
    events = [
        make_event(filer_name="Priced One", usd_value=9_000_000.0),
        make_event(filer_name="Priced Two", usd_value=6_000_000.0),
        make_event(filer_name="Unpriced", usd_value=None),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    assert "Unpriced" not in svg
    # Background plus one bar per priced filing. Nothing else is a rectangle.
    assert svg.count("<rect") == 3
    assert "$9.0M" in svg and "$6.0M" in svg


def test_chart_labels_carry_no_unsourced_figures():
    """Every number the chart *prints* must trace to the events it was drawn from.

    Checked against the stripped text rather than the markup, exactly as the email is:
    coordinates and hex colours are geometry, not claims about magnitude.
    """
    events = [
        make_event(
            filer_name="A", usd_value=9_000_000.0, context=EventContext(percent_of_float=2.1)
        ),
        make_event(
            filer_name="B", usd_value=6_000_000.0, context=EventContext(percent_of_float=1.4)
        ),
    ]
    for kind in ("position_vs_float", "disclosed_value_by_filing"):
        svg = render_chart_svg(events, kind)
        assert svg is not None
        assert check_unsourced_numbers(html_to_text(svg), events) == []


def test_hostile_names_are_escaped_in_the_svg():
    """Filer names come from third-party feeds and land inside markup."""
    events = [
        make_event(filer_name='<script>alert("x")</script>'),
        make_event(filer_name="Ordinary Fund"),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_long_names_are_clipped_inside_the_label_gutter():
    """Nothing is allowed to paint outside the viewBox.

    The gutter is set flush right, so an unclipped name walks off the left edge of the
    figure and over whatever the page puts beside it. Clipping is estimated from an
    average glyph width, so the assertion is on the character count the estimate yields.
    """
    long_name = "Wilhelmina Constance Featherstonehaugh-Bartholomew"
    events = [
        make_event(filer_name=long_name, usd_value=9_000_000.0),
        make_event(filer_name="B", usd_value=6_000_000.0),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    assert long_name not in svg
    assert "…" in svg
    drawn = re.search(r">(Wilhelm[^<]*)<", svg).group(1)
    assert len(drawn) <= 30 and drawn.endswith("…")


def test_short_names_are_not_clipped():
    events = [
        make_event(filer_name="Lucas Donna", usd_value=9_000_000.0),
        make_event(filer_name="B", usd_value=6_000_000.0),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None and "Lucas Donna" in svg and "…" not in svg


def test_repeat_filings_by_one_filer_on_one_day_are_distinguished():
    """Two filings are two rows, and must not read as the same row printed twice."""
    events = [
        make_event(filer_name="Allbaugh Larry Eugene", usd_value=4_000_000.0),
        make_event(filer_name="Allbaugh Larry Eugene", usd_value=250_000.0),
        make_event(filer_name="Someone Else", usd_value=1_000_000.0),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    assert "2026-07-25 (a)" in svg and "2026-07-25 (b)" in svg
    # The unrepeated filer keeps a clean date with no suffix.
    assert ">2026-07-25<" in svg


def test_distinguishing_suffixes_introduce_no_unsourced_figures():
    """Letters, not digits: a numeral in the drawn text would have to trace to a filing."""
    events = [
        make_event(filer_name="Repeat Filer", usd_value=4_000_000.0),
        make_event(filer_name="Repeat Filer", usd_value=250_000.0),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    assert check_unsourced_numbers(html_to_text(svg), events) == []


def test_the_value_label_sits_immediately_after_its_own_bar():
    """A far right column makes the reader travel back across every other bar."""
    events = [
        make_event(filer_name="Big", usd_value=2_500_000_000.0),
        make_event(filer_name="Small", usd_value=50_000.0),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    bars = [
        (float(x), float(w))
        for x, w in re.findall(
            r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)" height="12.0"', svg
        )
    ]
    label_x = [float(m) for m in re.findall(r'<text x="([\d.]+)"[^>]*font-weight="600"', svg)]
    assert len(bars) == 2 and len(label_x) == 2
    for (start, width), label in zip(bars, label_x, strict=False):
        assert 0 < label - (start + width) < 12


def test_a_tiny_value_beside_a_huge_one_still_draws_a_visible_bar():
    """A $50K filing next to $2.5B rounds to nothing, but it is not nothing."""
    events = [
        make_event(filer_name="Big", usd_value=2_500_000_000.0),
        make_event(filer_name="Small", usd_value=50_000.0),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    widths = [
        float(m) for m in re.findall(r'<rect x="2[\d.]+" y="[\d.]+" width="([\d.]+)"', svg)
    ]
    assert min(widths) >= 2.0


def test_nothing_is_drawn_outside_the_viewbox_at_twelve_rows():
    events = [
        make_event(
            filer_name=f"Filer Number {n:02d} Of Twelve", usd_value=1_000_000.0 * (n + 1)
        )
        for n in range(12)
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    width, height = (
        float(v) for v in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups()
    )
    xs = [float(m) for m in re.findall(r'<(?:rect|text|line) x1?="(-?[\d.]+)"', svg)]
    ys = [float(m) for m in re.findall(r'y1?="(-?[\d.]+)"', svg)]
    assert min(xs) >= 0.0 and max(xs) < width
    assert min(ys) >= 0.0 and max(ys) <= height


def test_bar_colour_follows_the_direction_of_the_filing():
    """Green for accumulation, red for distribution, both taken from the house palette."""
    events = [
        make_event(filer_name="Buyer", usd_value=9_000_000.0),
        make_event(
            filer_name="Seller",
            usd_value=6_000_000.0,
            transaction_type=TransactionType.OPEN_MARKET_SELL,
        ),
    ]
    svg = render_chart_svg(events, "disclosed_value_by_filing")
    assert svg is not None
    assert f'fill="{ACCUMULATE}"' in svg and f'fill="{DISTRIBUTE}"' in svg


def test_output_is_byte_identical_for_the_same_input():
    """The article is regenerated on a schedule; a churning figure is a false diff."""
    events = [
        make_event(filer_name="A", usd_value=9_000_000.0),
        make_event(filer_name="B", usd_value=9_000_000.0),
        make_event(filer_name="C", usd_value=6_000_000.0),
    ]
    first = render_chart_svg(events, "disclosed_value_by_filing")
    second = render_chart_svg(list(reversed(events)), "disclosed_value_by_filing")
    assert first is not None and first == second


def test_the_issue_page_asks_for_a_chart_kind_that_exists():
    """A wrong kind returns None, and a missing chart is invisible in a passing suite.

    The issue page shipped once with no chart at all because it named a kind ("bars")
    that render_chart_svg does not know. Nothing failed: the function returned None and
    the figure block was simply skipped.
    """
    from tests.conftest import make_event
    from whale_agent.publishing.charts import choose_chart_kind, render_chart_svg

    events = [
        make_event(filer_name=n, usd_value=1_000_000.0 * i)
        for i, n in enumerate("ABCDE", start=1)
    ]
    kind = choose_chart_kind(events)
    assert kind, "a set of priced events should yield a chart kind"
    assert render_chart_svg(events, kind, title=""), "the chosen kind must render"
