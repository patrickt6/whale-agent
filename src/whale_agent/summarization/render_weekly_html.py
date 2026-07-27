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
except tables, and the fortnight view is genuinely tabular data. The layout is inlined
per-element with no reliance on the `<style>` block, which several clients strip.
"""

from __future__ import annotations

import html
from datetime import date

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


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _link(url: str, label: str) -> str:
    """A link that still reads as a link when a client strips colour."""
    if not url:
        return ""
    return (
        f'<a href="{_e(url)}" style="font-family:{SANS};font-size:12.5px;font-weight:700;color:{LINK};'
        f'text-decoration:none;border-bottom:1px solid {RULE_STRONG};white-space:nowrap;"'
        f">{_e(label)}</a>"
    )


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


def _section_table(section: Section) -> str:
    """One category. Renders its explanation rather than nothing when it is empty."""
    header = _rule_heading(section.label, f"{section.count} shown" if section.rows else "")

    if not section.rows:
        return header + (
            f'<div style="font-family:{SANS};font-size:13px;line-height:1.6;'
            f"color:{INK_FAINT};background:{PAPER};border-top:1px solid {RULE};"
            f'padding:11px 0 0 0;">{_e(section.empty_note)}</div>'
        )

    rows = []
    for row in section.rows:
        # An estimated figure is marked in the cell, not in a footnote nobody reaches.
        estimate = (
            f'<span style="color:{INK_FAINT};font-size:11px;"> est.</span>'
            if row.is_estimate
            else ""
        )
        rows.append(
            f"""
<tr>
  <td class="cell" style="font-family:{SANS};font-size:13px;color:{INK};background:{PAPER};
             border-top:1px solid {RULE};padding:9px 10px 9px 0;vertical-align:top;">
    {_e(row.filer)}{f'<div style="color:{NAVY};font-size:11.5px;padding-top:2px;">{_e(row.filer_role)}</div>' if row.filer_role else ""}
    <div class="cell-sub" style="color:{INK_FAINT};font-size:11.5px;padding-top:2px;">{_e(row.what)}
      &middot; {_e(row.when.isoformat())}</div>
  </td>
  <td class="cell" style="font-family:{SANS};font-size:13px;color:{INK_SOFT};background:{PAPER};
             border-top:1px solid {RULE};padding:9px 10px 9px 0;vertical-align:top;">
    {_e(row.issuer)}{f' <span class="tick" style="color:{INK_FAINT};">({_e(row.ticker)})</span>' if row.ticker else ""}
  </td>
  <td align="right" class="amt" style="font-family:{SANS};font-size:13.5px;color:{INK};
             background:{PAPER};border-top:1px solid {RULE};padding:9px 10px 9px 0;
             white-space:nowrap;vertical-align:top;">{_e(row.amount_label)}{estimate}</td>
  <td align="right" style="background:{PAPER};border-top:1px solid {RULE};
             padding:9px 0;vertical-align:top;">{_link(row.filing_url, "Filing")}</td>
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
    return (
        header + f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="border-collapse:collapse;background:{PAPER};">'
        + "".join(rows)
        + "</table>"
        + total
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
) -> str:
    """Assemble the weekly email.

    `how_to_read` is still accepted so callers do not have to change, and is no longer
    rendered: the long method block was cut at the owner's instruction and replaced by a
    single line saying what this is. Keeping the parameter rather than removing it means
    the text digest and the issue page can drop it on their own schedule.
    """
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Whale Weekly, {_e(on.isoformat())}</title>
<!--[if mso]><style>body,table,td,div,span,a{{font-family:'Segoe UI',Arial,sans-serif !important;}}</style><![endif]-->
<style>
  /* Nothing structural depends on this block: clients that strip it get the desktop
     layout, which is readable on a phone, just wide. What it buys is the phone case.
     The overview is four columns of genuinely tabular data, and four columns at 640px
     do not fit a 375px screen, so on a phone the ticker column folds under the issuer
     and the horizontal padding comes off. */
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

{_rule_heading("Research notes")}
{_notes(articles, no_thesis_line)}

{_rule_heading("The last fortnight")}
<div style="font-family:{SANS};font-size:12.5px;color:{INK_FAINT};background:{PAPER};
            padding:6px 0 0 0;">Every category, ranked by disclosed size. Empty sections are
  reported, not hidden.</div>
{"".join(_section_table(s) for s in sections)}

{_macro_block(macro_lines or [], macro_sourcing)}

<div style="border-top:2px solid {NAVY};margin:34px 0 0 0;font-size:0;line-height:0;">&nbsp;</div>
<div style="font-family:{SANS};font-size:12px;line-height:1.65;color:{INK_FAINT};
            background:{PAPER};padding:14px 0 0 0;">Every figure is copied from a public
  filing or computed from filing fields, and each row links to its source. Awareness tool,
  not investment advice.</div>

</td></tr></table>
</td></tr></table>
</body></html>"""
