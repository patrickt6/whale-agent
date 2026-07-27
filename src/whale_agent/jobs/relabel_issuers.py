"""Backfill English issuer names onto stored rows that were saved with CJK ones.

`enrichment.market_context.enrich_event` swaps a Japanese issuer name for the vendor's
English one, but it runs at ingest. Rows persisted before that rule existed keep the name
they were stored with, and the weekly report reads storage rather than re-enriching, so
the fix is invisible on the existing corpus without a pass like this one.

This is a maintenance job, not part of any schedule. It is idempotent: a row whose name is
already Latin is skipped, so running it twice costs a second FMP lookup per ticker and
changes nothing. Cost is one snapshot per distinct ticker, not per row.

Usage:
    python -m whale_agent.jobs.relabel_issuers --dry-run   # report, write nothing
    python -m whale_agent.jobs.relabel_issuers             # rewrite the rows
    python -m whale_agent.jobs.relabel_issuers --since 2026-01-01
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from whale_agent.config import get_settings, load_env_file
from whale_agent.enrichment.market_context import (
    MarketContextProvider,
    enrich_event,
    is_cjk,
)
from whale_agent.jobs.pipeline import build_market_provider
from whale_agent.storage.db import Store

log = logging.getLogger(__name__)

# Far enough back to cover everything the corpus holds. The job is keyed on the name being
# CJK, not on age, so a wide window costs lookups rather than correctness.
DEFAULT_LOOKBACK_DAYS = 3650


def relabel(
    store: Store,
    provider: MarketContextProvider,
    *,
    since: date,
    dry_run: bool = False,
) -> list[tuple[str, str]]:
    """Rewrite CJK issuer names in place. Returns the (before, after) pairs it changed.

    Reads UNSCREENED deliberately: a row quarantined on its figures still carries a name,
    and leaving it Japanese would mean the name silently reverts if the figure is ever
    fixed. Nothing here is rendered or delivered, which is the condition that read carries.
    """
    changed: list[tuple[str, str]] = []
    for event in store.events_since_UNSCREENED(since):
        if not event.ticker or not is_cjk(event.issuer_name):
            continue
        before = event.issuer_name
        enrich_event(event, provider)
        if event.issuer_name == before:
            continue
        changed.append((before, event.issuer_name))
        if not dry_run:
            store.upsert(event)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m whale_agent.jobs.relabel_issuers",
        description="backfill English issuer names onto stored CJK rows",
    )
    parser.add_argument("--dry-run", action="store_true", help="report but write nothing")
    parser.add_argument("--since", help="earliest filing date to touch (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_env_file()
    settings = get_settings()

    provider = build_market_provider(settings)
    if provider is None:
        # Fail loudly rather than reporting "0 rows changed", which reads as success.
        log.error("No market context provider: FMP is disabled or FMP_API_KEY is unset.")
        return 2

    since = (
        date.fromisoformat(args.since)
        if args.since
        else date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    )

    store = Store(settings.db_path)
    try:
        changed = relabel(store, provider, since=since, dry_run=args.dry_run)
    finally:
        store.close()

    for before, after in changed:
        log.info("  %s  ->  %s", before, after)
    verb = "would rename" if args.dry_run else "renamed"
    log.info("%s %d row(s)", verb, len(changed))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
