"""Header images built from our own filings, as hand-written inline SVG.

Two decisions, both of which are about the product rather than about graphics.

**The header is evidence, not decoration.** A stock photograph of a trading floor, or a
generated image of a whale, on an article whose entire claim is that every figure traces
to a filing, quietly says the opposite: that the picture is there to make you feel
something. So the header is a chart of the rows the article is about, and if those rows
carry nothing chartable there is no header. An article with no image is a smaller loss
than an article with a decorative one.

**Hand-built SVG rather than matplotlib.** matplotlib is not currently a dependency, and
adding a plotting stack plus its numeric dependencies to draw a dozen rectangles is a
poor trade for a project whose deployment story is a cron job. Inline SVG also survives
the constraint that governs the email: it is markup, not an image, so a client blocking
remote images still renders it, and there is no asset to host or expire.

Nothing is interpolated. Every bar is one filing's stated or enriched field; a filing
with no value contributes no bar rather than a zero-height one, because a zero-height bar
is a claim that the position was worthless. Every numeric label is formatted exactly as
the provenance gate allowlists it, so the finished page passes the same check as the
email.

**Typography without a font engine.** There is no way to measure a string in inline SVG,
so every label is truncated at a character count derived from an average glyph width for
the sans stack. The estimate is deliberately conservative: a label that stops one word
early is invisible, a label that overruns the gutter and paints outside the viewBox is
the most obvious kind of production error.
"""

from __future__ import annotations

import html
from collections.abc import Callable

from whale_agent.models.event import NormalizedEvent
from whale_agent.summarization.render import format_usd
from whale_agent.summarization.render_html import (
    INK,
    INK_FAINT,
    INK_SOFT,
    NAVY,
    PAPER,
    RULE,
    RULE_STRONG,
    SANS,
    SERIF,
    amount_colour,
)

__all__ = ["choose_chart_kind", "render_chart_svg", "CHART_KINDS"]

# A chart of one bar is a number with a rectangle around it.
MIN_BARS = 2

CHART_KINDS = ("position_vs_float", "disclosed_value_by_filing")

# Canvas. Kept under a thousand units in both axes so no coordinate in the emitted markup
# reads as a magnitude to the provenance tokenizer -- the gate cannot tell an x-offset
# from a dollar figure, and it should not have to.
_WIDTH = 660.0
# Right edge of the label gutter. Names are set flush right against it, so the ragged
# edge falls away from the bars and the bars all start from one clean vertical.
_LABEL_R = 196.0
_GUTTER = 14.0
# Room kept clear at the right for the value label of the longest bar. The label sits
# immediately after the bar it belongs to, so this is the only place it can overflow.
_VALUE_RESERVE = 88.0
_ROW_H = 32.0
_BAR_H = 12.0
_BOTTOM = 34.0

_NAME_SIZE = 12.0
_SUB_SIZE = 9.5
_VALUE_SIZE = 12.0

# Average advance width as a fraction of font size for the Helvetica/Arial stack, across
# mixed-case proper nouns. Measured generously; see the module docstring.
_GLYPH_W = 0.62

_FIGURE_FONT = f"{SANS};font-variant-numeric:tabular-nums lining-nums"

# One row per filing, as (primary label, qualifier, value, printed value, bar colour).
_Row = tuple[str, str, float, str, str]


def _e(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _f(value: float) -> str:
    """One decimal place, so coordinates stay short and stable between runs."""
    return f"{value:.1f}"


def _fits(width: float, size: float) -> int:
    """How many characters of `size` type fit in `width`, never fewer than one."""
    return max(1, int(width / (size * _GLYPH_W)))


def _clip(label: str, width: float, size: float) -> str:
    """Cut to an estimated pixel width, with a real ellipsis rather than a spill.

    The previous version let long names run past x=0 and out of the viewBox, which
    browsers happily paint over whatever sits beside the figure.
    """
    limit = _fits(width, size)
    if len(label) <= limit:
        return label
    return label[: max(1, limit - 1)].rstrip(" ·-") + "…"


def _distinguish(rows: list[_Row]) -> list[_Row]:
    """Letter-suffix rows whose filer and qualifier are identical.

    Two filings by one person on one day are two filings, but drawn as two identical
    label lines they read as the same row printed twice, which is worse than useless in
    a document about care with data. Letters rather than digits: a numeral in the
    rendered text would have to be traceable to a filing, and this one is not a figure.
    """
    counts: dict[tuple[str, str], int] = {}
    for primary, qualifier, _value, _display, _colour in rows:
        counts[(primary, qualifier)] = counts.get((primary, qualifier), 0) + 1
    seen: dict[tuple[str, str], int] = {}
    out: list[_Row] = []
    for primary, qualifier, value, display, colour in rows:
        key = (primary, qualifier)
        if counts[key] > 1:
            index = seen.get(key, 0)
            seen[key] = index + 1
            qualifier = f"{qualifier} ({_letter(index)})".strip()
        out.append((primary, qualifier, value, display, colour))
    return out


def _letter(index: int) -> str:
    """a, b, ... z, aa, ab: an unbounded non-numeric ordinal."""
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("a") + remainder) + label
    return label


def _order(rows: list[_Row]) -> list[_Row]:
    """Largest first, with names as the tiebreak so equal values never reorder."""
    return _distinguish(sorted(rows, key=lambda r: (-r[2], r[0], r[1])))


def _float_bars(events: list[NormalizedEvent]) -> list[_Row]:
    return _order(
        [
            (
                event.filer_name,
                event.issuer_name,
                event.context.percent_of_float,
                f"{event.context.percent_of_float:.1f}%",
                amount_colour(event),
            )
            for event in events
            if event.context.percent_of_float is not None
        ]
    )


def _value_bars(events: list[NormalizedEvent]) -> list[_Row]:
    return _order(
        [
            (
                event.filer_name,
                event.disclosure_date.isoformat(),
                event.effective_usd,
                format_usd(event.effective_usd),
                amount_colour(event),
            )
            for event in events
            if event.effective_usd is not None
        ]
    )


_BARS: dict[str, Callable[[list[NormalizedEvent]], list[_Row]]] = {
    "position_vs_float": _float_bars,
    "disclosed_value_by_filing": _value_bars,
}

_CAPTION = {
    "position_vs_float": "Position size as a share of free float, per disclosure",
    "disclosed_value_by_filing": "Disclosed value per filing, largest first",
}

# The unit, not the subject. The article prints its own caption directly underneath the
# figure, so repeating the subject line here would just be the same sentence twice.
_AXIS = {
    "position_vs_float": "PERCENT OF FREE FLOAT",
    "disclosed_value_by_filing": "DISCLOSED VALUE, US DOLLARS",
}

_FOOTNOTE = {
    "position_vs_float": "Filings with no float estimate are omitted, never zeroed.",
    "disclosed_value_by_filing": "Filings with no disclosed value are omitted, never zeroed.",
}


def choose_chart_kind(events: list[NormalizedEvent]) -> str | None:
    """The most informative chart these rows can actually support, or None.

    Float share is preferred over dollar value because it is the honest denominator: a
    $6M position is a real bet in a small company and rounding error in a large one. It
    is also the field enrichment most often lacks, which is why dollars are the fallback
    and "no chart" is a normal outcome rather than an error.
    """
    for kind in CHART_KINDS:
        if len(_BARS[kind](events)) >= MIN_BARS:
            return kind
    return None


def render_chart_svg(
    events: list[NormalizedEvent],
    kind: str | None = None,
    title: str = "",
) -> str | None:
    """Inline SVG for the header, or None when the data does not support one.

    Returning None rather than an empty frame is the point: the caller omits the header
    entirely, and no reader is shown an axis with nothing on it.
    """
    kind = kind or choose_chart_kind(events)
    if kind not in _BARS:
        return None
    bars = _BARS[kind](events)
    if len(bars) < MIN_BARS:
        return None

    peak = max(value for _, _, value, _, _ in bars)
    if peak <= 0:
        return None

    x0 = _LABEL_R + _GUTTER
    plot_w = _WIDTH - x0 - _VALUE_RESERVE
    head = 30.0 if title else 0.0
    top = head + 24.0
    plot_b = top + _ROW_H * len(bars)
    height = plot_b + _BOTTOM

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_f(_WIDTH)} {_f(height)}" '
        f'width="100%" role="img" aria-label="{_e(_CAPTION[kind])}" '
        f'style="background:{PAPER};display:block;">',
        f'<rect x="0" y="0" width="{_f(_WIDTH)}" height="{_f(height)}" fill="{PAPER}"/>',
    ]
    if title:
        parts.append(
            f'<text x="0" y="16" font-family="{SERIF}" font-size="15" fill="{INK}">'
            f"{_e(_clip(title, _WIDTH, 15.0))}</text>"
        )
        parts.append(
            f'<rect x="0" y="{_f(head - 8.0)}" width="{_f(_WIDTH)}" height="2" fill="{NAVY}"/>'
        )
    parts.append(
        f'<text x="{_f(x0)}" y="{_f(top - 11.0)}" font-family="{SANS}" font-size="9.5" '
        f'letter-spacing="0.9" fill="{INK_FAINT}">{_e(_AXIS[kind])}</text>'
    )

    # Quarter-of-peak gridlines, unlabelled. They give the eye something to compare
    # against without asserting a tick value we would then have to source.
    for step in (1, 2, 3, 4):
        x = x0 + plot_w * step / 4.0
        parts.append(
            f'<line x1="{_f(x)}" y1="{_f(top - 4.0)}" x2="{_f(x)}" y2="{_f(plot_b)}" '
            f'stroke="{RULE}" stroke-width="1"/>'
        )

    for index, (primary, qualifier, value, display, colour) in enumerate(bars):
        y = top + _ROW_H * index
        width = max(plot_w * (value / peak), 2.0)
        parts.append(
            f'<text x="{_f(_LABEL_R)}" y="{_f(y + 13.0)}" text-anchor="end" '
            f'font-family="{SANS}" font-size="{_f(_NAME_SIZE)}" fill="{INK}">'
            f"{_e(_clip(primary, _LABEL_R, _NAME_SIZE))}</text>"
        )
        if qualifier:
            parts.append(
                f'<text x="{_f(_LABEL_R)}" y="{_f(y + 24.5)}" text-anchor="end" '
                f'font-family="{SANS}" font-size="{_f(_SUB_SIZE)}" fill="{INK_FAINT}">'
                f"{_e(_clip(qualifier, _LABEL_R, _SUB_SIZE))}</text>"
            )
        parts.append(
            f'<rect x="{_f(x0)}" y="{_f(y + 10.0)}" width="{_f(width)}" '
            f'height="{_f(_BAR_H)}" fill="{colour}"/>'
        )
        # Immediately after the bar it belongs to. In a far right column the eye has to
        # travel back across every other bar to pair a number with its rectangle.
        parts.append(
            f'<text x="{_f(x0 + width + 7.0)}" y="{_f(y + 20.0)}" '
            f'font-family="{_FIGURE_FONT}" font-size="{_f(_VALUE_SIZE)}" '
            f'font-weight="600" fill="{INK}">{_e(display)}</text>'
        )

    # The zero line the bars actually grow from, and the baseline under the last row.
    parts.append(
        f'<line x1="{_f(x0)}" y1="{_f(top - 4.0)}" x2="{_f(x0)}" y2="{_f(plot_b)}" '
        f'stroke="{RULE_STRONG}" stroke-width="1"/>'
    )
    parts.append(
        f'<line x1="{_f(x0)}" y1="{_f(plot_b)}" x2="{_f(x0 + plot_w)}" '
        f'y2="{_f(plot_b)}" stroke="{INK_SOFT}" stroke-width="1"/>'
    )
    parts.append(
        f'<text x="{_f(x0)}" y="{_f(plot_b + 20.0)}" font-family="{SERIF}" '
        f'font-size="11" font-style="italic" fill="{INK_FAINT}">'
        f"{_e(_FOOTNOTE[kind])}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)
