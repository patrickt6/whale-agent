"""The appendix page behind the "5 largest trades" email section.

The email links two ways per row: out to SEC for the primary source, and into our own
explanation of the same filing. This module is that second page. Its only job is to
give each filing a stable anchor so the email can deep-link a reader to the exact
paragraph, not the top of a page they then have to search.

The anchor is derived from the accession number, which is the filing's own identity.
Not the manager name (several tracked managers can file the same week) and not a list
position (which shifts as soon as one more filing is pooled ahead of it in a later
run).
"""

from __future__ import annotations

import hashlib
import html
from collections.abc import Callable
from datetime import date
from pathlib import Path

from whale_agent.enrichment.news_context import NewsItem
from whale_agent.ingestion.position_history import PriorPositionFact, prior_position
from whale_agent.storage.db import Store

__all__ = [
    "filing_anchor",
    "item_anchor",
    "manager_anchor",
    "section_anchor",
    "tracked_filings_filename",
    "tracked_filings_page_filename",
    "PAGE_SLUGS",
    "render_tracked_filings_page",
    "write_tracked_filings_page",
]

# The report used to be one HTML document. A single long file is hard to read, and the problem is
# structural, not about the prose itself -- so this splits the same sections into
# separate pages, one file per major section, plus a landing/index page. Anything that
# used to link to `#sec-*` on the single page now links to `{slug}.html#{anchor}` on
# the specific page that anchor actually lives on.
#
# The mapping below is the single source of truth for "which anchor lives on which
# page" -- every caller (digest_weekly.py building email links, link_check.py's
# fragment verification, this module's own nav) must agree with it. Quarterly 13F
# filings share the trades-and-stakes page with the pooled trades/stakes: both use
# `filing_anchor`, and they were already one page together before this split, so
# folding them together keeps `filing_anchor` resolving to exactly one page.
PAGE_SLUGS: dict[str, str] = {
    "index": "index",
    "trades": "trades-and-stakes",
    "overview": "fortnight-overview",
    "bigstakes": "big-stakes",
    "resale": "arranging-to-sell",
    "moves": "tracked-manager-filings",
    "roster": "tracked-manager-roster",
}

_PAGE_TITLES: dict[str, str] = {
    "index": "Report contents",
    "trades": "This week's trades and stakes",
    "overview": "The full fortnight overview",
    "bigstakes": "Big stakes disclosed this week",
    "resale": "Arranging to sell",
    "moves": "What every tracked manager filed",
    "roster": "The full tracked-manager roster",
}

# Page order for the persistent nav bar and prev/next links.
_PAGE_ORDER = ["index", "trades", "overview", "bigstakes", "resale", "moves", "roster"]

NewsFetcher = Callable[..., list[NewsItem]]

# Single hoisted stylesheet for the whole report page, mirroring the approach the
# email renderer takes in render_weekly_html.py (short class names, decoration lives
# in one block rather than repeated per element). This is a self-contained static
# page served from a Cloudflare static site -- no external fonts, no CDN, no images --
# so everything here is inline CSS with a system font stack. Kept entirely out of the
# email path: render_weekly_html.py has its own stylesheet and must stay under
# Gmail's clip point, which this block has no bearing on.
_PAGE_CSS = """
:root{--ink:#1a1d21;--ink-soft:#4b5259;--ink-faint:#767c84;--paper:#fdfdfc;
  --paper-raised:#f4f5f6;--rule:#e2e4e7;--accent:#1f4e8c;--accent-soft:#3d6cad;
  --target:#fff3cd;--up-ink:#146c2e;--up-bg:#dcf3e2;--down-ink:#9a2f22;
  --down-bg:#fbe4e0;--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  Helvetica,Arial,sans-serif;--serif:Georgia,"Times New Roman",serif;
  --measure:70ch;--header-h:0px}
@media (prefers-color-scheme:dark){:root{--ink:#e8e9ea;--ink-soft:#b6bbc1;
  --ink-faint:#888e96;--paper:#16181b;--paper-raised:#1f2226;--rule:#31353a;
  --accent:#7aa6da;--accent-soft:#6690c4;--target:#4a3f14;--up-ink:#8fd6a0;
  --up-bg:#173626;--down-ink:#f2a99a;--down-bg:#3a1f1a}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  overflow-x:hidden}
.hd{background:var(--paper-raised);border-bottom:1px solid var(--rule);
  padding:28px 20px 20px}
.hd h1,.page h1{font-family:var(--sans);font-size:1.6rem;font-weight:700;
  line-height:1.25;letter-spacing:-.01em;margin:0 0 10px;max-width:var(--measure);
  margin-left:auto;margin-right:auto}
.dek{font-family:var(--serif);font-size:1.05rem;line-height:1.6;color:var(--ink-soft);
  max-width:var(--measure);margin:0 auto}
nav.toc{max-width:var(--measure);margin:18px auto 0;padding-top:14px;
  border-top:1px solid var(--rule);display:flex;flex-wrap:wrap;gap:8px 18px}
nav.toc a{font-family:var(--sans);font-size:.85rem;font-weight:600;color:var(--accent);
  text-decoration:none}
nav.toc a:hover{text-decoration:underline}
main.page{max-width:var(--measure);margin:0 auto;padding:8px 20px 60px}
main.page h2{font-size:1.25rem;font-weight:700;line-height:1.3;margin:2.4em 0 .6em;
  padding-top:.4em;border-top:1px solid var(--rule);scroll-margin-top:1rem}
main.page h2:first-child{margin-top:1.2em;border-top:0;padding-top:0}
.filing{padding:18px 16px;margin:14px 0;border:1px solid var(--rule);
  border-left:4px solid var(--accent-soft);border-radius:8px;background:
  var(--paper-raised);scroll-margin-top:5.5rem}
.filing:target{background:var(--target);border-left-color:var(--down-ink)}
.filing p{font-family:var(--serif);font-size:1rem;line-height:1.65;color:var(--ink);
  margin:0 0 .9em}
.filing p:last-child{margin-bottom:0}
.filing h3.filing-head{font-family:var(--sans);font-size:1.15rem;font-weight:800;
  line-height:1.3;margin:0 0 .6em;color:var(--ink);letter-spacing:-.005em}
h3.filing-head .amt{font-variant-numeric:tabular-nums;color:var(--ink-soft);
  font-weight:600}
/* A persistent bar of links to every other report page, present on every page so a
   reader never has to backtrack to the index just to move between sections. */
.sitebar{max-width:var(--measure);margin:0 auto 4px;padding:10px 20px 0;display:flex;
  flex-wrap:wrap;gap:6px 14px;font-family:var(--sans);font-size:.8rem}
.sitebar a{color:var(--ink-soft);text-decoration:none;padding:2px 0}
.sitebar a.current{color:var(--accent);font-weight:700;border-bottom:2px solid
  var(--accent)}
.sitebar a:hover{color:var(--accent)}
.pagenav{max-width:var(--measure);margin:2.4em auto 0;padding-top:1.2em;
  border-top:1px solid var(--rule);display:flex;justify-content:space-between;
  font-family:var(--sans);font-size:.9rem;gap:12px}
.pagenav a{font-weight:600}
.menu{max-width:var(--measure);margin:1.6em auto 0;padding:0;list-style:none;
  display:grid;gap:12px}
.menu li{border:1px solid var(--rule);border-radius:8px;background:var(--paper-raised)}
.menu a{display:block;padding:16px 18px;text-decoration:none;color:var(--ink)}
.menu a:hover{border-color:var(--accent)}
.menu .mt{font-family:var(--sans);font-weight:700;font-size:1.05rem;color:var(--ink)}
.menu .ms{display:block;font-family:var(--serif);font-size:.92rem;color:var(--ink-soft);
  margin-top:.25em}
main.page a{color:var(--accent);text-decoration:none;border-bottom:1px solid
  color-mix(in srgb,var(--accent) 40%,transparent)}
main.page a:hover{color:var(--accent-soft);border-bottom-color:var(--accent-soft)}
table{border-collapse:collapse;width:100%;margin:.5em 0 1.2em;font-size:.92rem}
td,th{padding:7px 8px;border-bottom:1px solid var(--rule);text-align:left;
  vertical-align:top}
.moves-table td.amt,.moves-table th.amt{text-align:right;
  font-variant-numeric:tabular-nums}
.moves-table td.name{font-weight:600}
.tag{display:inline-block;font-family:var(--sans);font-size:.7rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.03em;padding:.18em .5em;border-radius:3px;
  white-space:nowrap}
.tag-up{color:var(--up-ink);background:var(--up-bg)}
.tag-down{color:var(--down-ink);background:var(--down-bg)}
.tag-flat{color:var(--ink-soft);background:var(--paper-raised)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media (max-width:640px){
  .hd{padding:20px 14px 16px}
  main.page{padding:8px 14px 48px}
  table,thead,tbody,tr{display:block;width:100%}
  td,th{display:block;border-bottom:0;padding:2px 0}
  tr{padding:8px 0;border-bottom:1px solid var(--rule)}
  .moves-table td.amt{text-align:left}
}
/* The "Big stakes disclosed this week" and "Arranging to sell" blocks are the email
   renderer's own markup (render_weekly_html.py), reused here as-is rather than
   duplicated. That markup carries hardcoded inline background:#FFFFFF (correct for an
   email client, which never gets this page's dark-mode variables) but expects a rule
   set that only ever shipped inside the email's own head block. Without it those
   cells inherit this page's --ink, which in dark mode is near-white text on the
   fragment's hardcoded white card, unreadable. These rules give that fragment the
   same literal light-theme colors the email itself uses, scoped so they never leak
   into the rest of this theme-aware page.
*/
.a{font-family:'Helvetica Neue',Helvetica,Arial,'Segoe UI',Roboto,sans-serif;
  font-size:12.5px;font-weight:700;color:#0056B3;text-decoration:none;
  border-bottom:1px solid #B9BEC4;white-space:nowrap}
.rt,.rt2,.rt3{font-family:'Helvetica Neue',Helvetica,Arial,'Segoe UI',Roboto,sans-serif;
  font-size:13px;background:#FFFFFF;border-top:1px solid #DEE1E4;
  padding:8px 10px 8px 0;vertical-align:top}
.rt{color:#12100E}
.rtb{font-weight:700}
.rt2{color:#3C4043}
.rt3{font-size:13.5px;font-weight:700;color:#12100E;white-space:nowrap}
.rt4,.rt4w{background:#FFFFFF;border-top:1px solid #DEE1E4;padding:8px 0;
  vertical-align:top}
.rt4w{white-space:nowrap}
.subf{color:#6E7378;font-size:11.5px;padding-top:2px}
.subn{color:#003468;font-size:11.5px;padding-top:2px}
.tickf,.estf,.stockof{color:#6E7378}
.estf,.stockof{font-size:11px}
"""


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _link(url: str, label: str) -> str:
    if not url:
        return label
    return f'<a href="{_e(url)}">{_e(label)}</a>'


def _fetch_news_memoized(
    news_fetcher: NewsFetcher | None,
    cache: dict,
    *,
    ticker: str | None,
    cik: str | None,
    around: date,
) -> list[NewsItem] | None:
    """Runs `news_fetcher` at most once per (ticker, cik, date) within this render
    call. Returns None (not []) when no fetcher was supplied at all -- that is the
    "lookup was not run" case, distinct from "lookup ran and found nothing", and the
    prose below must say something different for each."""
    if news_fetcher is None:
        return None
    key = ((ticker or "").upper(), (cik or "").strip(), around)
    if key not in cache:
        try:
            cache[key] = news_fetcher(ticker=ticker, cik=cik, around=around)
        except Exception:  # noqa: BLE001 - a dead news source must not kill the page
            cache[key] = []
    return cache[key]


def _news_prose(items: list[NewsItem] | None, issuer: str) -> str:
    if items is None:
        return f"News and 8-K context for {_e(issuer)} was not looked up for this report run."
    if not items:
        return (
            f"Nothing turned up for {_e(issuer)} in the two weeks around this filing. "
            "The search covered FMP's stock news and press release feeds and SEC "
            "EDGAR's 8-K index. Those sources are not exhaustive."
        )
    sentences = []
    for it in items[:5]:
        sentences.append(
            f"On {_e(it.published.isoformat())}, {_e(it.publisher)} "
            f'{_link(it.url, "reported")} "{_e(it.headline)}".'
        )
    lead = (
        f"Coverage of {_e(issuer)} from the two weeks around this filing follows, "
        "quoted so a reader can judge it directly. A news date close to a filing date "
        "says nothing about why the filing was made."
    )
    return lead + " " + " ".join(sentences)


def _prior_position_prose(fact: PriorPositionFact | None, manager: str) -> str:
    if fact is None:
        return (
            f"No 13F position history is on file for {_e(manager)} in this system "
            "yet, so there is nothing to compare this filing against."
        )
    m = _e(manager)
    if fact.status == "no_history_on_file":
        return (
            f"No 13F filing has ever been recorded for {m}, so there is no prior "
            "position on file to compare this against; that will change as history "
            "accrues from future runs."
        )
    if fact.status == "no_prior_quarter_on_file":
        return (
            f"Only one 13F is on file for {m} so far (report period "
            f"{_e(fact.as_of_report_period.isoformat())}), so there is nothing "
            "earlier to compare this position to yet."
        )
    if fact.status == "no_record":
        return (
            f"{m}'s 13F filings on file do not mention this issuer. A 13F only "
            "covers 13F-reportable securities. Its absence here does not confirm no "
            "position was ever held."
        )
    if fact.status == "new":
        return (
            f"This is a new position for {m}: the issuer was absent from its "
            f"{_e(fact.prior_report_period.isoformat())} 13F and present in the "
            f"{_e(fact.as_of_report_period.isoformat())} filing, reported at "
            f"{fact.current_shares:,} shares "
            f"(${fact.current_value_usd:,.0f} of 13F-reported value)."
        )
    if fact.status == "closed":
        return (
            f"{m} reported this issuer in its {_e(fact.prior_report_period.isoformat())} "
            f"13F at {fact.prior_shares:,} shares "
            f"(${fact.prior_value_usd:,.0f} of reported value) and does not report it "
            f"in the following quarter, so the 13F record shows the position closed."
        )
    if fact.status in {"increased", "decreased", "unchanged"}:
        verb = {"increased": "grew", "decreased": "shrank", "unchanged": "held steady"}[
            fact.status
        ]
        pct = f" ({fact.delta_pct:+.1f}%)" if fact.delta_pct is not None else ""
        return (
            f"By 13F, {m}'s reported position in this issuer {verb} from "
            f"{fact.prior_shares:,} shares (${fact.prior_value_usd:,.0f}) as of "
            f"{_e(fact.prior_report_period.isoformat())} to {fact.current_shares:,} "
            f"shares (${fact.current_value_usd:,.0f}) as of "
            f"{_e(fact.as_of_report_period.isoformat())}{pct}."
        )
    return fact.note


def _filing_head(*, tag: tuple[str, str] | None, title: str, amount_label: str = "") -> str:
    """A self-identifying heading for one filing section: who filed, what action,
    which issuer, how much -- so the section reads on its own even when the email
    deep-links straight past the top-of-page context into the middle of this one.

    `tag` is an (label, css_class) pair such as ("Sold", "tag-down"); color is never
    the only signal for direction, the word is always printed too. Pass None to skip
    the tag entirely (a filing type, like a 13D/G, that has no up/down direction).
    """
    tag_html = f'<span class="tag {tag[1]}">{_e(tag[0])}</span> ' if tag else ""
    amt_html = f' <span class="amt">{_e(amount_label)}</span>' if amount_label else ""
    return f'<h3 class="filing-head">{tag_html}{title}{amt_html}</h3>'


def _moves_table(changes, prior_period: str) -> str:
    """The enumerable half of a 13F comparison -- which names were opened, added to,
    trimmed, or no longer reported, and for how much -- as a real semantic table
    instead of flowing prose, so magnitudes can be scanned and compared. Rows are
    grouped by direction (each row already sorted largest-first within its group by
    `diff_holdings`), with a colored-and-worded tag per row rather than color alone.

    The "13F silence" caveat is written once, after the table, for the whole
    no-longer-reported group -- not once per name in that group. That was the
    concrete defect the reader reported: the same caveat sentence appearing 133
    times on one page, once per holding, instead of once per section.
    """
    groups: list[tuple[str, str, list[tuple[str, int]]]] = []
    if changes.opened:
        groups.append(("Opened", "tag-up", [(h.issuer, h.value_usd) for h in changes.opened]))
    if changes.increased:
        groups.append(("Added to", "tag-up", list(changes.increased)))
    if changes.decreased:
        groups.append(
            (
                "Trimmed",
                "tag-down",
                [(issuer, abs(delta)) for issuer, delta in changes.decreased],
            )
        )
    if changes.closed:
        groups.append(
            (
                "No longer reported",
                "tag-down",
                [(h.issuer, h.value_usd) for h in changes.closed],
            )
        )
    if not groups:
        return ""

    rows = []
    for label, css_class, items in groups:
        for issuer, amount in items:
            rows.append(
                "<tr>"
                f'<td><span class="tag {css_class}">{_e(label)}</span></td>'
                f'<td class="name">{_e(issuer)}</td>'
                f'<td class="amt">${amount:,.0f}</td>'
                "</tr>"
            )
    table = (
        '<table class="moves-table">'
        '<caption class="sr-only">Position changes this quarter</caption>'
        '<thead><tr><th scope="col">Direction</th><th scope="col">Issuer</th>'
        '<th scope="col" class="amt">Amount</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    caveat = ""
    if changes.closed:
        caveat = (
            "<p>Every name marked 'No longer reported' above was held as of "
            f"{prior_period} and is absent from the current filing. A 13F's silence "
            "on a name does not itself prove it was sold, only that it is absent "
            "from the current filing.</p>"
        )
    return table + caveat


def filing_anchor(accession: str) -> str:
    """A stable anchor id for one filing, keyed on its accession number.

    Prefix is `f-`, not the more readable `filing-`: at 200+ anchors per email this
    repeats in every "Our report" href, and the five bytes saved per link is real
    weight against Gmail's clip point (see `_row_links` in render_weekly_html.py).
    Still unique -- the accession number carries all of that -- just not spelled out.
    """
    return f"f-{accession}"


def item_anchor(accession: str = "", url: str = "") -> str:
    """A stable anchor id for one row, from its accession number when it has one.

    Not every row this page carries an accession field yet -- the fortnight-overview
    rows in particular are built from whatever `source_url` the ingesting adapter
    happened to record, which is not always an SEC accession. For those, the anchor is
    a short hash of the row's own URL instead: still stable across runs (the same
    filing has the same URL every time), unique enough within one page's few hundred
    rows at 10 hex characters (40 bits), and short, for the same reason `filing_anchor`
    trimmed its prefix. Every row this module renders gets one or the other, so a link
    built from either never points at a page with no matching id.
    """
    if accession:
        return filing_anchor(accession)
    if url:
        return "u-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return ""


def manager_anchor(name: str) -> str:
    """A stable anchor id for one manager's block under "What every tracked manager
    filed" (`#sec-moves`), so the email's per-manager truncation note ("N more
    filing(s) from X this week") can link straight to that manager's own filings
    instead of the generic top of the report page.

    Keyed on the manager name, not a list position (list order changes run to run,
    same reasoning as `filing_anchor`). Two managers never share a name in this
    system, so the slug plus a short hash of the name is unique and stable across
    runs without needing a database id.
    """
    slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"mgr-{slug}-{digest}"


def section_anchor(label: str) -> str:
    """A stable anchor id for one fortnight-overview category (Insider filings,
    Congressional trading, International plays, and so on) under "The full fortnight
    overview" (`#sec-overview`), so the email's "N more X this week" truncation note
    can link straight to that category instead of the generic top of the report page.
    """
    slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
    return f"cat-{slug}"


def tracked_filings_filename(on: date) -> str:
    """The legacy single-file name. Kept only for the opt-in email attachment path
    (`--attach-report`), which still ships one file rather than a small zip of pages;
    see `write_tracked_filings_page`'s docstring for what that attachment actually
    contains now."""
    return f"tracked-filings-{on.isoformat()}.html"


def tracked_filings_page_filename(on: date, slug: str) -> str:
    """The filename for one page of the multi-page report, e.g.
    `tracked-filings-2026-08-17-trades-and-stakes.html`. `slug` is one of the keys in
    `PAGE_SLUGS` (e.g. "trades", "overview") or, equivalently, one of its values."""
    page_slug = PAGE_SLUGS.get(slug, slug)
    return f"tracked-filings-{on.isoformat()}-{page_slug}.html"


def _trade_prose(
    t,
    *,
    store: Store | None,
    news_fetcher: NewsFetcher | None,
    news_cache: dict,
) -> str:
    """A written note on one Form 4 open-market trade, covering all six points the
    reader asked for: the filing itself, context, prior position, when, who, and any
    other news. Only P/S trades reach this function -- `pool_top_trades` already
    excludes grants (A) and tax withholding (F), which are not discretionary
    decisions -- so the language below can say "sold"/"bought" without hedging."""
    verb = "sold" if t.direction == "sold" else "bought"
    tag = ("Sold", "tag-down") if t.direction == "sold" else ("Bought", "tag-up")
    head = _filing_head(
        tag=tag,
        title=f"{_e(t.manager)}, {_e(t.issuer)} ({_e(t.symbol)})",
        amount_label=f"${t.dollar_value:,.0f}",
    )
    opening = (
        f"On {_e(t.filed.isoformat())}, {_e(t.manager)} filed a Form 4. "
        f"It reported that {_e(t.manager)} {verb} {t.shares:,} shares of "
        f"{_e(t.issuer)} ({_e(t.symbol)}) at ${t.price:,.2f} per share, "
        f"${t.dollar_value:,.0f} in total. "
        f"{_link(t.url, 'The filing itself is here')}. "
    )
    if t.percent_of_portfolio is not None:
        opening += (
            f"Against its own disclosed 13F portfolio of roughly "
            f"${t.portfolio_value_usd:,.0f}, this trade is about "
            f"{t.percent_of_portfolio:.1f}% of that book. This report ranks trades "
            "by that percentage. "
        )
    else:
        opening += (
            f"{_e(t.manager)} has no 13F portfolio value on file to size this trade "
            "against, so it is ranked here by dollar value alone. "
        )

    fact = (
        prior_position(store, t.manager_cik, t.manager, t.issuer, t.filed)
        if store is not None
        else None
    )
    position_sentence = _prior_position_prose(fact, t.manager)

    news_items = _fetch_news_memoized(
        news_fetcher, news_cache, ticker=t.symbol, cik=None, around=t.filed
    )
    news_sentence = _news_prose(news_items, t.issuer)

    return (
        f'<div id="{_e(filing_anchor(t.accession))}" class="filing">'
        f"{head}"
        f"<p>{opening}</p>"
        f"<p>{position_sentence}</p>"
        f"<p>{news_sentence}</p>"
        "</div>"
    )


def _stake_prose(
    s,
    *,
    store: Store | None,
    news_fetcher: NewsFetcher | None,
    news_cache: dict,
) -> str:
    """A written note on one SC 13D/13G stake disclosure. Never a dollar value: a
    13D/G discloses percent of class and share count, not price."""
    kind = "a new position" if s.is_new else "an amended stake"
    tag = ("New position", "tag-up") if s.is_new else ("Amended", "tag-flat")
    head = _filing_head(
        tag=tag,
        title=f"{_e(s.manager)}, {_e(s.issuer)} ({_e(s.form)})",
        amount_label=f"{s.percent:g}% of class",
    )
    shares_clause = f", {s.shares:,} shares of the class" if s.shares else ""
    opening = (
        f"On {_e(s.filed.isoformat())}, {_e(s.manager)} filed a {_e(s.form)}. "
        f"It disclosed {kind} of {s.percent:g}% of {_e(s.issuer)}{shares_clause}. "
        f"{_link(s.url, 'The filing itself is here')}. A 13D/13G states percent of "
        "class and share count only. It carries no transaction price. "
        "So no dollar value can honestly be attached to it. "
    )

    fact = (
        prior_position(store, s.manager_cik, s.manager, s.issuer, s.filed)
        if store is not None
        else None
    )
    position_sentence = _prior_position_prose(fact, s.manager)

    news_items = _fetch_news_memoized(
        news_fetcher, news_cache, ticker=None, cik=None, around=s.filed
    )
    news_sentence = _news_prose(news_items, s.issuer)

    return (
        f'<div id="{_e(filing_anchor(s.accession))}" class="filing">'
        f"{head}"
        f"<p>{opening}</p>"
        f"<p>{position_sentence}</p>"
        f"<p>{news_sentence}</p>"
        "</div>"
    )


def _quarterly_filing_prose(
    fc,
    *,
    news_fetcher: NewsFetcher | None,
    news_cache: dict,
) -> str:
    """A written note on one manager's quarterly 13F, filed inside the report window:
    overall portfolio change, largest moves, and news context for the most
    significant names -- matching `_trade_prose`/`_stake_prose`'s provenance and
    honesty conventions exactly.

    Never says a portfolio is "new" just because no prior period is on file (see
    `quarterly_filings.FilingChange.has_prior`); never sizes a put as ownership or a
    bearish bet, since `Holding.is_option` positions carry the 13F's notional value of
    the underlying, not premium paid or capital at risk; never asserts causation
    between a news item and a position change.
    """
    m = _e(fc.manager_name)
    period = _e(fc.report_period.isoformat())
    filed = _e(fc.filed_date.isoformat())
    head = _filing_head(
        tag=None,
        title=f"{m}, 13F-HR for the quarter ended {period}",
        amount_label=f"${fc.current_total_value_usd:,.0f}",
    )
    opening = (
        f"On {filed}, {m} filed a 13F-HR for the quarter ended {period}, "
        f"{_link(fc.source_url, 'the filing itself is here')}, reporting "
        f"{fc.current_position_count:,} stock positions worth "
        f"${fc.current_total_value_usd:,.0f} in aggregate 13F-reported value. This is "
        "a quarter-end snapshot, filed up to 45 days after the fact. It describes "
        f"the portfolio as of {period}."
    )

    if not fc.has_prior:
        overview = (
            f"No earlier 13F is on file for {m} to compare this against, so nothing "
            "here can be called opened, closed, added to, or trimmed. This is a gap "
            "in our own data collection. It says nothing about what this manager "
            "filed before. Future runs will close the gap as more quarters accrue."
        )
        moves = ""
    else:
        prior_period = _e(fc.prior_report_period.isoformat())
        delta = fc.total_value_delta_usd or 0
        verb = "grew" if delta > 0 else "shrank" if delta < 0 else "held steady"
        pct = (
            f" ({fc.total_value_delta_pct:+.1f}%)"
            if fc.total_value_delta_pct is not None
            else ""
        )
        count_delta = fc.position_count_delta or 0
        count_clause = (
            f"the position count went from {fc.prior_position_count:,} to "
            f"{fc.current_position_count:,}"
            + (f" ({count_delta:+d})" if count_delta else ", unchanged")
        )
        if fc.composition_shifted:
            # The stock-only percentage is arithmetically correct and descriptively
            # false here, so it is not printed at all rather than printed and then
            # qualified: a reader skimming the paragraph would carry the number away
            # and leave the caveat behind.
            overview = (
                f"Against its prior 13F on file for the quarter ended {prior_period}, "
                f"{_link(fc.prior_source_url, 'filed here')}, this book changed shape "
                f"as well as size, so a stock-to-stock percentage would mislead. It is "
                f"left out. Options were {fc.prior_option_share_pct:.0f}% of the "
                f"${fc.prior_disclosed_total_usd:,.0f} disclosed for the quarter ended "
                f"{prior_period} and {fc.current_option_share_pct:.0f}% of the "
                f"${fc.current_disclosed_total_usd:,.0f} disclosed for the quarter ended "
                f"{_e(fc.report_period.isoformat())}. Reported stock went from "
                f"${fc.prior_total_value_usd:,.0f} to ${fc.current_total_value_usd:,.0f} "
                f"and {count_clause}. A 13F reports options at the notional value of the "
                f"underlying. That is not the premium paid, and the filing does not "
                f"disclose what offsets the options position. The change between the "
                f"two disclosed totals is a shift in how the exposure is held. It is "
                f"not simply the book growing or shrinking by that amount."
            )
        else:
            overview = (
                f"Against its prior 13F on file for the quarter ended {prior_period}, "
                f"{_link(fc.prior_source_url, 'filed here')}, reported stock value {verb} "
                f"from ${fc.prior_total_value_usd:,.0f} to ${fc.current_total_value_usd:,.0f}"
                f"{pct}, and {count_clause}."
            )

        moves = _moves_table(fc.changes, prior_period)

    top_issuers: list[str] = []
    if fc.has_prior and fc.changes is not None:
        for h in fc.changes.opened[:2]:
            top_issuers.append(h.issuer)
        for h in fc.changes.closed[:1]:
            top_issuers.append(h.issuer)
    news_sentences = []
    for issuer in top_issuers[:3]:
        items = _fetch_news_memoized(
            news_fetcher, news_cache, ticker=None, cik=None, around=fc.filed_date
        )
        news_sentences.append(_news_prose(items, issuer))

    body = (
        f"<p>{opening}</p>"
        f"<p>{overview}</p>" + moves + "".join(f"<p>{s}</p>" for s in news_sentences)
    )
    return f'<div id="{_e(filing_anchor(fc.accession))}" class="filing">{head}{body}</div>'


def _overview_row_prose(
    row,
    *,
    news_fetcher: NewsFetcher | None,
    news_cache: dict,
) -> str:
    """A written note on one row of the fortnight overview (insider, congressional,
    institutional, international) -- the same category that used to be a bare list
    with a filer, an issuer, and a dollar figure and nothing else.

    These rows carry no manager CIK, so there is no 13F history to compare against
    (that question is already answered by `_trade_prose`/`_stake_prose` for the
    subset of filings that are pooled trades of tracked managers); what this prose
    adds over the bare row is the filing link, an honest account of whether the
    dollar figure is stated or estimated (and from what), and any nearby news.
    """
    anchor = item_anchor(url=row.filing_url) if row.filing_url else ""
    id_attr = f' id="{_e(anchor)}"' if anchor else ""
    role = f" ({_e(row.filer_role)})" if row.filer_role else ""
    ticker = f" ({_e(row.ticker)})" if row.ticker else ""
    filing_link = (
        _link(row.filing_url, "the filing itself is here")
        if row.filing_url
        else "no filing link is on record for this row"
    )
    if row.amount_usd is None:
        amount_sentence = "No dollar value is disclosed for this filing."
    elif row.is_estimate:
        amount_sentence = (
            f"The reported value, {_e(row.amount_label)}, is an estimate. The "
            "source feed for this filing carries a share count but no price the "
            "filer itself stated for this transaction. The figure is computed from "
            "that share count times a reference price: a market closing price "
            "around the filing's transaction date. The filing itself states no "
            "price."
        )
    else:
        amount_sentence = f"The filing states a value of {_e(row.amount_label)}."
    opening = (
        f"On {_e(row.when.isoformat())}, {_e(row.filer)}{role} filed regarding "
        f"{_e(row.what)} in {_e(row.issuer)}{ticker}, {filing_link}. {amount_sentence}"
    )
    news_items = _fetch_news_memoized(
        news_fetcher, news_cache, ticker=row.ticker or None, cik=None, around=row.when
    )
    news_sentence = _news_prose(news_items, row.issuer)
    return f'<div{id_attr} class="filing"><p>{opening}</p><p>{news_sentence}</p></div>'


def _overview_section_prose(
    section,
    *,
    news_fetcher: NewsFetcher | None,
    news_cache: dict,
) -> str:
    """One fortnight-overview category, written as prose per row rather than a table.

    Renders the section's own explanation when it has no rows -- the empty state is
    unchanged from `_section_table`, since a quiet section deserves the same honest
    coverage note as before, just without a `<table>` shell around zero rows.
    """
    if not section.rows:
        return f"<p>{_e(section.empty_note)}</p>"
    return "".join(
        _overview_row_prose(row, news_fetcher=news_fetcher, news_cache=news_cache)
        for row in section.rows
    )


def _trade_describe(t) -> str:
    """One Form 4 transaction line, honest about what the code actually means.

    P and S are open-market discretionary decisions and are described as bought/sold.
    Everything else -- grants, tax withholding, gifts, derivative exercises -- is
    described as what it is, never as a buy or a sell, per the reader's own
    correction on the State Street block.
    """
    code = t.code.upper()
    shares = f"{t.shares:,} shares" if t.shares else "an unstated number of shares"
    if code == "P":
        clause = f"bought {shares}"
    elif code == "S":
        clause = f"sold {shares}"
    elif code == "A":
        clause = f"was granted {shares}"
    elif code == "F":
        clause = f"had {shares} withheld for tax"
    elif code == "G":
        clause = f"gifted {shares}"
    elif code in {"M", "X"}:
        clause = f"exercised a derivative for {shares}"
    elif code == "C":
        clause = f"converted a derivative into {shares}"
    elif code == "D":
        clause = f"disposed of {shares}"
    else:
        clause = f"reported code {_e(t.code)} on {shares}"
    price_clause = f" at ${t.price:,.2f} per share" if t.price else ""
    date_clause = f" on {_e(t.on.isoformat())}" if t.on else " on an unstated date"
    return f"{clause} of {_e(t.issuer)} ({_e(t.symbol)}){price_clause}{date_clause}"


def _weekly_filing_prose(
    snap,
    f,
    *,
    store: Store | None,
    news_fetcher: NewsFetcher | None,
    news_cache: dict,
) -> str:
    """A written note on one fast-clock filing (13D/G or Form 3/4/5) inside a tracked
    manager's weekly block -- the per-manager sections that used to render as a bare
    row per filing with no explanation at all.
    """
    from whale_agent.ingestion.fund_watchlist import filing_index_url  # noqa: F401

    anchor = item_anchor(accession=f.accession, url=f.url)
    id_attr = f' id="{_e(anchor)}"' if anchor else ""

    if f.detail is not None:
        d = f.detail
        is_new = "/A" not in f.form.upper()
        shares_clause = f", {d.shares:,} shares of the class" if d.shares else ""
        pct = f"{d.percent:g}%" if d.percent is not None else "an undisclosed percent"
        opening = (
            f"On {_e(f.filed.isoformat())}, {_e(snap.name)} filed a {_e(f.form)}. "
            f"It disclosed {'a new position' if is_new else 'an amended stake'} of "
            f"{pct} of {_e(d.issuer)}{shares_clause}. "
            f"{_link(f.url, 'The filing itself is here')}. A 13D/13G states percent "
            "of class and share count only. It carries no transaction price. "
            "So no dollar value can honestly be attached to it."
        )
        issuer_key = d.issuer or d.issuer_cusip
        fact = (
            prior_position(store, snap.cik, snap.name, issuer_key, f.filed)
            if store is not None and issuer_key
            else None
        )
        position_sentence = _prior_position_prose(fact, snap.name)
        news_items = _fetch_news_memoized(
            news_fetcher, news_cache, ticker=None, cik=None, around=f.filed
        )
        news_sentence = _news_prose(news_items, d.issuer or "the issuer")
        body = f"<p>{opening}</p><p>{position_sentence}</p><p>{news_sentence}</p>"
    elif f.trades:
        issuer = f.trades[0].issuer
        symbol = f.trades[0].symbol
        sentences = "; ".join(_trade_describe(t) for t in f.trades)
        opening = (
            f"On {_e(f.filed.isoformat())}, {_e(snap.name)} filed a Form {_e(f.form)}. "
            f"It reported that it {sentences}. "
            f"{_link(f.url, 'The filing itself is here')}. "
            "Codes P and S are the only open-market discretionary trades here. "
            "Grants, tax withholding, gifts and derivative exercises are described "
            "above as what they are. They are not counted as a buy or a sell."
        )
        fact = (
            prior_position(store, snap.cik, snap.name, issuer, f.filed)
            if store is not None and issuer
            else None
        )
        position_sentence = _prior_position_prose(fact, snap.name)
        news_items = _fetch_news_memoized(
            news_fetcher, news_cache, ticker=symbol or None, cik=None, around=f.filed
        )
        news_sentence = _news_prose(news_items, issuer or "the issuer")
        body = f"<p>{opening}</p><p>{position_sentence}</p><p>{news_sentence}</p>"
    else:
        opening = (
            f"On {_e(f.filed.isoformat())}, {_e(snap.name)} filed a {_e(f.form)}, "
            f"{_link(f.url, 'the filing itself is here')}. No further detail was "
            "parsed out of this filing."
        )
        body = f"<p>{opening}</p>"

    return f'<div{id_attr} class="filing">{body}</div>'


def _fund_moves_prose(
    snapshots: list,
    on: date,
    since: date,
    *,
    store: Store | None,
    news_fetcher: NewsFetcher | None,
    news_cache: dict,
) -> str:
    """Every tracked manager's fast-clock filings this window, written as prose per
    filing rather than the bare table `_fund_moves_block` renders for the email."""
    moved = [s for s in snapshots if s.error is None and s.recent_filings]
    if not moved:
        return "<p>No tracked manager filed a stake disclosure in this window.</p>"
    moved.sort(key=lambda s: len(s.recent_filings), reverse=True)
    blocks = []
    for snap in moved:
        rows = "".join(
            _weekly_filing_prose(
                snap, f, store=store, news_fetcher=news_fetcher, news_cache=news_cache
            )
            for f in snap.recent_filings
        )
        blocks.append(f'<h3 id="{_e(manager_anchor(snap.name))}">{_e(snap.name)}</h3>{rows}')
    return "".join(blocks)


def _roster_entry_prose(snap, on: date) -> str:
    """One tracked manager's roster line, written as prose -- including an explicit
    flag when its most recent 13F on file is unusually old, rather than silently
    presenting a stale report period as if it were current (reader-facing bug: the
    BlackRock roster row was quietly showing a 2016 report period next to 2026
    filings elsewhere on the same page)."""
    from whale_agent.summarization.tracked_funds import format_usd_short

    anchor = item_anchor(url=snap.latest_13f_url) if snap.latest_13f_url else ""
    id_attr = f' id="{_e(anchor)}"' if anchor else ""

    if not snap.has_13f or snap.period_end is None:
        return (
            f'<div{id_attr} class="filing"><p>No 13F is on file for '
            f"{_e(snap.name)} in this system.</p></div>"
        )

    as_of = snap.period_end
    age_days = (on - as_of).days
    scaled_clause = (
        " Values on the filing itself were reported in thousands and have been "
        "scaled up here to whole dollars."
        if snap.values_scaled_from_thousands
        else ""
    )
    staleness = ""
    if age_days > 400:
        years = age_days / 365.25
        staleness = (
            f" This is unusually old. SEC EDGAR's own submissions history has no "
            f"13F-HR filed by this CIK more recently than the quarter ended "
            f"{_e(as_of.isoformat())}, about {years:.0f} year"
            f"{'s' if years >= 1.5 else ''} before this report. This is not a data "
            "error on our side. It is the newest 13F-HR this manager's tracked "
            "filing entity has on file. The figures above describe a book from that "
            "date. They do not describe this manager's current holdings."
        )
    opening = (
        f"A 13F tracks {_e(snap.name)}. Its most recent 13F-HR on file reports "
        f"{snap.position_count:,} stock positions worth "
        f"{format_usd_short(snap.total_value_usd)} in aggregate reported value. "
        f"The report period is the quarter ended {_e(as_of.isoformat())}"
        + (
            f". {_link(snap.latest_13f_url, 'The filing itself is here')}."
            if snap.latest_13f_url
            else "."
        )
        + scaled_clause
        + staleness
    )
    return f'<div{id_attr} class="filing"><p>{opening}</p></div>'


def _roster_prose(snapshots: list, on: date) -> str:
    usable = [s for s in snapshots if s.error is None and s.has_13f]
    if not usable:
        return "<p>No tracked manager has a 13F on file in this system yet.</p>"
    ranked = sorted(usable, key=lambda s: s.total_value_usd, reverse=True)
    return "".join(_roster_entry_prose(s, on) for s in ranked)


def _sitebar(on: date, present: list[str], current: str) -> str:
    """The persistent cross-page nav bar: every present page, on every page, so a
    reader moving between sections never has to backtrack through the index. `present`
    is the ordered subset of `_PAGE_ORDER` that this render actually produced (a page
    with no data for this week is not linked to, since it would 404)."""
    links = []
    for slug in present:
        filename = tracked_filings_page_filename(on, slug)
        cls = ' class="current"' if slug == current else ""
        links.append(f'<a href="{filename}"{cls}>{_e(_PAGE_TITLES[slug])}</a>')
    return '<nav class="sitebar" aria-label="Report pages">' + "".join(links) + "</nav>"


def _pagenav(on: date, present: list[str], current: str) -> str:
    """Prev/next links between pages, in `_PAGE_ORDER` order, so a reader reading
    front-to-back can move forward without hunting the sitebar for the next slug."""
    idx = present.index(current)
    prev_html = ""
    next_html = ""
    if idx > 0:
        prev_slug = present[idx - 1]
        prev_html = (
            f'<a href="{tracked_filings_page_filename(on, prev_slug)}">'
            f"&laquo; {_e(_PAGE_TITLES[prev_slug])}</a>"
        )
    if idx < len(present) - 1:
        next_slug = present[idx + 1]
        next_html = (
            f'<a href="{tracked_filings_page_filename(on, next_slug)}">'
            f"{_e(_PAGE_TITLES[next_slug])} &raquo;</a>"
        )
    if not prev_html and not next_html:
        return ""
    return f'<nav class="pagenav" aria-label="Page navigation"><span>{prev_html}</span><span>{next_html}</span></nav>'


def _render_page(*, on: date, slug: str, present: list[str], dek: str, body: str) -> str:
    """Wraps one section's body in a complete, well-formed HTML document, reusing
    `_PAGE_CSS` and the same header pattern every page in this report shares, plus the
    persistent cross-page nav bar and prev/next links (`_sitebar` / `_pagenav`)."""
    title = _PAGE_TITLES[slug]
    # `present` is not known until every section has been examined (a page with no
    # data for the week is simply absent, and every other page's nav must not link to
    # it). Rather than thread the full page list through every block builder, this
    # renders with a placeholder here; render_tracked_filings_page patches it in once
    # the full set is known -- see the loop at the end of that function.
    if present:
        sitebar = _sitebar(on, present, slug)
        pagenav = _pagenav(on, present, slug)
    else:
        sitebar = '<nav class="sitebar" aria-label="Report pages"></nav>'
        pagenav = ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}, {_e(on.isoformat())}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
{sitebar}
<header class="hd">
<h1>{_e(title)}, week of {_e(on.isoformat())}</h1>
<p class="dek">{dek}</p>
</header>
<main class="page">
{body}
{pagenav}
</main>
</body></html>"""


def render_tracked_filings_page(
    trades: list,
    stakes: list,
    on: date,
    snapshots: list | None = None,
    sections: list | None = None,
    since: date | None = None,
    market_stakes: list | None = None,
    resale: list | None = None,
    store: Store | None = None,
    news_fetcher: NewsFetcher | None = None,
    quarterly: list | None = None,
) -> dict[str, str]:
    """The tracked-manager report, as a small library of pages rather than one long
    scroll: a landing page with a real table of contents, plus one complete HTML
    document per major section, keyed by filename
    (`tracked_filings_page_filename(on, slug)`) in `PAGE_SLUGS` order.

    A written note per pooled trade and stake, each carrying its own anchor, on the
    "trades-and-stakes" page -- the page every per-item email link deep-links into for
    that filing. When `snapshots`, `sections`, `market_stakes` and/or `resale` are
    given, those sections become their own pages with the same row-level anchors the
    email uses, so every row the email links to has a matching id on the specific page
    that section lives on (see `PAGE_SLUGS` for the anchor-to-page mapping).

    `store` and `news_fetcher` are both optional and both used to answer "was there a
    prior position" and "is there other news", respectively. Omitting either is
    honest, not silent: the prose says plainly that the lookup was not run for this
    render, rather than pretending the question was answered. `news_fetcher` is called
    at most once per (ticker-or-cik, filing date) across the whole render -- see
    `_fetch_news_memoized` -- so a manager's own repeat filings on the same issuer in
    one run cost one HTTP round trip, not one per filing.
    """
    news_cache: dict = {}
    pages: dict[str, str] = {}
    menu_items: list[tuple[str, str]] = []  # (slug, blurb) for the index page's menu

    trade_blocks: list[str] = [
        _trade_prose(t, store=store, news_fetcher=news_fetcher, news_cache=news_cache)
        for t in trades
    ]
    trade_blocks.extend(
        _stake_prose(s, store=store, news_fetcher=news_fetcher, news_cache=news_cache)
        for s in stakes
    )
    trades_body = "".join(trade_blocks) or "<p>No pooled trade or stake this window.</p>"

    quarterly_body = ""
    if quarterly:
        quarterly_body = (
            '<h2 id="sec-quarterly">Quarterly 13F filings this week</h2>'
            + "".join(
                _quarterly_filing_prose(fc, news_fetcher=news_fetcher, news_cache=news_cache)
                for fc in quarterly
            )
        )

    # The trades-and-stakes page always exists (even with nothing pooled this window,
    # it says so plainly), so it and the index are the only two pages present by
    # default. Every other page below is only ever linked to, or written, when there
    # is a section to put on it -- a page with no content for the week would otherwise
    # 404 on every link the sitebar and email built pointing at it.
    trades_dek = (
        "The filings behind this week's largest trades and stakes, each with its own "
        "link back to the primary source on SEC EDGAR."
    )
    pages[tracked_filings_page_filename(on, "trades")] = _render_page(
        on=on,
        slug="trades",
        present=[],  # patched below once every present page is known
        dek=trades_dek,
        body=quarterly_body
        + '<h2 id="sec-trades">This week\'s trades and stakes</h2>'
        + trades_body,
    )
    menu_items.append(("trades", trades_dek))

    if snapshots or sections or market_stakes or resale:
        # Imported here, not at module load: this module has no other dependency on
        # the email renderer, and importing lazily avoids a load-time cycle between
        # the two publishing surfaces.
        from whale_agent.summarization.render_weekly_html import (
            _market_stakes_block,
            _resale_block,
        )

        if sections:
            body = '<h2 id="sec-overview">The full fortnight overview</h2>' + "".join(
                f'<h3 id="{_e(section_anchor(s.label))}">{_e(s.label)}</h3>'
                + _overview_section_prose(s, news_fetcher=news_fetcher, news_cache=news_cache)
                for s in sections
            )
            dek = (
                "Every filing this fortnight's screen picked up, by category: insider "
                "filings, congressional trading, institutional moves, and international "
                "plays."
            )
            pages[tracked_filings_page_filename(on, "overview")] = _render_page(
                on=on, slug="overview", present=[], dek=dek, body=body
            )
            menu_items.append(("overview", dek))

        if market_stakes:
            body = (
                '<h2 id="sec-bigstakes">Big stakes disclosed this week</h2>'
                + _market_stakes_block(market_stakes, on, since or on)
            )
            dek = "Market-wide 5% stake crossings this week, independent of the watchlist."
            pages[tracked_filings_page_filename(on, "bigstakes")] = _render_page(
                on=on, slug="bigstakes", present=[], dek=dek, body=body
            )
            menu_items.append(("bigstakes", dek))

        if resale:
            body = '<h2 id="sec-resale">Arranging to sell</h2>' + _resale_block(
                resale, on, since or on
            )
            dek = (
                "Registration statements naming a tracked fund as a selling "
                "securityholder, the step that runs weeks ahead of any Form 4."
            )
            pages[tracked_filings_page_filename(on, "resale")] = _render_page(
                on=on, slug="resale", present=[], dek=dek, body=body
            )
            menu_items.append(("resale", dek))

        if snapshots:
            move_since = since or on
            body = (
                '<h2 id="sec-moves">What every tracked manager filed</h2>'
                + _fund_moves_prose(
                    snapshots,
                    on,
                    move_since,
                    store=store,
                    news_fetcher=news_fetcher,
                    news_cache=news_cache,
                )
            )
            dek = "Every fast-clock filing (13D/G, Form 3/4/5) from every tracked manager this window."
            pages[tracked_filings_page_filename(on, "moves")] = _render_page(
                on=on, slug="moves", present=[], dek=dek, body=body
            )
            menu_items.append(("moves", dek))

            body = '<h2 id="sec-roster">The full tracked-manager roster</h2>' + _roster_prose(
                snapshots, on
            )
            dek = "Every manager this system tracks, and its most recent 13F on file."
            pages[tracked_filings_page_filename(on, "roster")] = _render_page(
                on=on, slug="roster", present=[], dek=dek, body=body
            )
            menu_items.append(("roster", dek))

    present = ["index"] + [slug for slug, _ in menu_items]

    # Patch every already-rendered page with the real sitebar/pagenav now that the
    # full set of present pages is known -- cheaper than threading `present` through
    # every block builder above, and _render_page's sitebar/pagenav markup depends
    # only on `on`, `present`, and the page's own slug, all of which are known here.
    for slug, _dek in menu_items:
        filename = tracked_filings_page_filename(on, slug)
        pages[filename] = (
            pages[filename]
            .replace(
                '<nav class="sitebar" aria-label="Report pages"></nav>',
                _sitebar(on, present, slug),
            )
            .replace(
                "\n\n</main>",
                f"\n{_pagenav(on, present, slug)}\n</main>",
            )
        )

    menu_html = (
        '<ul class="menu">'
        + "".join(
            f'<li><a href="{tracked_filings_page_filename(on, slug)}">'
            f'<span class="mt">{_e(_PAGE_TITLES[slug])}</span>'
            f'<span class="ms">{_e(blurb)}</span></a></li>'
            for slug, blurb in menu_items
        )
        + "</ul>"
    )

    index_dek = (
        "This week's tracked-manager report, split into one page per section. Pick a "
        "section below."
    )
    index_body = menu_html
    pages[tracked_filings_page_filename(on, "index")] = _render_page(
        on=on, slug="index", present=present, dek=index_dek, body=index_body
    )
    pages[tracked_filings_page_filename(on, "index")] = pages[
        tracked_filings_page_filename(on, "index")
    ].replace("\n\n</main>", "\n</main>")

    return pages


def write_tracked_filings_page(
    pages: dict[str, str], out_dir: Path | str, on: date
) -> dict[str, Path]:
    """Writes every page `render_tracked_filings_page` produced, keyed by the same
    filenames, and returns the paths they were written to. `on` is accepted (though
    the filenames already carry the date, via `tracked_filings_page_filename`) to
    keep this function's signature symmetric with `render_tracked_filings_page`'s and
    to match how callers already have `on` in scope."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for filename, html_body in pages.items():
        path = out_dir / filename
        path.write_text(html_body, encoding="utf-8")
        written[filename] = path
    return written
