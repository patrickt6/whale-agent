"""Weekly report: aggregation and the permanent "how to read this" block.

The daily digest answers "what happened today". The weekly answers "what does the week
look like", which is where patterns live: names bought by several unrelated filers,
first-time filers appearing, the same issuer surfacing in more than one jurisdiction.

The evidence block at the bottom is not boilerplate and must not be trimmed. This
product's honest position is that disclosed-position data is weak, decayed, and mostly
uninformative; a reader who forgets that will misuse the digest. It sits directly
adjacent to the Taiwan section because Taiwan is the market of interest and the Taiwanese
evidence is the least encouraging of all.

The weekly is also where the thesis layer lands. Patterns are detected over the week's
events, the ones that clear the bar become article pages written to disk, and the email
*links* to them rather than inlining them: a research note is a page, and an email that
carries three of them is not read. When nothing clears the bar the weekly still sends and
says so in one sentence. Padding a quiet week with a manufactured thesis would cost the
only thing this product has.

Usage:
    python -m whale_agent.jobs.digest_weekly
    python -m whale_agent.jobs.digest_weekly --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from whale_agent.analysis.patterns import detect_patterns
from whale_agent.config import Settings, get_settings, load_env_file
from whale_agent.delivery.base import DeliveryResult
from whale_agent.delivery.email import send_digest_email
from whale_agent.enrichment.macro import (
    ChainedMacroProvider,
    FredMacroProvider,
    TreasuryMacroProvider,
)
from whale_agent.enrichment.plausibility import is_placeholder_issuer, screen
from whale_agent.enrichment.tickers import default_ticker_resolver, fill_issuer_names
from whale_agent.ingestion.fund_watchlist import (
    WATCHLIST,
    fetch_market_stakes,
    fetch_watchlist,
    pool_top_stakes,
    pool_top_trades,
)
from whale_agent.ingestion.resale_watch import fetch_resale_registrations
from whale_agent.jobs.overview import OVERVIEW_DAYS, build_overview
from whale_agent.models.annotation import ContextAnnotation
from whale_agent.models.enums import TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.models.thesis import Thesis
from whale_agent.monitoring.health import coverage_notes_for_stale_sources
from whale_agent.monitoring.link_check import (
    Fetcher,
    ReportLink,
    broken_links,
    default_fetcher,
)
from whale_agent.monitoring.quarantine import coverage_note, default_log
from whale_agent.monitoring.sampled_review import default_review_log
from whale_agent.profiles import ProfileError, resolve_config
from whale_agent.publishing.article_html import article_filename, write_article
from whale_agent.publishing.issue_html import issue_filename, write_issue
from whale_agent.publishing.tracked_filings_html import (
    filing_anchor,
    item_anchor,
    manager_anchor,
    render_tracked_filings_page,
    section_anchor,
    tracked_filings_filename,
    tracked_filings_page_filename,
    write_tracked_filings_page,
)
from whale_agent.scoring.score import passes_gate
from whale_agent.storage.db import Store
from whale_agent.summarization.issue import build_issue
from whale_agent.summarization.llm import LLMProvider, get_provider
from whale_agent.summarization.macro_context import backdrop_lines, macro_note_for
from whale_agent.summarization.render import format_usd
from whale_agent.summarization.render_weekly_html import render_weekly_html
from whale_agent.summarization.thesis import generate_theses
from whale_agent.summarization.tracked_funds import (
    aggregate_figure_tokens,
    render_fund_moves,
    render_market_stakes,
    render_resale_watch,
    render_top_trades,
    render_tracked_funds,
)
from whale_agent.summarization.weekly_headline import HeadlineFacts, build_headline

log = logging.getLogger(__name__)

BUY_TYPES = {
    TransactionType.OPEN_MARKET_BUY,
    TransactionType.ACTIVIST_13D,
    TransactionType.FUND_NEW_POSITION,
    TransactionType.FUND_ADD_POSITION,
}

HOW_TO_READ = """\
HOW TO READ THIS (permanent block: the evidence, not the pitch)

- People sell shares for many reasons, such as taxes or diversification, so a sale says
  less about the filer's view than a purchase.
- Cohen, Malloy and Pomorski (2012) separate routine, calendar-driven insider trades from
  opportunistic ones. Rows here are classified on that basis where the
  filer's own disclosure history allows it, which today is no row at all: the test
  needs three years of a filer's dates and no filer on record yet has more than one.
  Treat every row below as unclassified rather than as opportunistic.
- The effect was concentrated in small caps and has decayed substantially since the
  research was published. Assume less signal today than the papers report.
- 13F data is 45 to 135 days stale by the time it is public, long-only, and excludes
  shorts, derivatives and non-US holdings. It describes a past portfolio, not a
  current view.
- Taiwan specifically: Chiang et al. (2004) found no reliable abnormal returns to
  insider trading disclosures in the Taiwanese market. Taiwan coverage exists here
  because it is the market of interest, not because the evidence supports trading it.
- This is an awareness tool. It is not investment advice, and every figure in it is
  copied from a filing field rather than computed by a model.\
"""


# A week yields 10-30 gated events. More than a few notes off that many rows means the
# notes are describing the same filings from different angles, which reads as volume and
# is not. The cap is a ceiling, never a target: fewer is the normal outcome.
MAX_ARTICLES_PER_WEEK = 3

NO_THESIS_LINE = (
    "No pattern in this week's filings cleared the evidence bar, so there is no research "
    "note this week. That is the common outcome and nothing was written to fill the space."
)


def _company_names(settings: Settings) -> dict[str, str]:
    """SEC ticker-to-name map, or an empty one if it cannot be had.

    Never raises: a report that fails because a cosmetic name lookup was unavailable
    would trade the whole week's delivery for a nicer noun.
    """
    try:
        resolver = default_ticker_resolver(settings)
        return resolver.company_names() if hasattr(resolver, "company_names") else {}
    except Exception as exc:  # noqa: BLE001 - cosmetic enrichment must not cost the send
        log.warning("Company-name resolution unavailable: %s", exc)
        return {}


def macro_snapshot(settings: Settings) -> dict:
    """Whatever macro series are readable right now, or nothing.

    Chained deliberately: Treasury is keyless and always tried, FRED is layered over it
    when a key exists and supplies the two series Treasury cannot cover at all, bank
    credit and payrolls. A failure at either returns an empty snapshot, and the report
    then says it could not read them rather than printing last week's figures.
    """
    providers: list = []
    if settings.fred_enabled:
        providers.append(FredMacroProvider(settings))
    if settings.treasury_enabled:
        providers.append(TreasuryMacroProvider(settings))
    if not providers:
        return {}
    try:
        return ChainedMacroProvider(*providers).snapshot()
    except Exception as exc:  # noqa: BLE001 - background context must not cost the send
        log.warning("Macro snapshot unavailable: %s", exc)
        return {}


def macro_sourcing_line(settings: Settings) -> str:
    """Where the figures came from, and what is missing, in one sentence."""
    if settings.fred_enabled:
        return (
            "Rates from Treasury Fiscal Data, bank credit and payrolls from FRED. "
            "Treasury figures are average interest by security type, used as a proxy."
        )
    return (
        "Rates from Treasury Fiscal Data, reported as average interest by security type "
        "and used as a proxy for the policy rate and the long yield. Bank credit and "
        "payrolls need a FRED API key, which is free and not currently set."
    )


def attach_macro_notes(
    theses: list[Thesis],
    events: list[NormalizedEvent],
    snapshot: dict,
    on: date,
) -> list[Thesis]:
    """Add a dated backdrop line to the notes whose sector supports one.

    Each thesis is matched to its own supporting filings by event id, so the sector test
    runs against the rows the note actually argues about rather than the whole week. Most
    notes get nothing back, because sector is populated on a small minority of rows and a
    macro figure attached to an unknown sector is decoration.

    Appends rather than replaces: `context_notes` already carries what enrichment learned,
    and the backdrop is one more piece of context, not a substitute for it.
    """
    if not snapshot:
        return theses
    by_id = {ev.event_id: ev for ev in events}
    out: list[Thesis] = []
    for thesis in theses:
        supporting = [by_id[eid] for eid in thesis.event_ids if eid in by_id]
        note = macro_note_for(supporting, snapshot, on)
        out.append(
            thesis.model_copy(update={"context_notes": [*thesis.context_notes, note]})
            if note
            else thesis
        )
    return out


def screen_for_report(
    events: list[NormalizedEvent], withheld: list | None = None
) -> list[NormalizedEvent]:
    """Drop implausible rows before anything renders them.

    `enrichment/plausibility.py` marks and excludes rather than deleting, precisely so a
    vendor schema change stays visible as a rejection count instead of a silent gap. The
    consequence is that the rows are still in storage, and **every reader of the store has
    to screen again**. The daily pipeline does this inside `build_ranked`; the weekly read
    straight past it and put a $1.1 quadrillion insider row into a reader-facing table.

    Storage now screens on read, so this is the second of two independent passes rather
    than the only one. It is kept because it is the pass that produces the reader-facing
    count: `withheld`, when given, collects the rejections so the report can say what it
    is not showing.
    """
    kept, rejected = screen(events, sink=default_log())
    if rejected:
        log.warning(
            "Excluded %d implausible row(s) from the weekly; first: %s",
            len(rejected),
            rejected[0][1].reasons[0] if rejected[0][1].reasons else "unknown",
        )
    if withheld is not None:
        withheld.extend(rejected)
    return kept


def _window(on: date, days: int) -> date:
    return on - timedelta(days=days)


def most_bought(events: list[NormalizedEvent], top: int = 10) -> list[tuple[str, int, float]]:
    """Issuers with the most distinct buying filers this week, with total disclosed USD.

    Ranked by *number of independent filers*, not dollars: five unrelated people buying
    the same name is a different observation from one person buying a lot of it.
    """
    filers_by_issuer: dict[str, set[str]] = defaultdict(set)
    usd_by_issuer: dict[str, float] = defaultdict(float)
    for ev in events:
        if ev.transaction_type not in BUY_TYPES:
            continue
        if is_placeholder_issuer(ev.issuer_name):
            # An unpopulated vendor field is not a company. Ranking it credits a name
            # that does not exist with other companies' money.
            continue
        key = ev.issuer_name
        filers_by_issuer[key].add((ev.filer_id or ev.filer_name).lower())
        usd_by_issuer[key] += ev.effective_usd or 0.0
    ranked = sorted(
        filers_by_issuer.items(),
        key=lambda kv: (len(kv[1]), usd_by_issuer[kv[0]]),
        reverse=True,
    )
    return [(name, len(filers), usd_by_issuer[name]) for name, filers in ranked[:top]]


def first_time_filers(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
    return [ev for ev in events if ev.is_first_time_filer]


def cross_jurisdiction(events: list[NormalizedEvent]) -> list[tuple[str, list[str]]]:
    """Issuers that appeared in more than one jurisdiction's filings this week."""
    by_issuer: dict[str, set[str]] = defaultdict(set)
    for ev in events:
        by_issuer[ev.issuer_name].add(ev.jurisdiction.value)
    return [(name, sorted(codes)) for name, codes in by_issuer.items() if len(codes) > 1]


def weekly_theses(
    events: list[NormalizedEvent],
    on: date,
    provider: LLMProvider | None = None,
    limit: int = MAX_ARTICLES_PER_WEEK,
) -> list[Thesis]:
    """Detect this week's patterns and write up the ones that survive.

    No baseline is passed to `detect_patterns`, so the versus-history detector does not
    run: claiming an issuer is unusually active requires knowing its norm, and the weekly
    job does not compute one. A detector that cannot be honest is better switched off than
    run against an assumed norm of zero.
    """
    patterns = detect_patterns(events)
    return generate_theses(patterns, provider, on, limit=limit)


# Where the render step leaves the week's written-up theses for the send step to pick
# up. The CI job invokes this module twice for one weekly, once with --dry-run to write
# and deploy the pages and once to send the email, and that ordering is deliberate: no
# link in a delivered email may point at a page that does not exist yet. Without a cache
# the second invocation regenerated every thesis, which meant paying the model twice for
# one report and throwing the first set of answers away (three patterns in a typical
# week, so three wasted calls).
#
# A file rather than a flag on the send step, because the cached object is the thing
# that has to survive: the email carries each thesis title and claim, not just a link, so
# reusing the already-written pages would still leave the send step without the text.
# `Thesis` is a pydantic model and already serialises losslessly.
#
# The leading dot keeps it out of the deployed asset bundle (wrangler ignores dotfiles),
# so an internal cache written into deploy/public is not served as a public URL.
THESES_CACHE_PREFIX = ".theses-"


def theses_cache_path(articles_dir: Path, on: date) -> Path:
    """One cache per report date, so last week's notes can never be sent as this week's.

    Deliberately a sibling of the articles directory rather than a file inside it.
    Wrangler publishes dotfiles: an earlier version wrote `.theses-<date>.json` into
    `deploy/public` on the assumption that the dot prefix excluded it, and after a
    deploy the file was served at the public URL with HTTP 200. An `.assetsignore`
    would not help, because the assets directory is generated and gitignored, so CI
    would never have the ignore file. Keeping the cache out of the published tree is
    the only version of this that cannot regress.

    The two CI steps share a working directory, so a sibling path is just as reachable
    for the handoff as one inside the directory was.
    """
    articles = Path(articles_dir)
    return articles.parent / ".weekly-cache" / f"{THESES_CACHE_PREFIX}{on.isoformat()}.json"


class _CountingProvider:
    """Passes calls through and records how many actually returned a response.

    Not a metric for its own sake: it is the only signal available to the caller that a
    thesis carries model-written prose rather than the deterministic fallback, because
    every failure inside `generate_thesis` is swallowed there by design.
    """

    def __init__(self, wrapped) -> None:
        self.wrapped = wrapped
        self.completions = 0
        self.name = getattr(wrapped, "name", "none")

    def complete(self, system: str, user: str) -> str:
        text = self.wrapped.complete(system, user)
        self.completions += 1
        return text


def save_theses(path: Path, theses: list[Thesis], *, model_wrote_prose: bool = True) -> None:
    """Persist the theses for the send step, unless they are deterministic fallbacks.

    `generate_thesis` returns the deterministic article whenever the model is
    unreachable: no key, transport error, spend cap, or a reply that failed the
    provenance gate. Caching that makes the failure sticky, turning the render step's
    transient outage into the send step's permanent one, and the reader gets the
    template headline for the week even though the API recovered in between. A run that
    produced no prose therefore writes nothing and lets the send step try again.
    """
    if not model_wrote_prose:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [t.model_dump(mode="json") for t in theses]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_cached_theses(path: Path) -> list[Thesis] | None:
    """The theses this week's render step already wrote, or None to generate them.

    None on anything unexpected, never an exception: a cache is an optimisation, and a
    truncated or hand-edited file must cost an LLM call rather than the week's report.
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return None
        return [Thesis.model_validate(item) for item in payload]
    except Exception as exc:  # noqa: BLE001 - an unusable cache is simply no cache
        log.warning("Ignoring unusable thesis cache %s: %s", path, exc)
        return None


def publish_articles(
    theses: list[Thesis],
    events: list[NormalizedEvent],
    out_dir: Path,
    base_url: str = "",
) -> list[tuple[Thesis, str]]:
    """Write one page per thesis and return each with the link the email should carry.

    Each page is drawn with only its own pattern's filings, looked up by event id: the
    header chart must show the rows the article argues about and no others.
    """
    by_id = {ev.event_id: ev for ev in events}
    published: list[tuple[Thesis, str]] = []
    for thesis in theses:
        supporting = [by_id[eid] for eid in thesis.event_ids if eid in by_id]
        write_article(thesis, out_dir, supporting)
        # Never a file:// path: it is dead the moment it leaves the machine that
        # wrote it, and "Read the note" degrades to nothing (see `_link` in
        # render_weekly_html.py) rather than a button that resolves nowhere.
        link = public_report_link(base_url, article_filename(thesis))
        published.append((thesis, link))
    return published


def render_weekly(
    events: list[NormalizedEvent],
    on: date,
    annotations: list[ContextAnnotation] | None = None,
    articles: list[tuple[Thesis, str]] | None = None,
    coverage_notes: list[str] | None = None,
    tracked_funds: list[str] | None = None,
) -> str:
    """Render the weekly report. Fully deterministic: no prose layer, no LLM."""
    start = _window(on, 7)
    # Two numbers, because they mean different things and conflating them overstates the
    # week. The window deliberately retains sub-threshold rows: a cluster is gated on its
    # aggregate, so four $2M buys into one issuer are a finding even though no single one
    # is a whale. Reporting the window size as the whale count is the one error the
    # product cannot afford, and it is what this line used to do.
    cleared = sum(1 for ev in events if passes_gate(ev))
    lines = [
        f"WHALE WEEKLY, {start.isoformat()} to {on.isoformat()}",
        f"{cleared} disclosed moves cleared the threshold this week, "
        f"out of {len(events)} recorded.",
    ]
    # Surfaced the same way a degraded source is: the reader is told what is missing.
    for note in coverage_notes or []:
        lines.append(f"Coverage note: {note}")

    # Placed above the week's aggregation, and clearly labelled, because it answers a
    # different question on a different clock: standing positions rather than seven days
    # of filings. Below the aggregation it would read as this week's activity.
    if tracked_funds:
        lines.append("")
        lines.extend(tracked_funds)

    if articles:
        lines.append("\nRESEARCH NOTES THIS WEEK")
        for thesis, link in articles:
            lines.append(f"  {thesis.title}")
            lines.append(f"    {thesis.claim}")
            lines.append(f"    Read it: {link}")
    else:
        lines.append("\n" + NO_THESIS_LINE)

    bought = most_bought(events)
    if bought:
        lines.append("\nMOST-BOUGHT NAMES (ranked by number of independent filers)")
        for name, n_filers, total in bought:
            lines.append(f"  {name}: {n_filers} filer(s), {format_usd(total)} disclosed")

    firsts = first_time_filers(events)
    if firsts:
        lines.append("\nFIRST-TIME FILERS")
        for ev in firsts[:15]:
            lines.append(
                f"  {ev.filer_name}: {format_usd(ev.effective_usd)} in {ev.issuer_name} "
                f"({ev.jurisdiction.value})"
            )

    crossings = cross_jurisdiction(events)
    if crossings:
        lines.append("\nSAME NAME, MULTIPLE JURISDICTIONS")
        for name, codes in crossings[:15]:
            lines.append(f"  {name}: {', '.join(codes)}")

    by_jurisdiction = Counter(ev.jurisdiction.value for ev in events)
    if by_jurisdiction:
        lines.append("\nCOVERAGE BY JURISDICTION")
        for code, count in by_jurisdiction.most_common():
            lines.append(f"  {code}: {count}")

    if annotations:
        lines.append("\nCONTEXT (lobbying and government contracts: slow context, not events)")
        for ann in annotations[:15]:
            amount = format_usd(ann.amount_usd)
            subject = ann.issuer_name or ann.ticker or "unknown"
            lines.append(f"  {subject}: {ann.label}, {amount} ({ann.as_of.isoformat()})")

    lines.append(
        "\nEvery figure is copied from a public filing or computed from filing fields. "
        "Awareness tool, not investment advice."
    )
    return "\n".join(lines)


def public_report_link(article_base_url: str, filename: str) -> str:
    """The public URL for a page written under `articles_dir`, or "" when there is
    none configured.

    Never a `file://` path. That used to be the fallback when --article-base-url was
    unset, which is a dead link the moment it leaves the machine that wrote it -- a
    button in an email that resolves to nothing on the reader's computer. With no
    public base configured the link is omitted rather than faked. As of the live
    Cloudflare site, "no public base configured" should no longer arise in practice --
    `Settings.article_base_url` defaults to it -- but the fallback stays for a caller
    that explicitly overrides it to "".

    Canonical, extensionless form: the deployed Workers static-assets site serves
    `/whale-weekly-2026-08-17` as the 200 and `/whale-weekly-2026-08-17.html` as a 307
    redirect to it. Every page this module writes still has a `.html` name on disk (that
    is what wrangler deploys), but the link handed to a reader is built without the
    extension so a click resolves in one hop rather than two.
    """
    base = (article_base_url or "").rstrip("/")
    if not base:
        return ""
    stem = filename[:-5] if filename.endswith(".html") else filename
    return f"{base}/{stem}"


def run_weekly_digest(
    store: Store,
    settings: Settings | None = None,
    *,
    on: date | None = None,
    deliver: bool = True,
    annotations: list[ContextAnnotation] | None = None,
    articles_dir: Path | None = None,
    article_base_url: str = "",
    html_out: str = "",
    attach_report: bool | None = None,
    link_fetcher: Fetcher = default_fetcher,
) -> tuple[str, list[DeliveryResult]]:
    s = settings or get_settings()
    on = on or date.today()
    # An explicit --article-base-url always wins; otherwise fall back to
    # Settings.article_base_url, which defaults to the live Cloudflare site. So a plain
    # run with no flag still produces working per-item links instead of needing the URL
    # typed on every invocation.
    base_url = article_base_url or s.article_base_url
    events = store.events_since(_window(on, 7))
    # Display-time only: fill in issuer names that arrived as nothing but their ticker,
    # so the ranking and the headlines say "Five Star Bancorp" rather than "FSBC". The
    # stored payload is left as the source wrote it.
    withheld: list = []
    events = screen_for_report(events, withheld)
    events = fill_issuer_names(events, _company_names(s))

    snapshot = macro_snapshot(s)

    # Articles are an upgrade to the weekly, exactly as prose is to the daily: if the
    # thesis layer fails, the aggregation email still goes out unchanged.
    articles: list[tuple[Thesis, str]] = []
    out_dir = articles_dir or Path("articles")
    # Every link the email is about to carry, collected as it is built, so the
    # reachability gate below checks the URLs actually going out rather than merely
    # confirming that publishing happened at some point.
    report_links: list[ReportLink] = []
    if s.include_research_notes:
        try:
            # Generated once per weekly run and reused by any later invocation for the
            # same date. Publishing still runs both times: writing the pages is
            # deterministic and free, and the send step needs the links regardless.
            cache = theses_cache_path(out_dir, on)
            theses = load_cached_theses(cache)
            if theses is None:
                # The provider is the honest witness to whether any prose was written.
                # Every failure path inside generate_thesis, no key, transport error,
                # spend cap, a reply that failed the provenance gate, ends on the
                # deterministic article and looks identical from the outside. Counting
                # completions that actually returned is what separates a real answer
                # from a silent fallback, and a run with none of them must not poison
                # the cache.
                provider = _CountingProvider(get_provider(s))
                generated = weekly_theses(events, on, provider if provider.wrapped else None)
                theses = attach_macro_notes(generated, events, snapshot, on)
                save_theses(cache, theses, model_wrote_prose=provider.completions > 0)
            else:
                log.info("Reusing %d thesis/theses from %s; no model call", len(theses), cache)
            if theses:
                articles = publish_articles(theses, events, out_dir, base_url)
                report_links.extend(
                    ReportLink(url=link, label=f"research note: {thesis.title}")
                    for thesis, link in articles
                    if link
                )
        except Exception as exc:  # noqa: BLE001 - delivery must not depend on publishing
            log.warning("Thesis layer failed; sending the weekly without articles: %s", exc)

    # Two different kinds of gap, told to the reader the same way: rows we withheld,
    # and sources that stopped answering. A core source going quiet never reaches here
    # -- the gate blocks the send before the weekly is rendered at all.
    notes = [n for n in [coverage_note(withheld)] if n]
    notes.extend(coverage_notes_for_stale_sources(store))

    # Off unless switched on, so the test suite and any offline run never reach EDGAR.
    # The workflow sets WHALE_TRACKED_FUNDS=1 explicitly.
    tracked: list[str] = []
    tracked_snapshots: list = []
    market_stakes: list = []
    stakes_examined = 0
    resale_examined = 0
    resale: list = []
    tracked_figures: set[str] = set()
    top_trades: list = []
    top_stakes: list = []
    quarterly_filings: list = []
    # Keyed by tracked_filings_html.PAGE_SLUGS slug ("trades", "overview", ...) to the
    # public URL of that page, or "" for a slug whose page was not written this run
    # (no data for it, or the appendix pages failed to build entirely). Replaces the
    # old single `tracked_report_link` string now that the report is a small library
    # of pages rather than one file -- see render_weekly_html's `tracked_report_link`
    # docstring for how the dict form is consumed.
    tracked_report_links: dict[str, str] = {}
    report_attachment_html = ""
    # Computed here rather than after the tracked-fund block below so the full report
    # page (which now carries the fortnight overview the email trims) can be built in
    # one pass. Same query the "last fortnight" HTML section uses.
    overview_events = fill_issuer_names(
        screen_for_report(store.events_since(_window(on, OVERVIEW_DAYS))), _company_names(s)
    )
    sections = build_overview(overview_events, on)
    if os.environ.get("WHALE_TRACKED_FUNDS", "").strip() in {"1", "true", "yes"}:
        try:
            window_start = _window(on, 7)
            tracked_snapshots = fetch_watchlist(
                api_key=s.fmp_api_key, since_date=window_start, store=store
            )
            # Moves first: a stake filed on Tuesday is news, a March portfolio is not.
            market_stakes, stakes_examined = fetch_market_stakes(
                s.fmp_api_key, window_start, on, with_count=True
            )
            # Registering shares is the step before selling, and it lands weeks before
            # any Form 4 does. Nothing else in the pipeline sees it.
            resale, resale_examined = fetch_resale_registrations(
                s.fmp_api_key, window_start, on, with_count=True
            )
            # The 5 largest trades, pooled across every tracked manager, plus the
            # largest new/amended stakes ranked separately since a 13D/G carries no
            # price. Written to our own appendix page first so the email rows below
            # can deep-link into it -- each filing gets a stable anchor keyed on its
            # accession number, not on this run's ordering.
            top_trades = pool_top_trades(tracked_snapshots)
            top_stakes = pool_top_stakes(tracked_snapshots)
            # The 13F headline: tracked managers whose quarterly information table
            # was FILED (not report-period-dated) inside this week's window. See
            # ingestion/quarterly_filings.py -- this is the fix for the 2026-08-14
            # deadline, where 35 managers' full portfolios landed in storage and the
            # email said nothing about it.
            try:
                from whale_agent.ingestion.quarterly_filings import (
                    recent_quarterly_filings,
                )

                quarterly_filings = recent_quarterly_filings(
                    store, window_start, on, watchlist=WATCHLIST
                )
            except Exception as exc:  # noqa: BLE001 - a query failure must not sink the run
                log.warning("13F headline query failed: %s", exc)
                quarterly_filings = []
            try:
                import functools

                from whale_agent.enrichment.news_context import context_for

                pages = render_tracked_filings_page(
                    top_trades,
                    top_stakes,
                    on,
                    snapshots=tracked_snapshots,
                    sections=sections,
                    since=window_start,
                    market_stakes=market_stakes,
                    resale=resale,
                    store=store,
                    news_fetcher=functools.partial(context_for, settings=s),
                    quarterly=quarterly_filings,
                )
                written_pages = write_tracked_filings_page(pages, out_dir, on)
                # Same rule as the article links: only ever a public URL when one is
                # configured. Without --article-base-url there is no report link at
                # all -- never a file:// path, which is dead the moment it leaves the
                # machine that wrote it. Pages can still be attached with
                # --attach-report (see report_attachment below), off by default.
                #
                # Every slug in PAGE_SLUGS gets an entry, "" for one this run had no
                # data for (its file was never written by write_tracked_filings_page,
                # so linking to it would 404) -- render_weekly_html's `_link_for`
                # already treats a missing/empty entry as "no report link", same as
                # the old single-string "" case.
                tracked_report_links = {
                    slug: public_report_link(base_url, tracked_filings_page_filename(on, slug))
                    for slug in (
                        "index",
                        "trades",
                        "overview",
                        "bigstakes",
                        "resale",
                        "moves",
                        "roster",
                    )
                    if tracked_filings_page_filename(on, slug) in written_pages
                }
                # The opt-in attachment ships the index page plus every section page
                # concatenated after it -- not one well-formed document, but the
                # `--attach-report` path is off by default and exists only as a
                # fallback for a reader whose site is unreachable, so a readable dump
                # of every page's content in one file is an acceptable tradeoff against
                # rebuilding the attachment as an actual multi-file bundle.
                report_attachment_html = "\n\n".join(pages.values())
                trades_link = tracked_report_links.get("trades", "")
                if trades_link:
                    # Every anchor the email's top-trades/top-stakes rows link into
                    # lives on the trades-and-stakes page; the other sections'
                    # truncation notes ("N more ... See the full report") link into
                    # their own pages, each checked below with its own ReportLink so a
                    # broken link on any one page is caught before send, not just the
                    # trades page.
                    fragment_set: set[str] = {
                        filing_anchor(row.accession)
                        for row in (*top_trades, *top_stakes)
                        if getattr(row, "accession", "")
                    }
                    for fc in quarterly_filings or []:
                        if getattr(fc, "accession", ""):
                            fragment_set.add(filing_anchor(fc.accession))
                    report_links.append(
                        ReportLink(
                            url=trades_link,
                            label="tracked-manager report (trades and stakes)",
                            fragments=tuple(fragment_set),
                        )
                    )

                overview_link = tracked_report_links.get("overview", "")
                if overview_link and sections:
                    fragment_set = set()
                    for section in sections:
                        fragment_set.add(section_anchor(section.label))
                        for row in section.rows:
                            if row.filing_url:
                                fragment_set.add(item_anchor(url=row.filing_url))
                    report_links.append(
                        ReportLink(
                            url=overview_link,
                            label="tracked-manager report (fortnight overview)",
                            fragments=tuple(fragment_set),
                        )
                    )

                resale_link = tracked_report_links.get("resale", "")
                if resale_link and resale:
                    fragment_set = {"sec-resale"}
                    for r in resale:
                        anchor = item_anchor(accession=getattr(r, "accession", ""), url=r.url)
                        if anchor:
                            fragment_set.add(anchor)
                    report_links.append(
                        ReportLink(
                            url=resale_link,
                            label="tracked-manager report (arranging to sell)",
                            fragments=tuple(fragment_set),
                        )
                    )

                bigstakes_link = tracked_report_links.get("bigstakes", "")
                if bigstakes_link and market_stakes:
                    # The report page renders _market_stakes_block with no limit (all
                    # stakes get an anchor); only the email caps it to 10/5. This checks
                    # against what the REPORT page renders, so it must stay unlimited.
                    fragment_set = set()
                    for stake in market_stakes:
                        anchor = item_anchor(
                            accession=getattr(stake, "accession", ""), url=stake.url
                        )
                        if anchor:
                            fragment_set.add(anchor)
                    report_links.append(
                        ReportLink(
                            url=bigstakes_link,
                            label="tracked-manager report (big stakes)",
                            fragments=tuple(fragment_set),
                        )
                    )

                moves_link = tracked_report_links.get("moves", "")
                if moves_link and tracked_snapshots:
                    fragment_set = {"sec-moves"}
                    for snap in tracked_snapshots:
                        if snap.error is not None or not snap.recent_filings:
                            continue
                        fragment_set.add(manager_anchor(snap.name))
                        for f in snap.recent_filings:
                            anchor = item_anchor(accession=f.accession, url=f.url)
                            if anchor:
                                fragment_set.add(anchor)
                    report_links.append(
                        ReportLink(
                            url=moves_link,
                            label="tracked-manager report (what every manager filed)",
                            fragments=tuple(fragment_set),
                        )
                    )

                roster_link = tracked_report_links.get("roster", "")
                if roster_link and tracked_snapshots:
                    report_links.append(
                        ReportLink(
                            url=roster_link,
                            label="tracked-manager report (roster)",
                            fragments=("sec-roster",),
                        )
                    )

                index_link = tracked_report_links.get("index", "")
                if index_link:
                    report_links.append(
                        ReportLink(url=index_link, label="tracked-manager report (index)")
                    )
            except Exception as exc:  # noqa: BLE001 - the appendix page is an extra
                log.warning("Tracked-filings page failed; SEC links only: %s", exc)
                tracked_report_links = {}

            def _our_link(accession: str) -> str:
                trades_link = tracked_report_links.get("trades", "")
                return (
                    f"{trades_link}#{filing_anchor(accession)}"
                    if trades_link and accession
                    else ""
                )

            tracked = render_market_stakes(
                market_stakes, on, window_start, examined=stakes_examined
            )
            tracked += render_resale_watch(resale, on, window_start, examined=resale_examined)
            tracked += render_top_trades(top_trades, top_stakes, on, our_link=_our_link)
            tracked += render_fund_moves(tracked_snapshots, on, window_start)
            tracked += render_tracked_funds(tracked_snapshots, on)
            # Declared to the egress guard: a 13F portfolio is legitimately above the
            # per-disclosure ceiling, and blocking on it killed the 2026-08-03 send.
            tracked_figures = aggregate_figure_tokens(tracked_snapshots)
        except Exception as exc:  # noqa: BLE001 - a manager lookup must not cost the send
            log.warning("Tracked-fund section failed; sending without it: %s", exc)

    report = render_weekly(events, on, annotations, articles, notes or None, tracked or None)

    cleared = sum(1 for ev in events if passes_gate(ev, s))
    issue_link = ""
    try:
        issue = build_issue(
            on=on,
            cleared=cleared,
            recorded=len(events),
            sections=sections,
            macro_lines=backdrop_lines(snapshot, on),
            macro_sourcing=macro_sourcing_line(s),
            how_to_read=HOW_TO_READ,
        )
        # One row per filer, their largest filing. The per-filing chart is right for a
        # note about a single issuer, where five filings by one person IS the finding.
        # At issue level it just spends five of ten rows on one name and tells the reader
        # less than ten distinct names would.
        chart_events = [
            ev
            for ev in events
            if ev.effective_usd is not None and ev.transaction_type in BUY_TYPES
        ]
        chart_events.sort(key=lambda e: -(e.effective_usd or 0))
        seen: set[str] = set()
        deduped = []
        for ev in chart_events:
            key = (ev.filer_id or ev.filer_name or "").strip().lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ev)
        chart_events = deduped
        write_issue(issue, out_dir, chart_events[:10])
        # Only ever a public URL, and the canonical extensionless form (see
        # public_report_link). The old fallback was a file:// path, which on a CI
        # runner points into a container that no longer exists by the time anyone reads
        # the email -- a button that looks clickable and resolves nowhere. With no
        # public base the button is simply omitted, and the email says nothing it
        # cannot back up.
        issue_link = public_report_link(base_url, issue_filename(on))
        if issue_link:
            report_links.append(ReportLink(url=issue_link, label="weekly overview"))
        if not issue_link:
            log.warning(
                "No article base URL: the overview page was written but cannot be "
                "linked, so the button is omitted. Deploy deploy/ and set ARTICLE_BASE_URL."
            )
    except Exception as exc:  # noqa: BLE001 - the issue page must not cost the send
        log.warning("Issue page failed; sending the weekly without it: %s", exc)

    # "International plays" is left out of the weekly email by default. This filters
    # only the copy handed to the email renderer --
    # ingestion, storage, and the full report page (built above from the unfiltered
    # `sections`) are untouched. WHALE_INCLUDE_INTERNATIONAL=1 puts it back in the email.
    email_sections = (
        sections
        if s.include_international
        else [sec for sec in sections if sec.key != "international"]
    )

    html_body = ""
    try:
        html_body = render_weekly_html(
            on=on,
            cleared=cleared,
            recorded=len(events),
            articles=articles,
            sections=email_sections,
            issue_link=issue_link,
            how_to_read=HOW_TO_READ,
            no_thesis_line=NO_THESIS_LINE,
            macro_lines=backdrop_lines(snapshot, on),
            macro_sourcing=macro_sourcing_line(s),
            tracked_funds=tracked_snapshots or None,
            quarterly_filings=quarterly_filings or None,
            market_stakes=market_stakes,
            stakes_examined=stakes_examined,
            resale=resale,
            top_trades=top_trades,
            top_stakes=top_stakes,
            tracked_report_link=tracked_report_links,
        )
    except Exception as exc:  # noqa: BLE001 - a layout failure must not cost the send
        log.warning("Weekly HTML layout failed; falling back to plain conversion: %s", exc)

    # The subject line, composed from facts the report re-derives below it. Built here
    # rather than in the renderer because both the email and the log want it.
    ranked = most_bought(events, top=1)
    moved = [s for s in tracked_snapshots if s.recent_filings]
    headline = build_headline(
        HeadlineFacts(
            cluster_issuer=ranked[0][0] if ranked else "",
            cluster_filers=ranked[0][1] if ranked else 0,
            cluster_usd=ranked[0][2] if ranked else 0.0,
            fund_name=moved[0].name if moved else "",
            fund_form=moved[0].recent_filings[0].form if moved else "",
            fund_count=sum(len(s.recent_filings) for s in moved),
            cleared=cleared,
        ),
        on,
    )
    log.info("weekly headline: %s", headline)

    if html_out and html_body:
        Path(html_out).write_text(html_body, encoding="utf-8")
        log.info("wrote HTML body to %s", html_out)

    # Off by default. The reader does not want an HTML attachment on the weekly; the
    # per-item links (now backed by a defaulted, always-live base URL) are the way into
    # the full report. Attachment support stays available, opt-in only, via
    # --attach-report for whoever needs a copy while the site itself is down.
    should_attach = bool(attach_report)
    attachment = (
        (tracked_filings_filename(on), report_attachment_html.encode("utf-8"))
        if should_attach and report_attachment_html
        else None
    )

    results: list[DeliveryResult] = []
    if deliver:
        # Refuse to send an email whose links do not yet work. This is the guard for
        # the recurring failure: the render step writes pages to `articles_dir` but
        # something else has to actually publish them (wrangler deploy, or a CI step
        # that already ran), and if that has not happened yet every "our report" link
        # in the email below is dead. Checking the real URLs (and, where the email
        # points at an anchor, the anchor's presence in the fetched page) is the only
        # way to catch "published but missing the fragment" as well as "not published
        # at all" -- both have happened before. Same fail-closed instinct as
        # `watchdog.gate_report`: a blocked send that explains itself beats a delivered
        # email full of dead links.
        problems = broken_links(report_links, fetch=link_fetcher)
        if problems:
            detail = (
                "WEEKLY SEND BLOCKED: report link(s) are not live yet.\n\n"
                + "\n".join(f"  {p}" for p in problems)
                + "\n\nPublish the pages (e.g. `cd deploy && npx wrangler@latest deploy`, "
                "or re-run with --publish) and re-run the send."
            )
            log.error(detail)
            result = DeliveryResult(channel="link-check", ok=False, detail=detail)
            results.append(result)
            store.record_digest_run(on, "weekly", result.channel, result.ok, result.detail)
            return report, results
        result = send_digest_email(
            report,
            s,
            subject=headline,
            html=html_body or None,
            events=events,
            allow_figures=tracked_figures,
            attachment=attachment,
        )
        results.append(result)
        store.record_digest_run(on, "weekly", result.channel, result.ok, result.detail)
        if result.ok:
            default_review_log().record_sent(
                f"weekly-{on.isoformat()}",
                "weekly",
                jurisdictions=[ev.jurisdiction.value for ev in events],
                event_count=len(events),
                quarantined_count=len(withheld),
            )
    return report, results


def _publish_to_cloudflare(articles_dir: Path) -> None:
    """Deploy `articles_dir` to Cloudflare via the same command CI's own "Deploy the
    pages to Cloudflare" step runs.

    Wrangler deploys whatever `deploy/wrangler.toml` points `[assets].directory` at
    (currently `./public`), not an arbitrary path, so this only does the right thing
    when `articles_dir` is the default `deploy/public` -- i.e. `articles_dir.parent` is
    the `deploy/` project. Local publishing works off the saved wrangler OAuth login on
    this machine; no token is read or required.

    Raises on any failure (wrangler missing, not logged in, deploy rejected) so the
    caller aborts before sending -- a failed deploy must never be followed by an email
    whose links go nowhere.
    """
    deploy_dir = articles_dir.parent
    if not (deploy_dir / "wrangler.toml").exists():
        raise SystemExit(
            f"--publish expects --articles-dir to be Cloudflare's asset directory "
            f"(default deploy/public); {deploy_dir}/wrangler.toml was not found."
        )
    log.info("Publishing %s to Cloudflare (wrangler deploy)...", articles_dir)
    try:
        subprocess.run(
            ["npx", "--yes", "wrangler@latest", "deploy"],
            cwd=deploy_dir,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(
            f"--publish: wrangler deploy failed, refusing to send: {exc}"
        ) from exc


def main() -> None:
    ap = argparse.ArgumentParser(description="whale-agent weekly report")
    ap.add_argument("--dry-run", action="store_true", help="render but do not send email")
    ap.add_argument(
        "--html-out",
        default="",
        help="write the HTML body to this path, for looking at what the reader gets",
    )
    ap.add_argument("--date", default="", help="report end date (YYYY-MM-DD)")
    ap.add_argument(
        "--articles-dir", default="articles", help="where article pages are written"
    )
    ap.add_argument(
        "--article-base-url",
        default="",
        help="public URL the article directory is served from; defaults to "
        "Settings.article_base_url (the live Cloudflare site) when unset",
    )
    ap.add_argument(
        "--attach-report",
        dest="attach_report",
        action="store_true",
        default=None,
        help="attach the full tracked-manager report to the email as an HTML file "
        "(default: off; the per-item links are the way into the report)",
    )
    ap.add_argument(
        "--no-attach-report",
        dest="attach_report",
        action="store_false",
        help="never attach the full tracked-manager report to the email (the default)",
    )
    ap.add_argument(
        "--publish",
        action="store_true",
        help="deploy the rendered pages to Cloudflare (via wrangler) before sending, so "
        "publish-then-send happens as one command instead of an ordering a human "
        "has to remember. Off by default: CI deploys as its own workflow step and "
        "must not deploy twice or fail here for lacking wrangler/a login. Ignored "
        "with --dry-run, which never publishes or sends.",
    )
    ap.add_argument(
        "--profile",
        default="",
        help="named profile (profiles/<name>.toml) or a path to a .toml file",
    )
    ap.add_argument("--show-config", action="store_true", help="print config and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    load_env_file()
    try:
        resolved = resolve_config(args.profile or None)
    except ProfileError as exc:
        ap.error(str(exc))
        return
    settings = resolved.settings
    if args.show_config:
        from whale_agent.jobs.digest_daily import describe_config

        print(describe_config(resolved))
        return
    store = Store(settings.db_path)
    try:
        on = date.fromisoformat(args.date) if args.date else date.today()
        articles_dir = Path(args.articles_dir)
        if args.publish and not args.dry_run:
            # Mirrors the CI workflow's own two steps -- render/publish, then send --
            # inside one local command instead of leaving the ordering to a human. The
            # render pass writes pages into articles_dir and caches any theses the
            # model wrote (see theses_cache_path); the send pass below reuses that
            # cache, so this costs one set of LLM calls, not two, same as CI.
            run_weekly_digest(
                store,
                settings,
                on=on,
                deliver=False,
                articles_dir=articles_dir,
                article_base_url=args.article_base_url,
                attach_report=args.attach_report,
            )
            _publish_to_cloudflare(articles_dir)
        report, results = run_weekly_digest(
            store,
            settings,
            on=on,
            deliver=not args.dry_run,
            articles_dir=articles_dir,
            article_base_url=args.article_base_url,
            html_out=args.html_out,
            attach_report=args.attach_report,
        )
        print(report)
        for result in results:
            status = "sent" if result.ok else "NOT sent"
            print(f"\n[{result.channel}] {status}: {result.detail}")
        # A send that was attempted and refused must fail the job. On 2026-08-03 the
        # egress guard blocked delivery, the step still exited 0, and the run went green
        # with no email and no alert -- the same silent failure as the schedule gate.
        if not args.dry_run and results and not any(r.ok for r in results):
            raise SystemExit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
