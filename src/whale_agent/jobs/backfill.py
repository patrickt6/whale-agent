"""Backfill: pull weeks of historical filings so the agent has something to reason about.

The daily job fetches the most recent page and stops, which is right for a daily digest
and useless for anything else. A pattern needs a window: three filers into one issuer is
not a fact you can observe in a single afternoon, and "unfamiliar filer" means nothing
until the store has seen enough filers to know who is familiar. A first run therefore has
a fully built analysis layer and nothing for it to analyse.

This walks backwards through the vendor's pages until it reaches a cutoff date, and
writes everything into the same store the daily job uses. It is idempotent by
construction -- events key on a stable hash, so re-running a window updates rows rather
than duplicating them, and a backfill that dies halfway can simply be run again.

    python -m whale_agent.jobs.backfill --days 28
    python -m whale_agent.jobs.backfill --days 7 --dry-run

Deliberately does NOT send anything. Backfilling is a data operation; a month of
historical filings is not news and must never reach a reader as though it were.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from whale_agent.config import Settings, get_settings, load_env_file
from whale_agent.enrichment.filer_history import history_is_warm
from whale_agent.enrichment.fx import StaticFxProvider
from whale_agent.enrichment.valuation import value_events
from whale_agent.errors import NotConfiguredError, SourceUnavailableError
from whale_agent.ingestion.vendors.fmp import FmpInsiderAdapter
from whale_agent.models.event import NormalizedEvent
from whale_agent.monitoring.quarantine import STAGE_VALUATION, default_log
from whale_agent.scoring.dedup import dedup_events
from whale_agent.storage.db import Store

log = logging.getLogger(__name__)

# One page is 100 rows and a busy filing day is roughly six pages, so this caps a run at
# a bit over a quarter's worth. It exists to stop a runaway loop, not to shape the query.
DEFAULT_MAX_PAGES = 400

# FMP publishes no rate limit for this endpoint. A short pause between pages keeps a
# 200-request backfill from looking like a scrape, at a cost of under a minute total.
PAGE_DELAY_SECONDS = 0.25

# Vendor pages are not perfectly ordered by date, so one page that predates the cutoff is
# not proof we are done. Stop only after several consecutive pages fall entirely behind.
CONSECUTIVE_OLD_PAGES_TO_STOP = 3


@dataclass
class BackfillResult:
    """What a backfill run actually retrieved."""

    events: list[NormalizedEvent] = field(default_factory=list)
    pages_fetched: int = 0
    oldest_seen: date | None = None
    newest_seen: date | None = None
    stopped_because: str = ""

    def summary(self) -> str:
        span = "no rows"
        if self.oldest_seen and self.newest_seen:
            days = (self.newest_seen - self.oldest_seen).days
            span = f"{self.oldest_seen} .. {self.newest_seen} ({days} days)"
        return (
            f"{len(self.events)} events over {self.pages_fetched} pages, {span} "
            f"[{self.stopped_because}]"
        )


def backfill_fmp_insider(
    settings: Settings,
    since: date,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_delay: float = PAGE_DELAY_SECONDS,
) -> BackfillResult:
    """Page backwards through FMP's insider search until reaching `since`.

    Paging is the only mechanism available: the endpoint accepts `from`/`to` parameters
    but ignores them, returning the most recent page regardless -- verified live, which is
    why this walks pages rather than asking for a window.
    """
    adapter = FmpInsiderAdapter(settings)
    result = BackfillResult()
    consecutive_old = 0

    for page in range(max_pages):
        try:
            rows: list[dict] = []
            for raw in adapter.fetch(page=page, limit=100):
                rows.extend(adapter.parse(raw))
        except (NotConfiguredError, SourceUnavailableError) as exc:
            result.stopped_because = f"source error on page {page}: {exc}"
            return result

        if not rows:
            result.stopped_because = f"vendor returned no rows at page {page}"
            return result

        result.pages_fetched += 1
        events = [adapter.normalize(r) for r in rows]
        result.events.extend(events)

        dates = [e.disclosure_date for e in events]
        page_newest, page_oldest = max(dates), min(dates)
        result.newest_seen = max(filter(None, [result.newest_seen, page_newest]))
        result.oldest_seen = min(filter(None, [result.oldest_seen, page_oldest]))

        if page_newest < since:
            consecutive_old += 1
            if consecutive_old >= CONSECUTIVE_OLD_PAGES_TO_STOP:
                result.stopped_because = f"reached {since}"
                return result
        else:
            consecutive_old = 0

        if page_delay:
            time.sleep(page_delay)

    result.stopped_because = f"hit max_pages={max_pages}"
    return result


def store_backfill(store: Store, result: BackfillResult, since: date) -> int:
    """Value, then persist what the backfill found, trimmed to the requested window.

    Valuation is not optional here, and forgetting it is the bug this comment exists to
    prevent. `build_ranked` values events on the way through, so a backfill that writes
    straight to storage produces rows with a share count, a price, and no dollar figure --
    which then fail the $5M gate on every subsequent read. The symptom is a store holding
    thousands of filings of which almost none clear the threshold.

    No network is needed: FMP's insider rows carry the filing's own stated price, so
    shares x price is arithmetic. Rows older than the cutoff arrive because paging
    overshoots and are dropped, so the window a caller asked for is the window they get.
    """
    fx = StaticFxProvider()
    in_window, quarantined = value_events(
        [e for e in dedup_events(result.events) if e.disclosure_date >= since], fx
    )
    if quarantined:
        # Stored anyway, unvalued, with the refusal on the row. The backfill's whole
        # purpose is to make the store a faithful record of what the vendor sent; a row
        # dropped here would be invisible to the very count that tells us a vendor
        # changed something. It carries no figure, so nothing can render one.
        log.warning(
            "Backfill quarantined %d row(s) at valuation; first: %s",
            len(quarantined),
            quarantined[0][1].reason,
        )
        sink = default_log()
        for event, refusal in quarantined:
            sink.record_event(STAGE_VALUATION, event, [refusal.reason])
    store.upsert_many(in_window)
    store.record_filers(in_window)
    return len(in_window)


def revalue_store(store: Store, since: date) -> tuple[int, int]:
    """Re-value everything already stored, for a store written before valuation ran.

    Returns (rows seen, rows that now carry a USD figure). Purely arithmetic over data
    already held, so it costs nothing and can be run repeatedly.

    This is one of the two legitimate unscreened reads. A row that was withheld for a
    bad figure is exactly a row worth re-valuing, and a screened read would hide the
    rows this function exists to repair. Nothing here renders or sends.
    """
    fx = StaticFxProvider()
    valued, quarantined = value_events(store.events_since_UNSCREENED(since), fx)
    if quarantined:
        log.warning("Re-valuation quarantined %d row(s)", len(quarantined))
        sink = default_log()
        for event, refusal in quarantined:
            sink.record_event(STAGE_VALUATION, event, [refusal.reason])
    store.upsert_many(valued)
    return len(valued), sum(1 for e in valued if e.effective_usd is not None)


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill historical filings into the store")
    ap.add_argument("--days", type=int, default=28, help="how far back to reach")
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    ap.add_argument("--dry-run", action="store_true", help="fetch but do not store")
    ap.add_argument(
        "--revalue",
        action="store_true",
        help="re-value what is already stored; fetches nothing",
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    load_env_file()
    settings = get_settings()
    if not settings.fmp_enabled:
        raise SystemExit("FMP is not configured; nothing to backfill from.")

    since = date.today() - timedelta(days=args.days)

    if args.revalue:
        store = Store(settings.db_path)
        try:
            seen, priced = revalue_store(store, since)
            print(f"re-valued {seen} stored events; {priced} now carry a USD figure")
        finally:
            store.close()
        return

    print(f"Backfilling insider filings since {since} ...")

    result = backfill_fmp_insider(settings, since, args.max_pages)
    print(result.summary())

    if args.dry_run:
        print("(dry run, nothing stored)")
        return

    store = Store(settings.db_path)
    try:
        stored = store_backfill(store, result, since)
        print(f"stored {stored} events within the window")
        print(f"filer observations on record: {store.filer_observation_count()}")
        print(f"history span (days): {store.filer_history_span_days()}")
        print(f"unfamiliarity terms live: {history_is_warm(store)}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
