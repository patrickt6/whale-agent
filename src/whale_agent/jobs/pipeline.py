"""The pipeline: sources -> events -> digest markdown -> delivery.

Stages:
    collect -> dedup -> value -> enrich (first-time) -> persist -> score/gate -> rank
    -> optional LLM prose -> render -> provenance gate -> deliver

Two invariants hold no matter what is configured:

1. **A failing source never fails the run.** `collect_events` catches per-source
   exceptions, records them, and turns each into a coverage note printed on the digest.
   A missing API key is not an error condition; it is a source that is off.
2. **A failing LLM or mailbox never loses the digest.** Prose is additive and the render
   falls back to the deterministic template; delivery outcomes are returned, not raised,
   and the caller still holds the digest text either way.

`build_ranked` and `run_digest` stay pure (events in, markdown out) so they remain
testable with no network at all; `run_daily_digest` is the orchestration on top.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from whale_agent.config import Settings, get_settings
from whale_agent.delivery.base import DeliveryResult, digest_to_html
from whale_agent.delivery.email import send_digest_email
from whale_agent.enrichment.filer_history import (
    attach_filer_history,
    history_is_warm,
    mark_first_time,
)
from whale_agent.enrichment.fx import FxProvider, StaticFxProvider
from whale_agent.enrichment.history_context import HistoryContext, build_history_contexts
from whale_agent.enrichment.market_context import (
    FmpMarketContextProvider,
    MarketContextProvider,
    enrich_events,
)
from whale_agent.enrichment.plausibility import screen
from whale_agent.enrichment.price import FmpPriceProvider, PriceProvider
from whale_agent.enrichment.routine_classifier import mark_routine
from whale_agent.enrichment.valuation import value_events
from whale_agent.errors import (
    ErrorCategory,
    NotConfiguredError,
    SourceFailure,
    WhaleAgentError,
)
from whale_agent.ingestion.registry import SourceSpec, build_sources
from whale_agent.models.event import NormalizedEvent
from whale_agent.monitoring.quarantine import (
    STAGE_VALUATION,
    coverage_note,
    default_log,
)
from whale_agent.monitoring.sampled_review import default_review_log
from whale_agent.redact import redact
from whale_agent.scoring.dedup import dedup_events
from whale_agent.scoring.filters import apply_filters
from whale_agent.scoring.score import rank_events
from whale_agent.storage.db import Store
from whale_agent.summarization.llm import generate_digest_prose
from whale_agent.summarization.prose import DigestProse
from whale_agent.summarization.provenance import (
    assert_no_hallucinated_numbers,
    check_unsourced_numbers,
)
from whale_agent.summarization.render import render_digest
from whale_agent.summarization.render_html import html_to_text, render_digest_html
from whale_agent.summarization.verifier import run_advisory_pass

log = logging.getLogger(__name__)


@dataclass
class CollectionResult:
    """What every source produced, and what to tell the reader about the ones that didn't."""

    events: list[NormalizedEvent] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    # The structured form of `failures`. A source that raised produces an entry here; a
    # source that succeeded with zero rows produces nothing, which is what keeps "the
    # feed broke" distinguishable from "nothing happened today" without parsing prose.
    source_failures: list[SourceFailure] = field(default_factory=list)


def collect_events(
    specs: list[SourceSpec],
    store: Store | None = None,
    settings: Settings | None = None,
) -> CollectionResult:
    """Run every enabled source, tolerating individual failures.

    Applies the active profile's filter stage (`scoring/filters.py`) to the combined
    result before returning it. This is the one place that runs: the daily digest, the
    instant alerts, and the weekly report (which reads back from storage) all end up
    seeing the same narrowed slice, because it is what gets persisted.
    """
    s = settings or get_settings()
    result = CollectionResult()
    for spec in specs:
        if spec.kind != "event":
            continue  # annotation-only sources are not part of the event pipeline
        if not spec.enabled:
            result.coverage_notes.append(f"{spec.label} not included ({spec.disabled_reason})")
            continue
        try:
            events = list(spec.run())
        except NotConfiguredError as exc:
            # Redacted before it reaches a log line: a misconfigured source can carry
            # a key in its exception text.
            msg = redact(exc)
            log.info("Source %s skipped: %s", spec.name, msg)
            failure = SourceFailure.from_exception(spec.label, exc)
            result.source_failures.append(failure)
            result.coverage_notes.append(failure.describe())
            if store:
                store.record_source_run(spec.name, ok=False, error=msg)
            continue
        except Exception as exc:  # a broken scraper must not take the digest with it
            msg = redact(exc)
            log.warning("Source %s failed: %s", spec.name, msg)
            result.failures[spec.name] = msg
            # Anything that is not already a WhaleAgentError came out of a vendor library
            # or the network stack, so it is classified as upstream and retryable rather
            # than guessed at more precisely than the evidence supports.
            failure = (
                SourceFailure.from_exception(spec.label, exc)
                if isinstance(exc, WhaleAgentError)
                else SourceFailure(
                    source=spec.label,
                    category=ErrorCategory.UPSTREAM,
                    retryable=True,
                    detail=msg,
                )
            )
            result.source_failures.append(failure)
            result.coverage_notes.append(failure.describe())
            if store:
                store.record_source_run(spec.name, ok=False, error=msg)
            continue
        result.events.extend(events)
        if store:
            store.record_source_run(spec.name, ok=True, event_count=len(events))
    result.events = apply_filters(result.events, s)
    return result


def build_price_provider(settings: Settings | None = None) -> PriceProvider | None:
    """An FMP price provider when FMP is configured, otherwise None.

    Without it, share-count and percent-of-company filings simply stay unvalued and read
    "not disclosed". They are still ingested and stored, so switching FMP on later values
    subsequent filings with no change to anything else.
    """
    s = settings or get_settings()
    return FmpPriceProvider(s) if s.fmp_enabled else None


def build_market_provider(settings: Settings | None = None) -> MarketContextProvider | None:
    """An FMP market-context provider when FMP is configured, otherwise None.

    Without it, events carry no company size or float and the scoring terms that read
    those stay neutral -- the ranking still works, it just cannot express "small company"
    as a reason to care.
    """
    s = settings or get_settings()
    return FmpMarketContextProvider(s) if s.fmp_enabled else None


def build_ranked(
    events: list[NormalizedEvent],
    store: Store,
    settings: Settings | None = None,
    fx: FxProvider | None = None,
    on: date | None = None,
    price: PriceProvider | None = None,
    market: MarketContextProvider | None = None,
    coverage_notes: list[str] | None = None,
    stats: dict | None = None,
) -> list[NormalizedEvent]:
    """Dedup, value, enrich, persist, score, gate, and rank. Everything but rendering.

    `stats`, when given, is filled in with counts the caller cannot recover from the
    returned list: how many rows were withheld, which the sampled-review record needs so
    a reviewer can tell a quiet day from a day the guards were busy.

    `coverage_notes`, when given, is appended to in place: a row withheld for an
    implausible figure is reported to the reader the same way an unconfigured source is,
    because the product rule is that a failing source becomes a coverage note and never
    an exception, and a silent drop reads as a quiet week.

    Enrichment order matters and is not arbitrary:

    1. **Market context before scoring**, because company size and percent-of-float are
       scoring inputs. An event enriched afterwards would be ranked as though we had
       never looked it up.
    2. **Filer history before persisting.** Recording this run's events first would put
       each event into its own history, and a debut would stop looking like one.
    3. **Persist before scoring**, so a crash in scoring still leaves the raw record.
    """
    settings = settings or get_settings()
    on = on or date.today()

    sink = default_log()

    deduped = [e for e in dedup_events(events) if settings.on_watchlist(e)]
    valued, quarantined = value_events(deduped, fx, price)
    for event, refusal in quarantined:
        # The durable record, not just a log line. A near-miss you cannot count is a
        # near-miss you cannot notice becoming a trend.
        sink.record_event(STAGE_VALUATION, event, [refusal.reason])
    # Drop the offending row and carry the rest, which is the same shape as a failing
    # source: the reader loses one filing and is told they did, rather than losing the
    # digest or being shown a figure nobody believes.
    withheld = {id(event) for event, _ in quarantined}
    valued = [e for e in valued if id(e) not in withheld]

    if market is not None:
        enrich_events(valued, market)

    # Screen after enrichment, because the strongest check -- a position worth more than
    # the whole company -- needs the market cap enrichment just attached.
    valued, rejected = screen(valued, sink=sink)
    if rejected:
        log.warning(
            "Dropped %d event(s) with implausible figures; first: %s",
            len(rejected),
            f"{rejected[0][0].filer_name} / {rejected[0][0].issuer_name}: "
            f"{'; '.join(rejected[0][1].reasons)}",
        )

    withheld = len(quarantined) + len(rejected)
    if stats is not None:
        stats["quarantined"] = withheld
    note = coverage_note(withheld)
    if note and coverage_notes is not None:
        coverage_notes.append(note)

    # Read history before writing this run into it, then let the store decide whether it
    # holds enough for absence to mean anything (the cold-start guard).
    attach_filer_history(store, valued, on)
    # Same history, second question: does this filer trade the same month every year?
    # The flag has been read in seven places and set by nobody.
    valued = mark_routine(store, valued, on)
    seen = store.seen_filer_ids()
    warm = history_is_warm(store)
    enriched = [mark_first_time(e, seen, history_is_warm=warm) for e in valued]

    store.upsert_many(enriched)
    store.record_filers(enriched)

    return rank_events(enriched, settings, on)


def run_digest(
    events: list[NormalizedEvent],
    store: Store,
    settings: Settings | None = None,
    fx: FxProvider | None = None,
    on: date | None = None,
    validate_provenance: bool = True,
    price: PriceProvider | None = None,
    prose: DigestProse | None = None,
    coverage_notes: list[str] | None = None,
    market: MarketContextProvider | None = None,
) -> str:
    """Run the full pipeline and return the rendered digest markdown."""
    settings = settings or get_settings()
    on = on or date.today()
    notes = list(coverage_notes or [])
    ranked = build_ranked(events, store, settings, fx, on, price, market, notes)
    return render_checked(ranked, on, prose, notes or None, validate_provenance)


def render_checked(
    ranked: list[NormalizedEvent],
    on: date,
    prose: DigestProse | None = None,
    coverage_notes: list[str] | None = None,
    validate_provenance: bool = True,
    history: Mapping[str, HistoryContext] | None = None,
) -> str:
    """Render, then enforce the numeric-provenance gate.

    If prose slipped a figure past the per-sentence filter in `llm.py`, all prose is
    dropped and the digest is re-rendered deterministically. Losing the language is
    always preferable to shipping a number that traces to nothing.
    """
    digest = render_digest(ranked, on, prose, coverage_notes, history)
    if not validate_provenance:
        return digest
    deterministic_ok = True
    if prose is not None and check_unsourced_numbers(digest, ranked, history):
        log.warning("Prose failed the provenance gate; falling back to template render")
        digest = render_digest(ranked, on, None, coverage_notes, history)
        deterministic_ok = False
    assert_no_hallucinated_numbers(digest, ranked, history)
    # Layer 2, and deliberately after the hard gate: an advisory second opinion whose
    # findings go to the quarantine record for an operator to read later. It cannot block
    # a send, and with no LLM provider configured it is a no-op, so the digest above is
    # already final by the time this runs.
    run_advisory_pass(digest, deterministic_ok=deterministic_ok)
    return digest


def run_daily_digest(
    store: Store,
    settings: Settings | None = None,
    *,
    on: date | None = None,
    limit: int = 50,
    events: list[NormalizedEvent] | None = None,
    specs: list[SourceSpec] | None = None,
    fx: FxProvider | None = None,
    price: PriceProvider | None = None,
    use_llm: bool = True,
    deliver: bool = True,
) -> tuple[str, list[DeliveryResult]]:
    """Collect, render, and deliver today's digest. Returns (markdown, delivery results).

    Pass `events` to skip collection entirely (the demo and test paths); pass `specs` to
    control exactly which sources run.
    """
    s = settings or get_settings()
    on = on or date.today()
    fx = fx or StaticFxProvider()
    price = price if price is not None else build_price_provider(s)
    market = build_market_provider(s)

    coverage_notes: list[str] = []
    if events is None:
        specs = (
            specs if specs is not None else build_sources(s, limit=limit, on=on, store=store)
        )
        collected = collect_events(specs, store)
        events = collected.events
        coverage_notes = collected.coverage_notes

    stats: dict = {}
    ranked = build_ranked(events, store, s, fx, on, price, market, coverage_notes, stats)

    prose: DigestProse | None = None
    if use_llm and s.llm_enabled:
        generated = generate_digest_prose(ranked, s, coverage_notes=coverage_notes)
        prose = generated if not generated.is_empty() else None

    history = build_history_contexts(store, ranked, on) if s.history_context else None
    digest = render_checked(ranked, on, prose, coverage_notes, history=history)

    results: list[DeliveryResult] = []
    if deliver:
        result = send_digest_email(
            digest,
            s,
            html=build_digest_html(ranked, on, prose, coverage_notes, history),
            events=ranked,
        )
        results.append(result)
        store.record_digest_run(on, "daily", result.channel, result.ok, result.detail)
        if result.ok:
            # Only what actually reached a reader is eligible for review. A blocked or
            # skipped send has nothing to sample, and counting it would dilute the rate.
            default_review_log().record_sent(
                f"daily-{on.isoformat()}",
                "daily",
                jurisdictions=[ev.jurisdiction.value for ev in ranked],
                event_count=len(ranked),
                quarantined_count=stats.get("quarantined", 0),
            )

    return digest, results


def build_digest_html(
    ranked: list[NormalizedEvent],
    on: date,
    prose: DigestProse | None = None,
    coverage_notes: list[str] | None = None,
    history: Mapping[str, HistoryContext] | None = None,
) -> str:
    """Render the HTML email body, gated the same way the text digest is.

    If the HTML somehow carries a figure the events cannot account for, the prose is
    dropped and it is rebuilt; if it still fails, the caller falls back to converting the
    already-validated text digest. The email is the artifact that actually reaches a
    human, so the guarantee has to hold on the markup, not only on the terminal output.
    """
    from whale_agent.delivery.theme import theme_from_env

    theme = theme_from_env()
    markup = render_digest_html(
        ranked, on, prose, coverage_notes, theme=theme, history=history
    )
    if check_unsourced_numbers(html_to_text(markup), ranked, history):
        log.warning("HTML digest failed provenance with prose; rebuilding without it")
        markup = render_digest_html(
            ranked, on, None, coverage_notes, theme=theme, history=history
        )
    if check_unsourced_numbers(html_to_text(markup), ranked, history):
        log.error("HTML digest failed provenance without prose; falling back to text render")
        return digest_to_html(render_digest(ranked, on, None, coverage_notes, history))
    return markup


def default_since(days: int = 1, on: date | None = None) -> date:
    return (on or date.today()) - timedelta(days=days)
