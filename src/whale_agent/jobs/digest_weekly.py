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
import logging
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
from whale_agent.jobs.overview import OVERVIEW_DAYS, build_overview
from whale_agent.models.annotation import ContextAnnotation
from whale_agent.models.enums import TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.models.thesis import Thesis
from whale_agent.monitoring.quarantine import coverage_note, default_log
from whale_agent.monitoring.sampled_review import default_review_log
from whale_agent.profiles import ProfileError, resolve_config
from whale_agent.publishing.article_html import article_filename, write_article
from whale_agent.publishing.issue_html import issue_filename, write_issue
from whale_agent.scoring.score import passes_gate
from whale_agent.storage.db import Store
from whale_agent.summarization.issue import build_issue
from whale_agent.summarization.llm import LLMProvider, get_provider
from whale_agent.summarization.macro_context import backdrop_lines, macro_note_for
from whale_agent.summarization.render import format_usd
from whale_agent.summarization.render_weekly_html import render_weekly_html
from whale_agent.summarization.thesis import generate_theses

log = logging.getLogger(__name__)

BUY_TYPES = {
    TransactionType.OPEN_MARKET_BUY,
    TransactionType.ACTIVIST_13D,
    TransactionType.FUND_NEW_POSITION,
    TransactionType.FUND_ADD_POSITION,
}

HOW_TO_READ = """\
HOW TO READ THIS (permanent block: the evidence, not the pitch)

- Insider *buys* are more informative than sells. People sell for a hundred reasons and
  buy for one.
- Only *opportunistic* trades ever carried signal. Cohen, Malloy and Pomorski (2012)
  found roughly 82 bps/month for opportunistic insider trades and essentially nothing
  for routine, calendar-driven ones. This tool computes that classification where a
  filer's history is long enough to support it and shows it on the daily table, marked
  "unknown" where it is not. Note the limitation honestly: the weekly rows below are
  neither filtered nor ordered by it, so routine activity can appear here on size alone.
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
    straight past it and put a $1.1 quadrillion insider row into a client-facing table.

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
        path = write_article(thesis, out_dir, supporting)
        link = (
            f"{base_url.rstrip('/')}/{article_filename(thesis)}" if base_url else path.as_uri()
        )
        published.append((thesis, link))
    return published


def render_weekly(
    events: list[NormalizedEvent],
    on: date,
    annotations: list[ContextAnnotation] | None = None,
    articles: list[tuple[Thesis, str]] | None = None,
    coverage_notes: list[str] | None = None,
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
        lines.append("\nCONTEXT (lobbying and government contracts: slow signals, not events)")
        for ann in annotations[:15]:
            amount = format_usd(ann.amount_usd)
            subject = ann.issuer_name or ann.ticker or "unknown"
            lines.append(f"  {subject}: {ann.label}, {amount} ({ann.as_of.isoformat()})")

    lines.append(
        "\nEvery figure is copied from a public filing or computed from filing fields. "
        "Awareness tool, not investment advice."
    )
    return "\n".join(lines)


def run_weekly_digest(
    store: Store,
    settings: Settings | None = None,
    *,
    on: date | None = None,
    deliver: bool = True,
    annotations: list[ContextAnnotation] | None = None,
    articles_dir: Path | None = None,
    article_base_url: str = "",
) -> tuple[str, list[DeliveryResult]]:
    s = settings or get_settings()
    on = on or date.today()
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
    try:
        theses = attach_macro_notes(
            weekly_theses(events, on, get_provider(s)), events, snapshot, on
        )
        if theses:
            articles = publish_articles(
                theses, events, articles_dir or Path("articles"), article_base_url
            )
    except Exception as exc:  # noqa: BLE001 - delivery must not depend on publishing
        log.warning("Thesis layer failed; sending the weekly without articles: %s", exc)

    note = coverage_note(withheld)
    report = render_weekly(events, on, annotations, articles, [note] if note else None)

    # The overview reaches back a fortnight while the notes cover a week: one category can
    # be two rows over seven days, and two rows is not an overview.
    overview_events = fill_issuer_names(
        screen_for_report(store.events_since(_window(on, OVERVIEW_DAYS))), _company_names(s)
    )
    sections = build_overview(overview_events, on)
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
        write_issue(issue, articles_dir or Path("articles"), chart_events[:10])
        base = (article_base_url or "").rstrip("/")
        issue_link = (
            f"{base}/{issue_filename(on)}"
            if base
            else ((articles_dir or Path("articles")) / issue_filename(on)).resolve().as_uri()
        )
    except Exception as exc:  # noqa: BLE001 - the issue page must not cost the send
        log.warning("Issue page failed; sending the weekly without it: %s", exc)

    html_body = ""
    try:
        html_body = render_weekly_html(
            on=on,
            cleared=cleared,
            recorded=len(events),
            articles=articles,
            sections=sections,
            issue_link=issue_link,
            how_to_read=HOW_TO_READ,
            no_thesis_line=NO_THESIS_LINE,
            macro_lines=backdrop_lines(snapshot, on),
            macro_sourcing=macro_sourcing_line(s),
        )
    except Exception as exc:  # noqa: BLE001 - a layout failure must not cost the send
        log.warning("Weekly HTML layout failed; falling back to plain conversion: %s", exc)

    results: list[DeliveryResult] = []
    if deliver:
        result = send_digest_email(
            report,
            s,
            subject=f"Whale weekly, {on.isoformat()}",
            html=html_body or None,
            events=events,
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


def main() -> None:
    ap = argparse.ArgumentParser(description="whale-agent weekly report")
    ap.add_argument("--dry-run", action="store_true", help="render but do not send email")
    ap.add_argument("--date", default="", help="report end date (YYYY-MM-DD)")
    ap.add_argument(
        "--articles-dir", default="articles", help="where article pages are written"
    )
    ap.add_argument(
        "--article-base-url",
        default="",
        help="public URL the article directory is served from; file:// links without it",
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
        report, results = run_weekly_digest(
            store,
            settings,
            on=on,
            deliver=not args.dry_run,
            articles_dir=Path(args.articles_dir),
            article_base_url=args.article_base_url,
        )
        print(report)
        for result in results:
            status = "sent" if result.ok else "NOT sent"
            print(f"\n[{result.channel}] {status}: {result.detail}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
