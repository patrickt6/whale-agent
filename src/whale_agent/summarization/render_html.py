"""HTML digest render: the email as a research note.

Rendered from `NormalizedEvent` objects rather than by parsing the plain-text digest.
That distinction is what lets the email do things text cannot -- colour a buy differently
from a sell, right-align every amount so they compare at a glance, and label where each
number came from -- without inventing anything: each visual element maps to a field.

**The signature element is the provenance line.** Under every dollar amount sits a small
caps label saying whether that number was stated in the filing or estimated from a market
close. The product's whole claim is that no figure is invented; an email that prints
"$12.4M" in the same weight whether it was disclosed or inferred hides the one thing that
makes it trustworthy. So the email shows its work, on every row.

Layout follows the convention data-dense research notes converge on: a three-tier
hierarchy per record (bold name and amount; muted metadata; normal-weight prose), one
hairline rule between records, and no per-row background banding -- banding is what makes
a list of numbers read as a spreadsheet dump.

Email client constraints, which are why this looks like 2005 HTML:
- **Table skeleton.** Outlook desktop renders through the Word engine and ignores flex
  and grid entirely.
- **Every style inlined.** Many clients strip `<style>` blocks, so nothing structural may
  live in one. The block here carries only the mobile media query, which cannot be
  inlined and which those clients simply do without.
- **Explicit background *and* colour on every text element.** Gmail does not honour
  `prefers-color-scheme`; it force-inverts colours using its own algorithm, and stating
  both values is the only reliable defence against unreadable pairings.
- **Conditional MSO font stack**, because Outlook ignores webfont and system-font stacks.
- **No images.** Gmail and Outlook block them on first open, so data in an image is data
  the reader does not see.
"""

from __future__ import annotations

import base64
import html
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

from whale_agent.models.enums import PriceSource, Tier, TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.summarization.prose import DigestProse
from whale_agent.summarization.render import (
    _TIER_HEADING,
    format_usd,
    why_it_matters,
)

# -- palette -------------------------------------------------------------------
# Ink on paper, with the two colours a position can be. Deliberately not the
# saturated green/red of a broker screen: this is a research note, and a $200M
# stake is information rather than a signal to act on.
INK = "#12100E"  # near-black body ink, warmer than pure black
INK_SOFT = "#3C4043"
INK_FAINT = "#6E7378"
PAPER = "#FFFFFF"
PAGE = "#F0F1F3"
RULE = "#DEE1E4"
RULE_STRONG = "#B9BEC4"
ACCUMULATE = "#008456"  # buys, new stakes: the up colour on a market tile
DISTRIBUTE = "#CC0000"  # sells: the down colour
CAVEAT = "#7A5B12"  # skeptic notes, routine flags
NAVY = "#003468"  # masthead bar, section rules, links

# The heavier navy used for the masthead bar itself, where white type sits on it.
NAVY_DEEP = "#002B56"
# Link blue, distinct from the masthead so a link on white does not read as chrome.
LINK = "#0056B3"

# A grotesque stack for display and chrome. The reference publication sets headlines in a
# heavy proprietary grotesque; Helvetica Neue and Arial are the closest faces that are
# actually present on a reader's machine, and no web font can be loaded because the pages
# and the email must stay self-contained.
SANS = "'Helvetica Neue',Helvetica,Arial,'Segoe UI',Roboto,sans-serif"
# Body copy on article pages is a serif, which is what the reference article uses below
# its sans headline. The contrast between the two is most of the look.
SERIF = "Georgia,'Times New Roman',Times,serif"
# Figures are set in the text sans with tabular lining numerals, not a monospace face.
# Monospace reads as terminal output; research notes set money in the text face and
# rely on tabular figures for the column alignment. Georgia is unsuitable for currency
# because its numerals are old-style, so they hop above and below the baseline.
FIGURE = f"{SANS};font-variant-numeric:tabular-nums lining-nums"


_ACCUMULATION = {
    TransactionType.OPEN_MARKET_BUY,
    TransactionType.ACTIVIST_13D,
    TransactionType.FUND_NEW_POSITION,
    TransactionType.FUND_ADD_POSITION,
    TransactionType.PASSIVE_13G,
}
_DISTRIBUTION = {
    TransactionType.OPEN_MARKET_SELL,
    TransactionType.SCHEDULED_SALE,
}

# Internal tier names leak the scoring machinery; these are what a reader should see.
_SECTION_NAME = {
    Tier.INSTANT: "Worth interrupting you for",
    Tier.NOTABLE: "Notable this period",
    Tier.WEEKLY: "Context",
}

_JURISDICTION_NAME = {
    "US": "United States",
    "CA": "Canada",
    "UK": "United Kingdom",
    "EU": "Europe",
    "TW": "Taiwan",
    "JP": "Japan",
    "HK": "Hong Kong",
    "KR": "South Korea",
    "IN": "India",
    "AU": "Australia",
    "CN": "China",
    "BR": "Brazil",
    "CRYPTO": "On-chain",
}

# Sentence-case action names for HTML. The shared _ACTION_LABEL is uppercase because
# plain text has no other emphasis; in a styled email it just shouts.
_ACTION_HTML = {
    TransactionType.OPEN_MARKET_BUY: "Open-market buy",
    TransactionType.OPEN_MARKET_SELL: "Open-market sell",
    TransactionType.ACTIVIST_13D: "New 13D stake",
    TransactionType.PASSIVE_13G: "13G stake",
    TransactionType.FUND_NEW_POSITION: "New fund position",
    TransactionType.FUND_ADD_POSITION: "Added to position",
    TransactionType.GRANT: "Compensation grant",
    TransactionType.OPTION_EXERCISE: "Option exercise",
    TransactionType.SCHEDULED_SALE: "Scheduled sale",
    TransactionType.CRYPTO_TRANSFER: "On-chain transfer",
    TransactionType.OTHER: "Other",
}

# What each price source means for trust, in the reader's language rather than ours.
_PROVENANCE_LABEL = {
    PriceSource.FILING_STATED: "Stated in the filing",
    PriceSource.CLOSE_ON_DATE: "Estimated from trade-date close",
    PriceSource.MOST_RECENT_CLOSE: "Estimated from last close",
    PriceSource.NOT_PRICED: "Value as disclosed",
}


def _e(text: object) -> str:
    """Escape for HTML. Filer and issuer names come from third-party feeds and land
    directly in the email body, so nothing reaches the template unescaped."""
    return html.escape(str(text if text is not None else ""), quote=True)


def amount_colour(event: NormalizedEvent) -> str:
    if event.transaction_type in _ACCUMULATION:
        return ACCUMULATE
    if event.transaction_type in _DISTRIBUTION:
        return DISTRIBUTE
    return INK


def provenance_label(event: NormalizedEvent) -> str:
    """The line under each amount. Never contains a number -- it explains one."""
    if event.effective_usd is None:
        return "Not disclosed"
    label = _PROVENANCE_LABEL.get(event.price_source, "Value as disclosed")
    if event.price_source == PriceSource.NOT_PRICED and event.usd_value_is_estimate:
        return "Estimated from disclosure"
    return label


def summary_line(events: list[NormalizedEvent]) -> str:
    """The shape of the period before the reader scrolls: how many, how much, biggest.

    Every figure is an aggregate of the rows below, computed in code -- which is why
    `provenance.py` allowlists sums and counts over the same event list.
    """
    if not events:
        return "Nothing cleared the threshold"
    values = [e.effective_usd for e in events if e.effective_usd is not None]
    parts = [f"{len(events)} disclosure{'s' if len(events) != 1 else ''}"]
    if values:
        parts.append(f"{format_usd(sum(values))} disclosed")
        parts.append(f"largest {format_usd(max(values))}")
    return " · ".join(parts)


def _masthead(on: date, events: list[NormalizedEvent], kind: str) -> str:
    return f"""
<tr><td style="background:{PAPER};padding:30px 30px 0 30px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="background:{PAPER};font-family:{SERIF};font-size:22px;letter-spacing:.13em;
               text-transform:uppercase;color:{INK};">{_e(kind)}</td>
    <td align="right" style="background:{PAPER};font-family:{SERIF};font-size:13px;
               font-style:italic;color:{INK_FAINT};">{_e(on.isoformat())}</td>
  </tr></table>
  <div style="height:3px;background:{NAVY};margin:11px 0 0 0;font-size:0;line-height:0;">&nbsp;</div>
  <div style="background:{PAPER};font-family:{SANS};font-size:13px;font-weight:600;
              color:{INK_SOFT};padding:11px 0 0 0;">{_e(summary_line(events))}</div>
  <div style="background:{PAPER};font-family:{SERIF};font-size:12.5px;font-style:italic;
              color:{INK_FAINT};padding:5px 0 0 0;">
    Disclosed positions above $5M. Awareness only, not advice.
  </div>
</td></tr>"""


def _headline(prose: DigestProse | None) -> str:
    if not prose or not prose.headline:
        return ""
    return f"""
<tr><td style="background:{PAPER};padding:20px 30px 0 30px;">
  <div style="background:{PAPER};font-family:{SERIF};font-size:17px;line-height:1.5;
              color:{INK};">{_e(prose.headline)}</div>
</td></tr>"""


def _coverage(coverage_notes: list[str] | None) -> str:
    if not coverage_notes:
        return ""
    items = "".join(f"<div style='padding:1px 0;'>· {_e(n)}</div>" for n in coverage_notes)
    return f"""
<tr><td style="background:{PAPER};padding:18px 30px 0 30px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="background:#FBF6E9;border-left:3px solid {CAVEAT};padding:10px 13px;
               font-family:{SANS};font-size:12px;line-height:1.5;color:{CAVEAT};">
      <span style="font-family:{SERIF};font-size:12.5px;font-style:italic;
                   color:{CAVEAT};">Coverage</span>
      <div style="padding-top:3px;color:{CAVEAT};">{items}</div>
    </td>
  </tr></table>
</td></tr>"""


def _tier_rule(tier: Tier) -> str:
    return f"""
<tr><td style="background:{PAPER};padding:30px 30px 0 30px;">
  <div style="height:2px;background:{RULE_STRONG};font-size:0;line-height:0;">&nbsp;</div>
  <div style="background:{PAPER};font-family:{SERIF};font-size:15px;font-style:italic;
              color:{NAVY};padding:8px 0 0 0;">
    {_e(_SECTION_NAME.get(tier, _TIER_HEADING[tier]))}
  </div>
</td></tr>"""


def _row(event: NormalizedEvent, prose: DigestProse | None, first: bool) -> str:
    """One record, in three tiers: identity and amount, metadata, then prose.

    Only the name and the amount are bold. Bolding every field is what destroys
    scannability in a list of thirty records.
    """
    colour = amount_colour(event)
    lag = (event.disclosure_date - event.transaction_date).days
    meta = [
        _ACTION_HTML.get(event.transaction_type, "Other"),
        _JURISDICTION_NAME.get(event.jurisdiction.value, event.jurisdiction.value),
    ]
    if event.ticker:
        meta.append(_e(event.ticker))
    meta.append(f"filed {_e(event.disclosure_date.isoformat())}")
    meta.append(f"{lag}-day lag")
    if event.percent_of_company is not None:
        meta.append(f"{event.percent_of_company:.1f}% of company")

    why = (prose.why_it_matters.get(event.event_id) if prose else None) or (
        why_it_matters(event) + "."
    )
    note = (prose.skeptic_notes.get(event.event_id) if prose else None) or (
        "Routine, calendar-driven trade; low signal." if event.is_routine else ""
    )

    note_html = ""
    if note:
        note_html = f"""
  <div style="background:{PAPER};font-family:{SANS};font-size:12.5px;line-height:1.5;
              color:{CAVEAT};padding-top:5px;">
    <span style="font-weight:600;color:{CAVEAT};">Skeptic:</span> {_e(note)}
  </div>"""

    link_html = ""
    if event.source_url:
        link_html = f"""
  <div style="background:{PAPER};padding-top:7px;">
    <a href="{_e(event.source_url)}" style="font-family:{SANS};font-size:12px;
       color:{NAVY};text-decoration:none;border-bottom:1px solid {RULE_STRONG};"
       >Read the filing</a>
  </div>"""

    # A hairline above every record but the first: separators the reader can track
    # across thirty rows, without banding the backgrounds.
    divider = "" if first else f"border-top:1px solid {RULE};"

    return f"""
<tr><td style="background:{PAPER};padding:0 30px;">
  <div style="{divider}padding:17px 0 18px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td valign="top" class="stack" style="background:{PAPER};font-family:{SANS};
                   font-size:15px;line-height:1.35;color:{INK};font-weight:700;">
          {_e(event.filer_name)}<span style="color:{INK_FAINT};font-weight:400;"> · </span><span
            style="font-family:{SERIF};font-weight:400;font-size:15.5px;color:{INK};"
            >{_e(event.issuer_name)}</span>
        </td>
        <td valign="top" align="right" width="152" class="stack-amount"
            style="background:{PAPER};">
          <div style="background:{PAPER};font-family:{FIGURE};font-size:20px;
                      font-weight:600;color:{colour};white-space:nowrap;letter-spacing:-.01em;"
            >{_e(format_usd(event.effective_usd))}</div>
          <div style="background:{PAPER};font-family:{SERIF};font-size:11.5px;
                      font-style:italic;color:{INK_FAINT};padding-top:4px;line-height:1.45;"
            >{_e(provenance_label(event))}</div>
        </td>
      </tr>
    </table>
    <div style="background:{PAPER};font-family:{SANS};font-size:11.5px;color:{INK_FAINT};
                padding-top:6px;">{" · ".join(meta)}</div>
    <div style="background:{PAPER};font-family:{SANS};font-size:13.5px;line-height:1.55;
                color:{INK_SOFT};padding-top:9px;">
      <span style="font-weight:600;color:{INK};">Why it matters:</span> {_e(why)}
    </div>{note_html}{link_html}
  </div>
</td></tr>"""


def _empty_state() -> str:
    return f"""
<tr><td style="background:{PAPER};padding:30px;">
  <div style="border:1px solid {RULE};padding:24px;text-align:center;background:{PAPER};">
    <div style="font-family:{SERIF};font-size:16px;color:{INK};">
      Nothing cleared the $5M threshold.
    </div>
    <div style="font-family:{SANS};font-size:13px;color:{INK_SOFT};padding-top:6px;">
      Quiet periods are the common case. The sources below reported normally.
    </div>
  </div>
</td></tr>"""


def _footer() -> str:
    return f"""
<tr><td style="background:{PAPER};padding:28px 30px 30px 30px;">
  <div style="height:1px;background:{RULE};font-size:0;line-height:0;">&nbsp;</div>
  <div style="background:{PAPER};font-family:{SERIF};font-size:14px;font-style:italic;
              color:{INK};padding:14px 0 6px 0;">On provenance</div>
  <div style="background:{PAPER};font-family:{SANS};font-size:12px;line-height:1.6;
              color:{INK_SOFT};">
    Every figure above is copied from a disclosure field or computed from one, and the
    line beneath each amount says which. No number here was written by a language model:
    the sentences may be, the figures never are, and any sentence carrying a figure that
    did not trace back to a filing was dropped before this was sent.
  </div>
  <div style="background:{PAPER};font-family:{SANS};font-size:12px;line-height:1.6;
              color:{INK_SOFT};padding-top:10px;">
    Insider <em>buys</em> carry more information than sells, and only non-routine trades
    have historically carried signal at all. This is an awareness tool. It is not
    investment advice.
  </div>
</td></tr>"""


def render_digest_html(
    events: list[NormalizedEvent],
    on: date | None = None,
    prose: DigestProse | None = None,
    coverage_notes: list[str] | None = None,
    kind: str = "Whale Digest",
) -> str:
    """Render ranked events as a standalone HTML email body."""
    on = on or date.today()
    body = [_masthead(on, events, kind), _headline(prose), _coverage(coverage_notes)]

    if not events:
        body.append(_empty_state())
    else:
        for tier in (Tier.INSTANT, Tier.NOTABLE, Tier.WEEKLY):
            group = [e for e in events if e.tier == tier]
            if not group:
                continue
            body.append(_tier_rule(tier))
            for index, event in enumerate(group):
                body.append(_row(event, prose, first=index == 0))

    body.append(_footer())

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{_e(kind)}, {_e(on.isoformat())}</title>
<!--[if mso]>
<style>
  /* Outlook renders through the Word engine and ignores the system-font stacks
     above, falling back to Times. Naming a face it actually has avoids that. */
  body, table, td, div, span, a {{ font-family: 'Segoe UI', Arial, sans-serif !important; }}
</style>
<![endif]-->
<style>
  /* Nothing structural lives here: many clients strip this block entirely, and the
     layout is fully inlined so they lose only the phone breakpoint. */
  body {{ margin:0; padding:0; width:100% !important; }}
  @media only screen and (max-width:600px) {{
    .shell {{ width:100% !important; }}
    /* A 19px figure cannot sit beside prose on a phone, so the amount moves above
       the record rather than wrapping mid-number. */
    .stack, .stack-amount {{ display:block !important; width:100% !important;
                             text-align:left !important; }}
    .stack-amount {{ padding:9px 0 0 0 !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{PAGE};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{PAGE};">
  <tr><td align="center" style="background:{PAGE};padding:22px 10px;">
    <table role="presentation" class="shell" width="640" cellpadding="0" cellspacing="0"
           border="0" style="width:640px;max-width:640px;background:{PAPER};
           border:1px solid {RULE_STRONG};">
      {"".join(body)}
    </table>
  </td></tr>
</table>
</body></html>"""


def html_to_text(markup: str) -> str:
    """Strip tags to plain text, for running the provenance gate over the HTML body.

    The HTML is built from the same structured fields as the text digest, so it cannot
    introduce an unsourced figure by construction -- but the gate is cheap and this keeps
    the guarantee true of what is actually sent rather than only of what is printed.
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text))


# -- hero image ----------------------------------------------------------------
# The masthead photograph. Two delivery paths, because the constraints differ.
#
# Article pages inline it as a data URI so a page stays a single file that can be saved,
# mailed as an attachment or served from a static directory and look the same in all
# three. Email hotlinks the remote copy instead, because Gmail strips data URIs on
# images in mail and would show nothing at all.
#
# In both, the headline sits in a solid block UNDER the photograph rather than overlaid
# on it. Text over an image depends on the image having loaded, and mail clients block
# remote images by default on first open, so an overlay headline is a headline the
# reader may simply never receive.
# The remote copy is cropped by the CDN rather than by us: Contentful honours w, h and
# fit on the query string, so the email gets the same 2.4:1 masthead band as the article
# without us having to host a second file. A portrait-shaped photograph pushed the whole
# report below the fold on a phone.
HERO_URL = (
    "https://images.ctfassets.net/rbl6nw8n2c6i/5BKR9WEZtS67DxDsNOC6po/"
    "d8caa7c3406ab641639ede68a351110c/wallstreet.png?fit=fill&w=1280&h=534"
)
HERO_ALT = "Wall Street and Broad Street signs outside the New York Stock Exchange"
_HERO_FILE = Path(__file__).resolve().parents[3] / "assets" / "wallstreet.jpeg"


@lru_cache(maxsize=1)
def hero_data_uri() -> str:
    """The masthead photo as a data URI, or the remote URL if the file is missing.

    Cached because it is read once per process and the encode is pure overhead after
    that. A missing asset degrades to the remote URL rather than raising: a page without
    its photograph is a worse page, not a failed run.
    """
    try:
        encoded = base64.b64encode(_HERO_FILE.read_bytes()).decode("ascii")
    except OSError:
        return HERO_URL
    return f"data:image/jpeg;base64,{encoded}"


def hero_block(headline: str, kicker: str, src: str, headline_size: str = "34px") -> str:
    """Photograph with the headline in a navy block beneath it.

    `src` is the caller's choice of data URI or remote URL, since only the caller knows
    whether it is rendering a page or an email.
    """
    tag = (
        f'<span style="background:{DISTRIBUTE};color:#FFFFFF;font-family:{SANS};'
        f"font-size:10.5px;font-weight:700;letter-spacing:.06em;padding:3px 7px;"
        f'text-transform:uppercase;">{html.escape(kicker)}</span>'
        if kicker
        else ""
    )
    return f"""
<img src="{html.escape(src, quote=True)}" width="640" alt="{html.escape(HERO_ALT, quote=True)}"
     style="display:block;width:100%;max-width:640px;height:auto;border:0;outline:none;
            text-decoration:none;margin:0;" />
<div style="background:{NAVY_DEEP};padding:26px 30px 24px 30px;margin:0;">
  <div class="hero-h" style="font-family:{SANS};font-size:{headline_size};line-height:1.16;
              font-weight:700;letter-spacing:-.021em;color:#FFFFFF;
              background:{NAVY_DEEP};">{html.escape(headline)}</div>
  <div style="height:1px;background:#3E6591;margin:18px 0 14px 0;font-size:0;line-height:0;">&nbsp;</div>
  <div style="font-family:{SANS};font-size:13px;color:#FFFFFF;background:{NAVY_DEEP};">
    {tag}<span style="padding-left:9px;font-weight:700;">Disclosed positions above $5M</span>
  </div>
</div>"""
