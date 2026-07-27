"""One thesis as a standalone HTML page.

Deliberately the same typographic register as the email in `render_html.py`: Georgia for
display and asides, the system sans for body text, figures set in the text sans with
tabular lining numerals rather than a monospace face, sentence case throughout, hairline
rules between records. The weekly email links here, so the two are one document a reader
walks through, and a page that changed typeface would read as somebody else's website.

The layout departs from the email in one respect, and it is the argument of the whole
layer: **counter-evidence and the falsifier are set as a block in the body**, at the same
weight as the evidence, not as small print under a rule at the bottom. A note that puts
its disconfirming condition in a footer has decided the reader will not reach it.

Self-contained by construction: no stylesheet, no font file, no script, no remote image.
The header chart is inline SVG. The page can be saved, mailed as an attachment, or served
from a static directory, and it looks the same in all three.
"""

from __future__ import annotations

import html
from pathlib import Path

from whale_agent.models.event import NormalizedEvent
from whale_agent.models.thesis import Citation, Thesis
from whale_agent.publishing.charts import render_chart_svg
from whale_agent.summarization.render_html import (
    CAVEAT,
    INK,
    INK_FAINT,
    INK_SOFT,
    NAVY,
    PAGE,
    PAPER,
    RULE,
    RULE_STRONG,
    SANS,
    SERIF,
    hero_block,
    hero_data_uri,
)

__all__ = ["render_article_html", "write_article", "article_filename"]

_MEASURE = "680px"  # a Substack-width reading column, not a research-note gutter


def _e(text: object) -> str:
    """Filer and issuer names arrive from third-party feeds and are never trusted."""
    return html.escape(str(text if text is not None else ""), quote=True)


def article_filename(thesis: Thesis) -> str:
    """Slug-based and stable: regenerating the same thesis overwrites its own page."""
    return f"{thesis.slug}.html"


def _section_heading(text: str) -> str:
    return (
        f'<h2 style="font-family:{SANS};font-size:13px;font-weight:700;'
        f"letter-spacing:.08em;text-transform:uppercase;color:{NAVY};background:{PAPER};"
        f'margin:40px 0 14px 0;">{_e(text)}</h2>'
    )


def _para(text: str, colour: str = INK_SOFT) -> str:
    return (
        f'<p style="font-family:{SERIF};font-size:19px;line-height:1.72;color:{colour};'
        f'background:{PAPER};margin:0 0 24px 0;">{_e(text)}</p>'
    )


def _list(items: list[str], colour: str = INK_SOFT, bg: str = PAPER) -> str:
    if not items:
        return ""
    rows = "".join(
        f'<li style="font-family:{SANS};font-size:19px;line-height:1.7;color:{colour};'
        f'background:{bg};padding:14px 0;border-top:1px solid {RULE};">{_e(item)}</li>'
        for item in items
    )
    return (
        f'<ul style="list-style:none;margin:0 0 24px 0;padding:0;background:{bg};">{rows}</ul>'
    )


def _key_points(thesis: Thesis) -> str:
    """The reference article opens with a boxed KEY POINTS list, and it is the single
    most useful thing on the page: the reader who stops after ten seconds still leaves
    with the finding.

    Populated from the thesis evidence lines, which are built in code from the filings,
    so this box can never contain a figure the article does not support.
    """
    points = [p for p in thesis.evidence[:3] if p]
    if not points:
        return ""
    bullets = "".join(
        f'<tr><td style="background:{PAPER};padding:0 10px 12px 0;vertical-align:top;'
        f'font-family:{SANS};font-size:15px;color:{INK_SOFT};line-height:1;">&bull;</td>'
        f'<td style="background:{PAPER};padding:0 0 12px 0;font-family:{SANS};'
        f'font-size:15.5px;line-height:1.55;color:{INK};">{_e(point)}</td></tr>'
        for point in points
    )
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="border-collapse:collapse;margin:26px 0 0 0;">
<tr>
  <td width="120" style="background:{PAPER};vertical-align:top;padding:0 22px 0 0;">
    <div style="font-family:{SANS};font-size:15px;font-weight:700;letter-spacing:.01em;
                color:{NAVY};background:{PAPER};line-height:1.2;">KEY<br>POINTS</div>
  </td>
  <td style="background:{PAPER};vertical-align:top;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
           style="border-collapse:collapse;">{bullets}</table>
  </td>
</tr></table>
<div style="height:1px;background:{RULE};margin:8px 0 0 0;font-size:0;line-height:0;">&nbsp;</div>"""


def _header(thesis: Thesis, chart: str | None) -> str:
    """Masthead in the register of a wire story: eyebrow, heavy sans headline, a dated
    publication line, then the rule that separates furniture from argument."""
    chart_block = ""
    if chart:
        # The reference article captions its hero image and credits the source under it.
        # Ours is a chart of the article's own filings, so the caption says exactly that
        # and the credit names the regime rather than a photo agency.
        chart_block = f"""
<div style="background:{PAPER};margin:26px 0 0 0;">
  {chart}
  <div style="font-family:{SANS};font-size:12.5px;font-weight:700;color:{INK};
              background:{PAPER};padding-top:10px;line-height:1.45;">
    The filings this note is about, by disclosed size.</div>
  <div style="font-family:{SERIF};font-size:12.5px;font-style:italic;color:{INK_FAINT};
              background:{PAPER};padding-top:3px;">Drawn from the source filings listed
    below. No figure here was estimated.</div>
</div>
<div style="height:1px;background:{RULE};margin:22px 0 0 0;font-size:0;line-height:0;">&nbsp;</div>"""

    eyebrow = (thesis.pattern_kind or "research note").replace("_", " ")
    published = thesis.published_on.strftime("%a, %b %d %Y").upper()
    # Inlined rather than linked: an article page has to survive being saved to disk or
    # sent as an attachment, and a remote src would leave a broken box in both cases.
    hero = hero_block(thesis.title, eyebrow, hero_data_uri(), headline_size="40px")
    return f"""
{hero}
<div style="background:{PAPER};padding-top:24px;">
  <div style="font-family:{SANS};font-size:11.5px;font-weight:700;letter-spacing:.04em;
              color:{INK_FAINT};background:{PAPER};">PUBLISHED {_e(published)}</div>
  <div style="font-family:{SANS};font-size:14px;color:{INK};background:{PAPER};
              margin:10px 0 0 0;">Whale Weekly</div>
  <div style="height:4px;background:{NAVY};margin:18px 0 0 0;font-size:0;line-height:0;">&nbsp;</div>
  {_key_points(thesis)}
  {chart_block}
</div>"""


def _kind_suffix(thesis: Thesis) -> str:
    if not thesis.pattern_kind:
        return ""
    return f" · {_e(thesis.pattern_kind.replace('_', ' '))}"


def _claim(thesis: Thesis) -> str:
    return f"""
<div style="background:{PAPER};border-left:3px solid {NAVY};padding:16px 0 16px 18px;
            margin:34px 0 0 0;">
  <div style="font-family:{SANS};font-size:13px;font-weight:700;letter-spacing:.08em;
              text-transform:uppercase;color:{INK_FAINT};background:{PAPER};">The claim</div>
  <div style="font-family:{SANS};font-size:20px;line-height:1.5;font-weight:600;
              color:{INK};background:{PAPER};padding-top:6px;">{_e(thesis.claim)}</div>
</div>"""


def _challenge(thesis: Thesis) -> str:
    """Counter-evidence and falsifier, given the visual weight the argument requires."""
    counter = _list(thesis.counter_evidence, CAVEAT, bg="#FBF6E9")
    falsifier = ""
    if thesis.falsifier:
        falsifier = f"""
  <div style="background:#FBF6E9;border-top:1px solid {RULE_STRONG};margin-top:16px;
              padding-top:14px;">
    <div style="font-family:{SANS};font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{CAVEAT};background:#FBF6E9;">What would show this is wrong</div>
    <div style="font-family:{SANS};font-size:18px;line-height:1.65;font-weight:600;
                color:{CAVEAT};background:#FBF6E9;padding-top:6px;">{_e(thesis.falsifier)}</div>
  </div>"""
    return f"""
<div style="background:#FBF6E9;border-left:3px solid {NAVY};border-radius:0 4px 4px 0;
            padding:20px 22px;margin:40px 0 24px 0;">
  <div style="font-family:{SANS};font-size:13px;font-weight:700;letter-spacing:.08em;
              text-transform:uppercase;color:{NAVY};background:#FBF6E9;">The case against</div>
  <div style="background:#FBF6E9;padding-top:8px;">{counter}</div>
  {falsifier}
</div>"""


def _citation_row(citation: Citation) -> str:
    link = ""
    if citation.url:
        link = (
            f'<div style="background:{PAPER};padding-top:4px;">'
            f'<a href="{_e(citation.url)}" style="font-family:{SANS};font-size:13px;'
            f"color:{NAVY};text-decoration:none;border-bottom:1px solid {RULE_STRONG};"
            f'word-break:break-all;">{_e(citation.url)}</a></div>'
        )
    detail = ""
    if citation.detail:
        detail = (
            f'<div style="font-family:{SANS};font-size:13px;line-height:1.55;'
            f'color:{INK_FAINT};background:{PAPER};padding-top:2px;">'
            f"{_e(citation.detail)}</div>"
        )
    return f"""
<li style="background:{PAPER};border-top:1px solid {RULE};padding:12px 0;">
  <div style="font-family:{SANS};font-size:14px;color:{INK};background:{PAPER};
              font-weight:600;">{_e(citation.label)}</div>
  {detail}{link}
</li>"""


def _sourcing(thesis: Thesis) -> str:
    rows = "".join(_citation_row(c) for c in thesis.citations)
    return f"""
<div style="background:{PAPER};margin-top:44px;">
  <div style="height:1px;background:{RULE_STRONG};font-size:0;line-height:0;">&nbsp;</div>
  <h2 style="font-family:{SANS};font-size:13px;font-weight:700;letter-spacing:.08em;
             text-transform:uppercase;color:{NAVY};background:{PAPER};margin:20px 0 8px 0;">
    Sourcing</h2>
  <p style="font-family:{SANS};font-size:14px;line-height:1.6;color:{INK_SOFT};
            background:{PAPER};margin:0 0 6px 0;">
    Every figure above is copied from one of the records below or computed from them in
    code. No number on this page was written by a language model: the sentences may have
    been, the figures never are, and any sentence carrying a figure that did not trace
    back to a filing was dropped before this was published.
  </p>
  <ul style="list-style:none;margin:0;padding:0;background:{PAPER};">{rows}</ul>
</div>"""


def render_article_html(thesis: Thesis, events: list[NormalizedEvent] | None = None) -> str:
    """Render one thesis as a complete, self-contained page.

    `events` are the pattern's own supporting filings and are used only to draw the
    header chart. Passing the week rather than the pattern would put rows in the picture
    that the article is not about, so the caller passes the subset.
    """
    chart = render_chart_svg(events or [], thesis.chart_kind, title="") if events else None

    body = [_header(thesis, chart), _claim(thesis)]
    if thesis.trigger_summary:
        body.append(_section_heading("What surfaced"))
        body.append(_para(thesis.trigger_summary))
    if thesis.evidence:
        body.append(_section_heading("The filings"))
        body.append(_list(thesis.evidence))
    if thesis.context_notes:
        body.append(_section_heading("Context"))
        body.append(_list(thesis.context_notes))
    body.append(_challenge(thesis))
    if thesis.what_to_watch:
        body.append(_section_heading("What would settle it"))
        body.append(_para(thesis.what_to_watch, INK))
    body.append(_sourcing(thesis))

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{_e(thesis.title)}</title>
<style>
  body {{ margin:0; padding:0; background:{PAGE}; }}
  .sheet {{ padding:56px 48px 48px 48px; }}
  @media only screen and (max-width:640px) {{
    .sheet {{ padding:32px 20px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{PAGE};">
<div style="background:{PAGE};">
  <div class="sheet" style="max-width:{_MEASURE};margin:0 auto;background:{PAPER};">
    {"".join(body)}
    <p style="font-family:{SANS};font-size:13px;line-height:1.6;color:{INK_FAINT};
              background:{PAPER};margin:32px 0 0 0;">
      Disclosed positions above $5M. This is an awareness tool. It is not investment
      advice.
    </p>
  </div>
</div>
</body></html>"""


def write_article(
    thesis: Thesis,
    out_dir: Path,
    events: list[NormalizedEvent] | None = None,
) -> Path:
    """Write the page and return its path. Overwrites its own slug, never a sibling."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / article_filename(thesis)
    path.write_text(render_article_html(thesis, events), encoding="utf-8")
    return path
