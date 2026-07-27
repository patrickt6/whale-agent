"""The weekly issue as one page, set the way the reference publications set theirs.

The typography is the whole brief, so it is worth writing down rather than leaving in the
numbers.

**A heavy grotesque headline, underlined, over a serif body.** That pairing is what the
reference publication does and it is most of why it reads as writing rather than as a
report. The headline carries the underline the reference uses; the deck under it is grey
sans; body copy is a serif at 20px with a 1.75 line height and a 46em measure, which is
the width long prose wants and is far narrower than a data table wants.

**Serif choice.** No web font can be loaded, because a page has to stay one file that
survives being saved or mailed. The stack leads with Iowan Old Style and Charter, which
are present on macOS and are close to the reference's book serif, then falls back through
Palatino to Georgia. The reader is on a Mac, so in practice he sees the first one.

**White page, black text.** Stated because the temptation with a heavy headline is to
reverse it out of a dark block, and the owner ruled that out.

Tables inside a serif article are a different problem from tables in an email: here they
can be wider than the reading measure, so they break out of it deliberately and sit in
their own full-width band. A number the reader is meant to scan should not be trapped in a
column sized for prose.
"""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

from whale_agent.publishing.charts import choose_chart_kind, render_chart_svg
from whale_agent.summarization.issue import Issue, IssueSection
from whale_agent.summarization.render_html import (
    ACCUMULATE,
    DISTRIBUTE,
    INK,
    INK_FAINT,
    INK_SOFT,
    LINK,
    NAVY,
    PAPER,
    RULE,
    hero_data_uri,
)

__all__ = ["render_issue_html", "write_issue", "issue_filename"]

# Present on macOS, close to the reference's book serif, and degrading through faces that
# exist everywhere else. Nothing is fetched.
BODY_SERIF = (
    "'Iowan Old Style','Charter','Palatino Linotype',Palatino,'Book Antiqua',"
    "Georgia,'Times New Roman',serif"
)
DISPLAY_SANS = "'Helvetica Neue',Helvetica,'Segoe UI',Roboto,Arial,sans-serif"

MEASURE = "46em"  # the width prose wants
FRAME = "1040px"  # the width a table is allowed to use


def _e(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def issue_filename(on: date) -> str:
    return f"whale-weekly-{on.isoformat()}.html"


def _para(text: str) -> str:
    return (
        f'<p style="font-family:{BODY_SERIF};font-size:20px;line-height:1.75;color:{INK};'
        f'margin:0 0 26px 0;">{_e(text)}</p>'
    )


def _heading(section: IssueSection) -> str:
    return (
        f'<h2 id="{_e(section.anchor)}" style="font-family:{DISPLAY_SANS};font-size:30px;'
        f"line-height:1.2;font-weight:700;letter-spacing:-.018em;color:{INK};"
        f'margin:54px 0 20px 0;">{_e(section.heading)}</h2>'
    )


def _contents(issue: Issue) -> str:
    """The directory. A weekly with seven parts needs one at the top or it is not read."""
    items = "".join(
        f'<li style="font-family:{DISPLAY_SANS};font-size:16px;line-height:1.95;'
        f'color:{INK_SOFT};">'
        f'<a href="#{_e(anchor)}" style="color:{LINK};text-decoration:none;'
        f'border-bottom:1px solid {RULE};">{_e(heading)}</a></li>'
        for anchor, heading in issue.contents
    )
    return f"""
<div style="border-top:1px solid {RULE};border-bottom:1px solid {RULE};padding:22px 0;margin:34px 0 0 0;">
  <div style="font-family:{DISPLAY_SANS};font-size:12px;font-weight:700;letter-spacing:.09em;
              text-transform:uppercase;color:{INK_FAINT};padding-bottom:10px;">In this issue</div>
  <ol style="margin:0;padding-left:20px;">{items}</ol>
</div>"""


def _rows_table(section) -> str:
    """The evidence under a section, allowed to be wider than the reading measure."""
    if not section or not section.rows:
        return ""
    head = (
        "<tr>"
        + "".join(
            f'<th align="{align}" style="font-family:{DISPLAY_SANS};font-size:11px;'
            f"font-weight:700;letter-spacing:.07em;text-transform:uppercase;"
            f'color:{INK_FAINT};border-bottom:2px solid {INK};padding:0 12px 8px 0;">{label}</th>'
            for label, align in (
                ("Filer", "left"),
                ("Company", "left"),
                ("Action", "left"),
                ("Disclosed", "right"),
                ("Filing", "right"),
            )
        )
        + "</tr>"
    )
    body = ""
    for row in section.rows:
        tone = ACCUMULATE if "buy" in row.what else (DISTRIBUTE if "sell" in row.what else INK)
        role = (
            f'<div style="font-family:{DISPLAY_SANS};font-size:12px;color:{INK_FAINT};'
            f'padding-top:2px;">{_e(row.filer_role)}</div>'
            if row.filer_role
            else ""
        )
        link = (
            f'<a href="{_e(row.filing_url)}" style="font-family:{DISPLAY_SANS};font-size:13px;'
            f'color:{LINK};text-decoration:none;border-bottom:1px solid {RULE};">Read</a>'
            if row.filing_url
            else ""
        )
        body += f"""
<tr>
  <td style="font-family:{DISPLAY_SANS};font-size:14.5px;color:{INK};
             border-bottom:1px solid {RULE};padding:11px 12px 11px 0;vertical-align:top;">
    {_e(row.filer)}{role}</td>
  <td style="font-family:{DISPLAY_SANS};font-size:14.5px;color:{INK_SOFT};
             border-bottom:1px solid {RULE};padding:11px 12px 11px 0;vertical-align:top;">
    {_e(row.issuer)}</td>
  <td style="font-family:{DISPLAY_SANS};font-size:13.5px;color:{tone};
             border-bottom:1px solid {RULE};padding:11px 12px 11px 0;vertical-align:top;">
    {_e(row.what)}<div style="color:{INK_FAINT};font-size:12px;padding-top:2px;">
    {_e(row.when.isoformat())}</div></td>
  <td align="right" style="font-family:{DISPLAY_SANS};font-size:15px;font-weight:700;
             color:{INK};border-bottom:1px solid {RULE};padding:11px 12px 11px 0;
             white-space:nowrap;vertical-align:top;">{_e(row.amount_label)}{
            '<span style="font-weight:400;font-size:11px;color:'
            + INK_FAINT
            + ';"> est.</span>'
            if row.is_estimate
            else ""
        }</td>
  <td align="right" style="border-bottom:1px solid {RULE};padding:11px 0;
             vertical-align:top;">{link}</td>
</tr>"""
    return f"""
<div style="max-width:{FRAME};margin:6px auto 30px auto;overflow-x:auto;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%"
         style="border-collapse:collapse;">{head}{body}</table>
</div>"""


def _macro_lines(lines: list[str]) -> str:
    if not lines:
        return ""
    rows = "".join(
        f'<div style="font-family:{DISPLAY_SANS};font-size:16px;color:{INK};'
        f'border-bottom:1px solid {RULE};padding:12px 0;">{_e(line)}</div>'
        for line in lines
    )
    return f'<div style="margin:0 0 26px 0;border-top:2px solid {INK};">{rows}</div>'


def render_issue_html(issue: Issue, chart_events: list | None = None) -> str:
    """One page, one file. No stylesheet, no font file, no script, no remote asset."""
    chart = ""
    if chart_events:
        # Kind is chosen from the events rather than named here: passing an unknown
        # kind returns nothing, which is how this silently shipped without a chart once.
        svg = render_chart_svg(chart_events, choose_chart_kind(chart_events), title="")
        if svg:
            chart = f"""
<figure style="margin:8px 0 34px 0;max-width:{FRAME};">
  {svg}
  <figcaption style="font-family:{DISPLAY_SANS};font-size:13px;color:{INK_FAINT};
                     padding-top:10px;">The largest disclosed filings this fortnight.
    Drawn from the rows below. Filings with no disclosed value are omitted, never zeroed.</figcaption>
</figure>"""

    body = ""
    for section in issue.sections:
        body += _heading(section)
        body += f'<div style="max-width:{MEASURE};">'
        body += "".join(_para(p) for p in section.paragraphs)
        body += "</div>"
        if section.lines:
            body += _macro_lines(section.lines)
        if section.key == "insider" and chart:
            body += chart
        body += _rows_table(section.section)

    published = issue.published_on.strftime("%b %d, %Y").upper()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(issue.title)}</title>
<style>
  body {{ margin:0; padding:0; background:{PAPER}; -webkit-text-size-adjust:100%; }}
  .wrap {{ max-width:{FRAME}; margin:0 auto; padding:0 28px 90px 28px; }}
  a:hover {{ color:{NAVY}; }}
  @media only screen and (max-width:720px) {{
    .wrap {{ padding:0 18px 60px 18px; }}
    h1 {{ font-size:34px !important; }}
    h2 {{ font-size:24px !important; }}
    p  {{ font-size:18px !important; }}
  }}
</style>
</head>
<body style="background:{PAPER};">
<div class="wrap">

  <div style="font-family:{DISPLAY_SANS};font-size:12px;font-weight:700;letter-spacing:.11em;
              text-transform:uppercase;color:{INK_FAINT};padding:46px 0 0 0;">Whale Weekly</div>

  <h1 style="font-family:{DISPLAY_SANS};font-size:52px;line-height:1.1;font-weight:800;
             letter-spacing:-.03em;color:{INK};margin:14px 0 0 0;
             text-decoration:underline;text-decoration-thickness:3px;
             text-underline-offset:6px;">{_e(issue.title)}</h1>

  <p style="font-family:{DISPLAY_SANS};font-size:21px;line-height:1.45;color:{INK_FAINT};
            margin:18px 0 0 0;max-width:{MEASURE};">{_e(issue.deck)}</p>

  <div style="font-family:{DISPLAY_SANS};font-size:13px;font-weight:700;letter-spacing:.06em;
              color:{INK_SOFT};margin:26px 0 0 0;">WHALE WEEKLY</div>
  <div style="font-family:{DISPLAY_SANS};font-size:13px;letter-spacing:.06em;
              color:{INK_FAINT};margin:4px 0 0 0;">{_e(published)}</div>

  <img src="{_e(hero_data_uri())}" alt="Wall Street and Broad Street signs"
       style="display:block;width:100%;max-width:{FRAME};height:auto;margin:32px 0 0 0;" />

  {_contents(issue)}

  {body}

</div>
</body></html>"""


def write_issue(issue: Issue, out_dir: Path, chart_events: list | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / issue_filename(issue.published_on)
    path.write_text(render_issue_html(issue, chart_events), encoding="utf-8")
    return path
