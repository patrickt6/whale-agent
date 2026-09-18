"""The weekly email as HTML: research notes, then a fortnight in four tables.

The weekly used to go out through `delivery/base.digest_to_html`, a generic
text-to-markup converter that gives every line the same weight. That was acceptable while
the weekly was a summary of a daily the reader already had. It is not acceptable now that
the weekly is the only thing sent, so this module gives it the same typographic register
as the article pages it links to.

Two things the reader asked for, and one he did not.

**Both links on every row he might act on.** A research note offers "Read the note" and
"Read the filings"; an overview row offers the filing directly. Reading the note is the
long path and reading the filing is the short one, and a reader who only wants to check a
number should not have to walk through an argument to reach it.

**Four tables, always all four**, even when three of them are thin -- which today they
are. `jobs/overview.py` carries the reasoning; the short version is that a section shown
empty with its own explanation is a report on our coverage, and a section quietly omitted
is a false impression that nothing happened.

**Tables, not prose, for the overview.** Email clients disagree about almost everything
except tables, and the fortnight view is genuinely tabular data.

**Decoration lives in one `<style>` block, not on every cell.** A measured render of a
real 52-manager watchlist put the email at 181KB against Gmail's ~102KB clip point, and
86% of that was markup, not words -- mostly the same `style="..."` string repeated on
every table cell and row. The fix was to hoist that repetition into short class names in
a single `<style>` block in `<head>`, which Gmail honours. Nothing
structural depends on it: a client that strips `<style>` (older Outlook, Gmail's app on a
non-Gmail account) still gets plain, readable table markup, native blue-underlined links,
and right-aligned numeric cells via the `align="right"` HTML attribute, which browsers
honour with no CSS at all. What such a client loses is decoration only -- the ink colour
hierarchy, hairline row rules, row padding, and the navy/beige accent colours.
"""

from __future__ import annotations

import html
from datetime import date, timedelta

from whale_agent.jobs.overview import Section
from whale_agent.models.thesis import Thesis
from whale_agent.summarization.render_html import (
    HERO_URL,
    INK,
    INK_FAINT,
    INK_SOFT,
    LINK,
    NAVY,
    NAVY_DEEP,
    PAGE,
    PAPER,
    RULE,
    RULE_STRONG,
    SANS,
    SERIF,
    hero_block,
)

__all__ = ["render_weekly_html"]

# Gmail clips a message body around ~102KB. These caps keep the email a digest with
# counts, while the full detail always still exists -- in the attached report, never
# silently dropped. Hoisting the repeated per-cell styles into the <style> block (see
# module docstring) freed most of the budget these caps used to fight over, so they were
# widened back up from the first, byte-starved pass (3/2/2) to cover most real weeks
# without truncation, while a watchlist far outside the norm (52 managers, dozens of
# rows per category) still degrades to a short digest instead of blowing the budget.
EMAIL_ROSTER_LIMIT = 10
EMAIL_MOVERS_LIMIT = 6
EMAIL_SECTION_ROW_LIMIT = 6
# Resale registrations had no cap at all: a quiet section for most weeks, but nothing
# stopped an active one from carrying dozens of rows straight into the email. Same
# pattern as every other capped section here -- moved to the report page, not dropped.
EMAIL_RESALE_LIMIT = 8
# A manager who filed a dozen times in an otherwise quiet week should not single-
# handedly carry the section's byte budget.
EMAIL_FILINGS_PER_MANAGER_LIMIT = 5
# The 13F headline section leads the email (see _quarterly_13f_block). Capped the same
# way as every other section here: a quarter where 35+ tracked managers all file in
# the same week (a real deadline-clustering event, not hypothetical -- see the 2026-08
# miss this section exists to fix) must still degrade to a compact digest rather than
# blow the byte budget, with the rest always available on the linked report page.
EMAIL_QUARTERLY_LIMIT = 8
EMAIL_QUARTERLY_MOVES_PER_MANAGER = 2


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _row_links(url: str, report_link: str, anchor: str) -> str:
    """One link per row: our own writeup when a report page is available, the SEC
    primary source otherwise.

    A measured 52-manager render put 443 of these at 58KB, almost entirely from
    printing both links on every one of ~217 rows -- enough on its own to push the
    email from 81,729 bytes back over Gmail's ~102,400 clip point. The SEC citation is
    not dropped, only moved: the report page (via `render_tracked_filings_page`) always
    carries it next to the same row, so a reader who wants the primary source is one
    click further rather than zero. `report_link` is "" when no report page is
    available (tracked funds switched off, or the appendix page failed to build), in
    which case the SEC link is what the row has and is shown directly rather than
    leaving the row with nothing.
    """
    if report_link and anchor:
        return _link(f"{report_link}#{anchor}", "Our report")
    if url:
        return _link(url, "Filing")
    return ""


def _link(url: str, label: str) -> str:
    """A link that still reads as a link when a client strips colour.

    Carries `class="a"` for the hoisted style, but no inline style of its own: in a
    client that drops the `<style>` block this falls back to the browser's native
    anchor rendering (blue, underlined), which is a link that is if anything *more*
    visible than the styled version, never less.
    """
    if not url:
        return ""
    return f'<a href="{_e(url)}" class="a">{_e(label)}</a>'


def _brandbar() -> str:
    """The navy wordmark strip above the photograph.

    A solid colour band rather than an image, so the publication still identifies itself
    on first open in a client that blocks remote content, which is every client by
    default and is exactly when the hero photograph will be missing.
    """
    return f"""
<tr><td style="background:{NAVY_DEEP};padding:13px 30px;" class="brand">
  <span style="font-family:{SANS};font-size:18px;font-weight:700;letter-spacing:-.02em;
               color:#FFFFFF;background:{NAVY_DEEP};">WHALE</span><span
        style="font-family:{SANS};font-size:18px;font-weight:300;letter-spacing:-.01em;
               color:#9FC0E4;background:{NAVY_DEEP};">WEEKLY</span>
</td></tr>"""


def _standfirst(on: date, cleared: int, recorded: int) -> str:
    """The dated line under the hero. The headline itself lives on the photograph block,
    so this carries only what the headline left out."""
    published = on.strftime("%a, %b %d %Y").upper()
    return f"""
<div style="background:{PAPER};">
  <div style="font-family:{SANS};font-size:11px;font-weight:700;letter-spacing:.04em;
              color:{INK_FAINT};background:{PAPER};">PUBLISHED {_e(published)}</div>
  <div style="font-family:{SERIF};font-size:17px;line-height:1.6;color:{INK_SOFT};
              background:{PAPER};padding-top:12px;">{cleared} disclosed moves cleared the
    $5M threshold this week, out of {recorded} filings recorded in the window.</div>
  <div style="height:4px;background:{NAVY};margin:20px 0 0 0;font-size:0;line-height:0;">&nbsp;</div>
</div>"""


def _cta(url: str, label: str = "Read the weekly overview") -> str:
    """The one button the reader is meant to press.

    Built as a table with a background colour on the cell rather than a styled anchor,
    because Outlook ignores padding on an inline element and would render a bare blue
    word where the button should be. The anchor still carries its own padding so the
    whole rectangle is the click target in clients that do honour it.
    """
    if not url:
        return ""
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0"
       style="border-collapse:collapse;margin:22px 0 4px 0;">
  <tr><td align="center" bgcolor="{NAVY}" style="background:{NAVY};">
    <a href="{_e(url)}" style="display:inline-block;font-family:{SANS};font-size:15px;
       font-weight:700;letter-spacing:.01em;color:#FFFFFF;text-decoration:none;
       padding:15px 30px;">{_e(label)} &rsaquo;</a>
  </td></tr>
</table>"""


def _notes(articles: list[tuple[Thesis, str]], no_thesis_line: str) -> str:
    if not articles:
        return (
            f'<div style="font-family:{SANS};font-size:14px;line-height:1.6;'
            f'color:{INK_SOFT};background:{PAPER};padding:14px 0 0 0;">'
            f"{_e(no_thesis_line)}</div>"
        )
    blocks = []
    for thesis, link in articles:
        # The filing behind the first citation, so a reader who does not want the argument
        # can still reach a primary source in one click.
        filing = next((c.url for c in thesis.citations if c.url), "")
        blocks.append(
            f"""
<div style="background:{PAPER};border-top:1px solid {RULE};padding:16px 0 0 0;margin-top:14px;">
  <div style="font-family:{SANS};font-size:20px;font-weight:700;line-height:1.22;
              letter-spacing:-.014em;color:{INK};background:{PAPER};">{_e(thesis.title)}</div>
  <div style="font-family:{SERIF};font-size:15.5px;line-height:1.62;color:{INK_SOFT};padding-top:9px;">
    {_e(thesis.claim)}
  </div>
  <div style="padding-top:11px;">
    {_link(link, "Read the note")}
    {"&nbsp;&nbsp;&nbsp;" + _link(filing, "Read the filing") if filing else ""}
  </div>
</div>"""
        )
    return "".join(blocks)


def _rule_heading(label: str, note: str = "") -> str:
    """A short navy bar over a bold uppercase label: the reference publication's
    section marker, and the thing that makes a long page navigable at a glance."""
    return (
        f'<div style="padding:30px 0 0 0;background:{PAPER};">'
        f'<div style="width:64px;height:3px;background:{NAVY};font-size:0;line-height:0;">&nbsp;</div>'
        f'<div style="font-family:{SANS};font-size:14px;font-weight:700;letter-spacing:.05em;'
        f'text-transform:uppercase;color:{INK};background:{PAPER};padding-top:9px;">{_e(label)}'
        + (
            f'<span style="font-weight:400;letter-spacing:0;text-transform:none;'
            f'font-size:12px;color:{INK_FAINT};"> &nbsp;{_e(note)}</span>'
            if note
            else ""
        )
        + "</div></div>"
    )


def _section_table(section: Section, limit: int | None = None, report_link: str = "") -> str:
    """One category. Renders its explanation rather than nothing when it is empty.

    `limit`, when given, shows only the first `limit` rows (already ranked by
    disclosed size) and appends a plain-text count of how many more exist. Nothing
    is dropped -- the full section is always available in the report page, which
    `report_link` (when given) also lets every shown row link into at its own anchor.
    """
    from whale_agent.publishing.tracked_filings_html import item_anchor, section_anchor

    shown_count = (
        f"{min(limit, section.count)} of {section.count} shown"
        if limit
        else (f"{section.count} shown" if section.rows else "")
    )
    header = _rule_heading(section.label, shown_count)

    if not section.rows:
        return header + (
            f'<div style="font-family:{SANS};font-size:13px;line-height:1.6;'
            f"color:{INK_FAINT};background:{PAPER};border-top:1px solid {RULE};"
            f'padding:11px 0 0 0;">{_e(section.empty_note)}</div>'
        )

    rows_to_show = section.rows[:limit] if limit else section.rows
    rows = []
    for row in rows_to_show:
        # An estimated figure is marked in the cell, not in a footnote nobody reaches.
        estimate = '<span class="estf"> est.</span>' if row.is_estimate else ""
        anchor = item_anchor(url=row.filing_url) if row.filing_url else ""
        id_attr = f' id="{_e(anchor)}"' if anchor else ""
        rows.append(
            f"""
<tr{id_attr}>
  <td class="cell rt">
    {_e(row.filer)}{f'<div class="subn">{_e(row.filer_role)}</div>' if row.filer_role else ""}
    <div class="cell-sub subf">{_e(row.what)}
      &middot; {_e(row.when.isoformat())}</div>
  </td>
  <td class="cell rt2">
    {_e(row.issuer)}{f' <span class="tick tickf">({_e(row.ticker)})</span>' if row.ticker else ""}
  </td>
  <td align="right" class="amt rt3">{_e(row.amount_label)}{estimate}</td>
  <td align="right" class="rt4">{_row_links(row.filing_url, report_link, anchor)}</td>
</tr>"""
        )

    total = (
        f'<div style="font-family:{SANS};font-size:11.5px;color:{INK_FAINT};'
        f'background:{PAPER};padding:7px 0 0 0;">'
        f"{section.priced_count} of {section.count} carry a disclosed figure."
        "</div>"
        if section.priced_count < section.count
        else ""
    )
    section_more_link = (
        _link(f"{report_link}#{section_anchor(section.label)}", "See the full report")
        if report_link
        else "See the full report"
    )
    more = (
        f'<div style="font-family:{SANS};font-size:12px;color:{INK_FAINT};'
        f'background:{PAPER};padding:6px 0 0 0;">'
        f"{section.count - limit} more {_e(section.label.lower())} this week. "
        f"{section_more_link}.</div>"
        if limit and section.count > limit
        else ""
    )
    return (
        header + f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="border-collapse:collapse;background:{PAPER};">'
        + "".join(rows)
        + "</table>"
        + total
        + more
    )


def _macro_block(lines: list[str], sourcing: str) -> str:
    """Rates and credit, or an honest statement that we could not read them.

    Shown even when empty for the same reason the overview sections are: a missing block
    reads as a quiet quarter, and the actual reason is usually that a key is not set.
    """
    if not lines:
        body = (
            f'<div style="font-family:{SANS};font-size:13px;line-height:1.65;color:{INK_FAINT};'
            f'background:{PAPER};">No macro series were readable this run. Rates come from '
            f"Treasury's keyless endpoint; bank credit and payrolls need a FRED key.</div>"
        )
    else:
        body = "".join(
            f'<div style="font-family:{SANS};font-size:13.5px;line-height:1.7;color:{INK};'
            f'background:{PAPER};border-top:1px solid {RULE};padding:9px 0;">{_e(line)}</div>'
            for line in lines
        )
    return f"""
{_rule_heading("Rates, credit and macro")}
<div style="font-family:{SANS};font-size:12.5px;color:{INK_FAINT};background:{PAPER};
            padding:6px 0 8px 0;">Background for the filings above. Context, not cause.</div>
{body}
<div style="font-family:{SANS};font-size:11.5px;line-height:1.6;color:{INK_FAINT};
            background:{PAPER};padding:9px 0 0 0;">{_e(sourcing)}</div>"""


def _fund_card(snap) -> str:
    """A reference row: who is watched, how large, and where the last full filing is.

    The quarterly holdings and the quarter-over-quarter diff used to live here. They were
    cut on the reader's instruction: a 13F describes a book months ago, and what he wants
    is what has happened since, which the sections above carry.
    """
    from whale_agent.summarization.tracked_funds import format_usd_short, option_note

    as_of = snap.period_end.isoformat() if snap.period_end else "date unknown"
    scaled = (
        '<span class="tickf"> &middot; reported in thousands</span>'
        if snap.values_scaled_from_thousands
        else ""
    )
    # Options get their own faint line rather than a share of the bolded number on the
    # right, which is stock only. See `option_note` for why the wording is what it is.
    note = option_note(snap)
    option_line = f'<div class="subf">{_e(note)}</div>' if note else ""
    return f"""
<tr>
  <td class="rt">{_e(snap.name)}
    <div class="subf">
      {snap.position_count} stock positions &middot; as of {_e(as_of)}{scaled}</div>
    {option_line}
  </td>
  <td align="right" class="rt3">{_e(format_usd_short(snap.total_value_usd))}
    <div class="stockof">in stock</div></td>
  <td align="right" class="rt4">{_link(snap.latest_13f_url, "13F")}</td>
</tr>"""


def _tracked_funds_block(
    snapshots: list, limit: int | None = None, report_link: str = ""
) -> str:
    """The roster. One row per manager, no holdings, one link.

    `limit`, when given, shows only the `limit` largest managers by disclosed
    portfolio value and appends a count of how many more are tracked -- the full
    roster is never dropped, only moved to the attached report. `report_link`, when
    given, links that count straight to the roster section of the report page
    (`#sec-roster`) rather than leaving it as unreachable plain text.
    """
    usable = [s for s in snapshots if s.error is None and s.has_13f]
    if not usable:
        return ""
    shown = (
        sorted(usable, key=lambda s: s.total_value_usd, reverse=True)[:limit]
        if limit
        else usable
    )
    rows = "".join(_fund_card(s) for s in shown)
    roster_link = (
        _link(f"{report_link}#sec-roster", "Full roster in the report")
        if report_link
        else "Full roster in the report"
    )
    more = (
        f'<div style="font-family:{SANS};font-size:12px;color:{INK_FAINT};'
        f'background:{PAPER};padding:6px 0 0 0;">'
        f"{len(usable) - limit} more tracked managers. {roster_link}.</div>"
        if limit and len(usable) > limit
        else ""
    )
    return (
        _rule_heading("Tracked managers", "last full 13F, for reference")
        + f'<div style="font-family:{SANS};font-size:12.5px;line-height:1.6;'
        f'color:{INK_FAINT};background:{PAPER};padding:6px 0 2px 0;">'
        f"Stock positions as of the date shown, which is a quarter end and not this "
        f"week. Option positions, which a 13F reports at the value of the underlying, "
        f"are stated separately.</div>"
        + f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="border-collapse:collapse;background:{PAPER};">{rows}</table>'
        + more
    )


def _resale_block(
    registrations: list, on, since, report_link: str = "", limit: int | None = None
) -> str:
    """Funds arranging to sell. Rendered even when empty, like every other section.

    `limit`, when given, shows only the first `limit` registrations and appends a
    count of the rest, the same pattern every other capped section in this module
    uses -- nothing is dropped, only moved to the report page, which always renders
    the full list (see `render_tracked_filings_page`).
    """
    from whale_agent.publishing.tracked_filings_html import item_anchor

    header = _rule_heading("Arranging to sell", "registration statements")
    intro = (
        f'<div style="font-family:{SANS};font-size:12.5px;line-height:1.6;'
        f'color:{INK_FAINT};background:{PAPER};padding:6px 0 2px 0;">'
        f"Registrations between {_e(since.isoformat())} and {_e(on.isoformat())} naming "
        f"a tracked fund as a selling securityholder. Registering shares is the step "
        f"before selling them, and it runs weeks ahead of any Form 4.</div>"
    )
    if not registrations:
        return (
            header
            + intro
            + (
                f'<div style="font-family:{SANS};font-size:13px;color:{INK_SOFT};'
                f'background:{PAPER};border-top:1px solid {RULE};padding:11px 0 0 0;">'
                f"No tracked fund was named in a registration this window.</div>"
            )
        )

    def _reg_row(r) -> str:
        anchor = item_anchor(accession=getattr(r, "accession", ""), url=r.url)
        id_attr = f' id="{_e(anchor)}"' if anchor else ""
        return f"""
<tr{id_attr}>
  <td class="rt rtb">{_e(r.whale_name)}
    <div class="subf">
      {_e(r.form)} &middot; {_e(r.filed.isoformat())}</div></td>
  <td class="rt2">
    {_e(r.describe())}</td>
  <td align="right" class="rt4">{_row_links(r.url, report_link, anchor)}</td>
</tr>"""

    shown = registrations[:limit] if limit else registrations
    rows = "".join(_reg_row(r) for r in shown)
    resale_more_link = (
        _link(f"{report_link}#sec-resale", "See the full report")
        if report_link
        else "See the full report"
    )
    more = (
        f'<div style="font-family:{SANS};font-size:12px;color:{INK_FAINT};'
        f'background:{PAPER};padding:6px 0 0 0;">'
        f"{len(registrations) - limit} more registration(s) this week. "
        f"{resale_more_link}.</div>"
        if limit and len(registrations) > limit
        else ""
    )
    return (
        header
        + intro
        + f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="border-collapse:collapse;background:{PAPER};">{rows}</table>'
        + more
    )


def _market_stakes_block(
    stakes: list,
    on,
    since,
    limit: int | None = None,
    examined: int = 0,
    report_link: str = "",
) -> str:
    """Market-wide 5% crossings. Independent of who happens to be on the watchlist."""
    from whale_agent.publishing.tracked_filings_html import item_anchor

    header = _rule_heading("Big stakes disclosed this week", "market-wide")
    # Says how many filings the ranking covers rather than asserting completeness. The
    # plaintext render was corrected first and this block kept the older, false claim,
    # which is the copy that actually reaches the reader.
    scope = (
        f"All {examined:,} 13D/G filings EDGAR indexed between "
        f"{_e(since.isoformat())} and {_e(on.isoformat())}"
        if examined
        else f"13D/G filings between {_e(since.isoformat())} and {_e(on.isoformat())}"
    )
    intro = (
        f'<div style="font-family:{SANS};font-size:12.5px;line-height:1.6;'
        f'color:{INK_FAINT};background:{PAPER};padding:6px 0 2px 0;">'
        f"{scope}, "
        f"ranked by size of the stake. Anyone crossing 5% must file within days.</div>"
    )
    if not stakes:
        return (
            header
            + intro
            + (
                f'<div style="font-family:{SANS};font-size:13px;color:{INK_SOFT};'
                f'background:{PAPER};border-top:1px solid {RULE};padding:11px 0 0 0;">'
                f"Nobody crossed 5% in this window.</div>"
            )
        )

    def _stake_row(s) -> str:
        anchor = item_anchor(accession=getattr(s, "accession", ""), url=s.url)
        id_attr = f' id="{_e(anchor)}"' if anchor else ""
        return f"""
<tr{id_attr}>
  <td class="rt">
    {_e(s.holders[0] if s.holders else "undisclosed filer")}
    <div class="subf">{_e(s.form)}
      &middot; {_e(s.filed.isoformat())}</div>
  </td>
  <td class="rt2">
    {_e(s.detail.issuer)}</td>
  <td align="right" class="rt3">{_e(f"{s.detail.percent:g}%") if s.detail.percent is not None else ""}</td>
  <td align="right" class="rt4">{_row_links(s.url, report_link, anchor)}</td>
</tr>"""

    def _rows_html(items: list) -> str:
        return "".join(_stake_row(s) for s in items)

    def table(items: list) -> str:
        return (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="border-collapse:collapse;background:{PAPER};">'
            f"{_rows_html(items)}</table>"
        )

    def subheading(text: str) -> str:
        return (
            f'<div style="font-family:{SANS};font-size:11.5px;letter-spacing:.08em;'
            f"text-transform:uppercase;color:{INK_FAINT};background:{PAPER};"
            f'padding:14px 0 2px 0;">{_e(text)}</div>'
        )

    # Same split as the plaintext render: control blocks are not positions, and mixing
    # them into one ranking buries the fund rows under founders and parent companies.
    def by_size(items: list) -> list:
        return sorted(items, key=lambda s: s.detail.percent or 0, reverse=True)

    funds = by_size([s for s in stakes if s.detail.is_institutional])
    others = by_size([s for s in stakes if not s.detail.is_institutional])

    body = ""
    if funds:
        body += subheading("Institutions") + table(
            funds[:limit] if limit is not None else funds
        )
    if others:
        others_limit = max(3, limit // 2) if limit is not None else None
        body += subheading(
            "Other filers: individuals, parents, and holders the filing does not classify"
        ) + table(others[:others_limit] if others_limit is not None else others)
    return header + intro + body


def _quarterly_13f_block(
    filings: list, report_link: str = "", limit: int = EMAIL_QUARTERLY_LIMIT
) -> str:
    """The 13F headline: which tracked managers just filed a quarterly information
    table, and what moved -- leading the email because a full-portfolio disclosure
    from a tracked manager is the single highest-signal event this product covers,
    and it went unmentioned in the email that shipped the week 35 managers filed.

    Ranked by `quarterly_filings.significance_pct` -- the largest single position
    move as a percent of the manager's OWN current 13F portfolio, the same
    "size against its own book" basis `_top_trades_block` already ranks trades on --
    not alphabetically and not by raw AUM. A manager with no prior period on file to
    diff against is still shown (the filing itself is the headline event), just
    without a sized move, and is never described as having filed a "new" portfolio.
    """
    from whale_agent.publishing.tracked_filings_html import filing_anchor
    from whale_agent.summarization.tracked_funds import format_usd_short

    if not filings:
        return ""

    header = _rule_heading("13F filings this week", "quarterly portfolios just disclosed")
    intro = (
        f'<div style="font-family:{SANS};font-size:12.5px;line-height:1.6;'
        f'color:{INK_FAINT};background:{PAPER};padding:6px 0 2px 0;">'
        f"Ranked by the largest single position move as a percent of the manager's "
        f"own 13F portfolio, not by raw AUM. The dollar figure on the right is total "
        f"disclosed value, stock plus option notional; a manager whose stock/options "
        f"mix shifted between quarters is marked rather than given a stock-only "
        f"percentage that would misstate the change. Each is a quarter-end snapshot, "
        f"filed up to 45 days later. See the linked filing for the as-of "
        f"date.</div>"
    )

    def _move_label(fc) -> str:
        if not fc.has_prior or fc.changes is None:
            return "no prior period on file to compare against"
        pieces = []
        for h in fc.changes.opened[:EMAIL_QUARTERLY_MOVES_PER_MANAGER]:
            pieces.append(f"opened {_e(h.issuer)}")
        for h in fc.changes.closed[:EMAIL_QUARTERLY_MOVES_PER_MANAGER]:
            pieces.append(f"no longer reports {_e(h.issuer)}")
        for issuer, _v in fc.changes.increased[:EMAIL_QUARTERLY_MOVES_PER_MANAGER]:
            pieces.append(f"added to {_e(issuer)}")
        for issuer, _v in fc.changes.decreased[:EMAIL_QUARTERLY_MOVES_PER_MANAGER]:
            pieces.append(f"trimmed {_e(issuer)}")
        return (
            ", ".join(pieces[:EMAIL_QUARTERLY_MOVES_PER_MANAGER])
            or "no position moves reported"
        )

    def _row(fc) -> str:
        anchor = filing_anchor(fc.accession)
        link = _row_links(fc.source_url, report_link, anchor)
        delta = ""
        if fc.has_prior and fc.prior_disclosed_total_usd is not None:
            if fc.composition_shifted:
                # A stock-only delta is not shown here: the options share of the book
                # moved enough between quarters that a single percentage would say
                # something the filing does not (see FilingChange.composition_shifted
                # and the report page, which spells out both quarters' mix). The
                # disclosed-total figure below (stock + option notional) is still
                # honest, since it does not depend on the mix -- only this delta line
                # is withheld.
                delta = '<div class="subf">stock/options mix shifted, see report</div>'
            else:
                disclosed_delta = fc.current_disclosed_total_usd - fc.prior_disclosed_total_usd
                sign = "+" if disclosed_delta >= 0 else "-"
                delta = (
                    f'<div class="subf">{sign}{format_usd_short(abs(disclosed_delta))} '
                    "vs prior quarter</div>"
                )
        return f"""
<tr>
  <td class="rt">
    {_e(fc.manager_name)}
    <div class="subf">13F-HR &middot; {_e(fc.filed_date.isoformat())}
      &middot; period {_e(fc.report_period.isoformat())}</div>
  </td>
  <td class="rt2">{_move_label(fc)}{delta}</td>
  <td align="right" class="rt3">{_e(format_usd_short(fc.current_disclosed_total_usd))}</td>
  <td align="right" class="rt4w">{link}</td>
</tr>"""

    rows = "".join(_row(fc) for fc in filings[:limit])
    table = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="border-collapse:collapse;background:{PAPER};">{rows}</table>'
    )
    return header + intro + table


def _top_trades_block(trades: list, stakes: list, on, report_link: str = "") -> str:
    """The 5 largest trades, pooled across every tracked manager.

    One link per row: our own writeup, deep-linked to the anchor for that exact
    filing, when a report page is available; the raw SEC filing otherwise. See
    `_row_links` for why this is one link and not two -- the SEC citation is still on
    the row, just on the report page's copy of it rather than paid for twice.
    """
    from whale_agent.ingestion.fund_watchlist import filing_index_url  # noqa: F401
    from whale_agent.publishing.tracked_filings_html import filing_anchor
    from whale_agent.summarization.tracked_funds import format_usd_short

    header = _rule_heading("The largest trades", "pooled across tracked managers")
    intro = (
        f'<div style="font-family:{SANS};font-size:12.5px;line-height:1.6;'
        f'color:{INK_FAINT};background:{PAPER};padding:6px 0 2px 0;">'
        f"Ranked by trade size relative to the manager's own disclosed 13F portfolio "
        f"value, not raw dollars. One row per filing, one row per manager. Limited to "
        f"open-market purchases and sales; grants, tax withholding and other "
        f"non-discretionary transactions are excluded.</div>"
    )

    def links(accession: str, url: str) -> str:
        anchor = filing_anchor(accession) if accession else ""
        return _row_links(url, report_link, anchor)

    ranked = [t for t in trades if t.percent_of_portfolio is not None]
    unrankable = [t for t in trades if t.percent_of_portfolio is None]

    def _trade_row(t, show_percent: bool) -> str:
        pct = (
            f'<div class="subf">{t.percent_of_portfolio:.1f}% of its 13F portfolio</div>'
            if show_percent
            else ""
        )
        return f"""
<tr>
  <td class="rt">
    {_e(t.manager)} {_e(t.direction)}
    <div class="subf">{_e(t.form)}
      &middot; {_e(t.filed.isoformat())}</div>
  </td>
  <td class="rt2">
    {t.shares:,} shares of {_e(t.issuer)} ({_e(t.symbol)}) at ${t.price:,.2f}{pct}</td>
  <td align="right" class="rt3">{_e(format_usd_short(int(t.dollar_value)))}</td>
  <td align="right" class="rt4w">{links(t.accession, t.url)}</td>
</tr>"""

    if not ranked:
        trade_body = (
            f'<div style="font-family:{SANS};font-size:13px;color:{INK_SOFT};'
            f'background:{PAPER};border-top:1px solid {RULE};padding:11px 0 0 0;">'
            f"No open-market trade by a tracked manager this window.</div>"
        )
    else:
        rows = "".join(_trade_row(t, True) for t in ranked)
        trade_body = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="border-collapse:collapse;background:{PAPER};">{rows}</table>'
        )

    unrankable_body = ""
    if unrankable:
        rows = "".join(_trade_row(t, False) for t in unrankable)
        unrankable_body = (
            f'<div style="font-family:{SANS};font-size:11.5px;letter-spacing:.08em;'
            f"text-transform:uppercase;color:{INK_FAINT};background:{PAPER};"
            f'padding:14px 0 2px 0;">Other large trades, no 13F on file to rank against'
            f"</div>"
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="border-collapse:collapse;background:{PAPER};">{rows}</table>'
        )

    stake_heading = (
        f'<div style="font-family:{SANS};font-size:11.5px;letter-spacing:.08em;'
        f"text-transform:uppercase;color:{INK_FAINT};background:{PAPER};"
        f'padding:14px 0 2px 0;">Largest new or amended stakes, by percent of class '
        f"No dollar value stated: 13D/G filings carry no price</div>"
    )
    if not stakes:
        stake_body = (
            f'<div style="font-family:{SANS};font-size:13px;color:{INK_SOFT};'
            f'background:{PAPER};padding:6px 0 0 0;">'
            f"No new or amended stake by a tracked manager this window.</div>"
        )
    else:
        rows = "".join(
            f"""
<tr>
  <td class="rt">
    {_e(s.manager)}
    <div class="subf">{_e(s.form)}
      &middot; {_e(s.filed.isoformat())}</div>
  </td>
  <td class="rt2">
    {_e("new position" if s.is_new else "amended stake")} in {_e(s.issuer)}
    {_e(f", {s.shares:,} shares") if s.shares else ""}</td>
  <td align="right" class="rt3">{_e(f"{s.percent:g}%")}</td>
  <td align="right" class="rt4w">{links(s.accession, s.url)}</td>
</tr>"""
            for s in stakes
        )
        stake_body = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="border-collapse:collapse;background:{PAPER};">{rows}</table>'
        )
    return header + intro + trade_body + unrankable_body + stake_heading + stake_body


def _fund_moves_block(
    snapshots: list,
    on,
    since,
    limit: int | None = None,
    report_link: str = "",
    filings_per_manager: int | None = None,
) -> str:
    """Stake filings by tracked managers inside the window.

    Above the standing positions, because this is the part that is news. Rendered even
    when empty: a section that disappears on a quiet week is indistinguishable from one
    that broke.

    `limit`, when given, details only the first `limit` managers (ranked by number of
    filings this window) and appends a count of the rest. `filings_per_manager`, when
    given, further caps how many of one manager's own filings are shown, for the
    manager who filed a dozen times in a quiet week for everyone else. Both are moves,
    not drops: the report page always renders every manager's full filing list.
    """
    moved = [s for s in snapshots if s.error is None and s.recent_filings]
    moved.sort(key=lambda s: len(s.recent_filings), reverse=True)
    shown = moved[:limit] if limit else moved
    header = _rule_heading("What the tracked managers filed", "this week")
    intro = (
        f'<div style="font-family:{SANS};font-size:12.5px;line-height:1.6;'
        f'color:{INK_FAINT};background:{PAPER};padding:6px 0 2px 0;">'
        f"Stake disclosures between {_e(since.isoformat())} and {_e(on.isoformat())}. "
        f"These run on a days-long clock, unlike the quarterly positions below.</div>"
    )
    if not moved:
        return (
            header
            + intro
            + (
                f'<div style="font-family:{SANS};font-size:13px;color:{INK_SOFT};'
                f'background:{PAPER};border-top:1px solid {RULE};padding:11px 0 0 0;">'
                f"No tracked manager filed a stake disclosure in this window.</div>"
            )
        )
    from whale_agent.publishing.tracked_filings_html import item_anchor, manager_anchor

    blocks = []
    for snap in shown:

        def _filing_rows(f) -> str:
            anchor = item_anchor(accession=f.accession, url=f.url)
            id_attr = f' id="{_e(anchor)}"' if anchor else ""
            return f"""
<tr{id_attr}>
  <td class="mvform">{_e(f.form)}</td>
  <td class="mvdesc">{_e(f.detail.describe()) if f.detail and f.detail.describe() else ""}
    <div class="mvdate">{_e(f.filed.isoformat())}
      &middot; {(on - f.filed).days} days ago</div></td>
  <td align="right" class="mvlink">{_row_links(f.url, report_link, anchor)}</td>
</tr>""" + "".join(
                f"""
<tr><td colspan="3" class="mvtrade">{_e(tr.describe())}</td></tr>"""
                for tr in f.trades[:4]
            )

        manager_filings = (
            snap.recent_filings[:filings_per_manager]
            if filings_per_manager
            else snap.recent_filings
        )
        rows = "".join(_filing_rows(f) for f in manager_filings)
        manager_more_link = (
            _link(f"{report_link}#{manager_anchor(snap.name)}", "See the full report")
            if report_link
            else "See the full report"
        )
        manager_more = (
            f'<div class="subf" style="padding:4px 0 0 0;">'
            f"{len(snap.recent_filings) - filings_per_manager} more filing(s) from "
            f"{_e(snap.name)} this week. {manager_more_link}.</div>"
            if filings_per_manager and len(snap.recent_filings) > filings_per_manager
            else ""
        )
        blocks.append(
            f"""
<div style="background:{PAPER};border-top:1px solid {RULE};padding:12px 0 2px 0;">
  <div style="font-family:{SANS};font-size:15px;font-weight:700;color:{INK};
              background:{PAPER};">{_e(snap.name)}</div>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
         style="border-collapse:collapse;background:{PAPER};padding-top:4px;">{rows}</table>
  {manager_more}
</div>"""
        )
    moves_more_link = (
        _link(f"{report_link}#sec-moves", "See the full report")
        if report_link
        else "See the full report"
    )
    more = (
        f'<div style="font-family:{SANS};font-size:12px;color:{INK_FAINT};'
        f'background:{PAPER};border-top:1px solid {RULE};padding:8px 0 0 0;">'
        f"{len(moved) - limit} more tracked managers filed this week. "
        f"{moves_more_link}.</div>"
        if limit and len(moved) > limit
        else ""
    )
    return header + intro + "".join(blocks) + more


def render_weekly_html(
    on: date,
    cleared: int,
    recorded: int,
    articles: list[tuple[Thesis, str]],
    sections: list[Section],
    how_to_read: str,
    no_thesis_line: str = "",
    macro_lines: list[str] | None = None,
    macro_sourcing: str = "",
    issue_link: str = "",
    tracked_funds: list | None = None,
    market_stakes: list | None = None,
    stakes_examined: int = 0,
    resale: list | None = None,
    top_trades: list | None = None,
    top_stakes: list | None = None,
    tracked_report_link: str | dict[str, str] = "",
    quarterly_filings: list | None = None,
) -> str:
    """Assemble the weekly email.

    `how_to_read` is still accepted so callers do not have to change, and is no longer
    rendered: the long method block was cut at the owner's instruction and replaced by a
    single line saying what this is. Keeping the parameter rather than removing it means
    the text digest and the issue page can drop it on their own schedule.

    `tracked_report_link` now accepts either form. A plain string (the old form, still
    used by tests and any caller with a single flat report page) links every "Our
    report" row and every "See the full report" note to that one URL, exactly as
    before. A dict, keyed by `PAGE_SLUGS` slug ("trades", "overview", "bigstakes",
    "resale", "moves", "roster"; see tracked_filings_html.py), links each section's
    rows and overflow notes to that section's OWN page instead of a single monolithic
    page -- this is what `digest_weekly.py` passes now that the report is a multi-page
    site. A missing key (or the whole argument omitted) means "no report link for that
    section", the same as the old empty string.
    """

    def _link_for(slug: str) -> str:
        if isinstance(tracked_report_link, dict):
            return tracked_report_link.get(slug, "")
        return tracked_report_link

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Whale Weekly, {_e(on.isoformat())}</title>
<!--[if mso]><style>body,table,td,div,span,a{{font-family:'Segoe UI',Arial,sans-serif !important;}}</style><![endif]-->
<style>
  /* Row-level decoration, hoisted out of the markup. Every one of these classes used to
     be a ~130-byte `style="..."` attribute repeated on every table cell -- with 50+
     tracked managers and hundreds of filing rows, that repetition was most of the
     email's weight. None of it is structural: a client that strips this block (older
     Outlook, or Gmail's app reading a non-Gmail account) falls back to plain table
     markup with default black-on-white text, the native blue-underlined link style
     (`.a`), and the `align="right"` HTML attribute already on the numeric cells, which
     browsers honour without any CSS at all. What is lost without this block: the ink
     colour hierarchy (primary vs. soft-grey text), the hairline row rules, the compact
     row padding, right-alignment of the "more" text and small caps, and the navy/beige
     accent colours. Nothing semantic -- no content, no link, no number -- depends on it.
  */
  .a {{ font-family:{SANS};font-size:12.5px;font-weight:700;color:{LINK};
        text-decoration:none;border-bottom:1px solid {RULE_STRONG};white-space:nowrap; }}
  .rt, .rt2, .rt3 {{ font-family:{SANS};font-size:13px;background:{PAPER};
        border-top:1px solid {RULE};padding:8px 10px 8px 0;vertical-align:top; }}
  .rt {{ color:{INK}; }}
  .rtb {{ font-weight:700; }}
  .rt2 {{ color:{INK_SOFT}; }}
  .rt3 {{ font-size:13.5px;font-weight:700;color:{INK};white-space:nowrap; }}
  .rt4, .rt4w {{ background:{PAPER};border-top:1px solid {RULE};padding:8px 0;
        vertical-align:top; }}
  .rt4w {{ white-space:nowrap; }}
  .subf {{ color:{INK_FAINT};font-size:11.5px;padding-top:2px; }}
  .subn {{ color:{NAVY};font-size:11.5px;padding-top:2px; }}
  .tickf {{ color:{INK_FAINT}; }}
  .estf {{ color:{INK_FAINT};font-size:11px; }}
  .stockof {{ color:{INK_FAINT};font-size:11px;font-weight:400; }}
  .mvform {{ font-family:{SANS};font-size:13px;color:{INK};background:{PAPER};
        padding:5px 10px 5px 0;white-space:nowrap; }}
  .mvdesc {{ font-family:{SANS};font-size:12.5px;color:{INK};background:{PAPER};
        padding:5px 10px 5px 0; }}
  .mvdate {{ color:{INK_FAINT};font-size:11.5px; }}
  .mvlink {{ background:{PAPER};padding:5px 0; }}
  .mvtrade {{ font-family:{SANS};font-size:12px;color:{INK_SOFT};background:{PAPER};
        padding:0 0 5px 0; }}
  /* Nothing structural depends on the rest of this block either: clients that strip it
     get the desktop layout, which is readable on a phone, just wide. What it buys is the
     phone case. The overview is four columns of genuinely tabular data, and four columns
     at 640px do not fit a 375px screen, so on a phone the ticker column folds under the
     issuer and the horizontal padding comes off. */
  body {{ margin:0; padding:0; width:100% !important; -webkit-text-size-adjust:100%; }}
  @media only screen and (max-width:620px) {{
    .shell {{ width:100% !important; max-width:100% !important; }}
    .pad {{ padding:20px 16px 24px 16px !important; }}
    .cell {{ font-size:14px !important; padding-right:6px !important; }}
    .cell-sub {{ font-size:11px !important; }}
    /* A figure and a link on the same phone row leaves neither room, so the link moves
       under the amount rather than wrapping mid-number. */
    .amt {{ font-size:14px !important; white-space:nowrap !important; }}
    .tick {{ display:block !important; padding-top:1px !important; }}
    .lede {{ font-size:16px !important; }}
    .h1 {{ font-size:24px !important; }}
    .brand {{ padding:12px 16px !important; }}
    /* The hero headline is set for a 640px column, so it needs to come down on a
       phone or it wraps to five lines before the reader has seen anything else. */
    .hero-h {{ font-size:25px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{PAGE};">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:{PAGE};"><tr><td align="center" style="padding:26px 12px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640"
       class="shell" style="width:640px;max-width:640px;background:{PAPER};">
{_brandbar()}
<tr><td style="padding:0;background:{PAPER};font-size:0;line-height:0;">
{hero_block(f"{cleared} disclosed moves cleared $5M this week", "Weekly", HERO_URL)}
</td></tr>
<tr><td class="pad" style="padding:26px 32px 34px 32px;background:{PAPER};">

{_standfirst(on, cleared, recorded)}
{_cta(issue_link or (articles[0][1] if articles else ""))}

{_quarterly_13f_block(quarterly_filings or [], _link_for("trades")) if quarterly_filings else ""}

{_rule_heading("Research notes")}
{_notes(articles, no_thesis_line)}

{_top_trades_block(top_trades or [], top_stakes or [], on, _link_for("trades")) if (top_trades is not None or top_stakes is not None) else ""}

{_market_stakes_block(market_stakes, on, on - timedelta(days=7), limit=10, examined=stakes_examined, report_link=_link_for("bigstakes")) if market_stakes is not None else ""}

{_resale_block(resale or [], on, on - timedelta(days=7), report_link=_link_for("resale"), limit=EMAIL_RESALE_LIMIT) if resale is not None else ""}

{_fund_moves_block(tracked_funds or [], on, on - timedelta(days=7), limit=EMAIL_MOVERS_LIMIT, report_link=_link_for("moves"), filings_per_manager=EMAIL_FILINGS_PER_MANAGER_LIMIT) if tracked_funds else ""}

{_rule_heading("The last fortnight")}
<div style="font-family:{SANS};font-size:12.5px;color:{INK_FAINT};background:{PAPER};
            padding:6px 0 0 0;">Every category, ranked by disclosed size. Empty sections are
  reported, not hidden.</div>
{"".join(_section_table(s, limit=EMAIL_SECTION_ROW_LIMIT, report_link=_link_for("overview")) for s in sections)}

{_tracked_funds_block(tracked_funds or [], limit=EMAIL_ROSTER_LIMIT, report_link=_link_for("roster"))}

{_macro_block(macro_lines or [], macro_sourcing)}

<div style="border-top:2px solid {NAVY};margin:34px 0 0 0;font-size:0;line-height:0;">&nbsp;</div>
<div style="font-family:{SANS};font-size:12px;line-height:1.65;color:{INK_FAINT};
            background:{PAPER};padding:14px 0 0 0;">Every figure is copied from a public
  filing or computed from filing fields, and each row links to its source. Awareness tool,
  not investment advice.</div>

</td></tr></table>
</td></tr></table>
</body></html>"""
