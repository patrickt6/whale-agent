"""Watchdog job: is the machine still working?

Run this on its own schedule, separate from the digest. That separation is the point --
a digest job that crashed cannot report its own absence, so something else has to notice
the missing delivery row.

Usage:
    python -m whale_agent.jobs.watchdog            # check and alert
    python -m whale_agent.jobs.watchdog --check    # report only, never send
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

from whale_agent.config import get_settings, load_env_file
from whale_agent.monitoring.alerting import alert_operator
from whale_agent.monitoring.health import digest_overdue, stale_sources
from whale_agent.monitoring.quarantine import QuarantineLog, alerting_reasons, default_log
from whale_agent.monitoring.sampled_review import (
    ReviewLog,
    default_review_log,
    review_backlog_reasons,
)
from whale_agent.storage.db import Store

log = logging.getLogger(__name__)

# How far back the watchdog looks at the quarantine record. Matched to a daily run: a
# longer window would keep re-reporting yesterday's spike, and an operator who has
# already seen an alert twice stops reading the third one.
QUARANTINE_WINDOW_HOURS = 24.0


def build_report(
    store: Store,
    on: date | None = None,
    *,
    quarantine_log: QuarantineLog | None = None,
    review_log: ReviewLog | None = None,
) -> tuple[str, bool]:
    """Return (human-readable report, whether anything is wrong).

    The quarantine and review sections are why this job is worth more than a dead-man's
    switch. A stale source is a loud failure that would surface eventually. A vendor that
    quietly started serving transaction totals in its price field shows up nowhere except
    as a rising count of blocked figures, and only if something looks at that count.
    """
    settings = get_settings()
    on = on or date.today()
    problems: list[str] = []
    notes: list[str] = []

    stale = stale_sources(store)
    for source in stale:
        problems.append(f"STALE SOURCE  {source.describe()}")
        if source.last_error:
            problems.append(f"              last error: {source.last_error}")

    overdue = digest_overdue(store, on=on, settings=settings)
    if overdue:
        problems.append(
            f"NO DIGEST     nothing delivered for {on.isoformat()} by "
            f"{settings.digest_deadline_hour_utc:02d}:00 UTC"
        )

    quarantine = quarantine_log or default_log()
    summary = quarantine.summary(hours=QUARANTINE_WINDOW_HOURS)
    problems.extend(alerting_reasons(summary))
    if summary.total:
        # Below the alert threshold this is context, not a problem. A handful of blocked
        # figures a day is the guardrail working, and reporting it as a fault would be
        # the fastest way to teach an operator to ignore this job.
        notes.append(f"QUARANTINE    {summary.describe()}")

    reviews = review_log or default_review_log()
    pending = reviews.pending_reviews()
    problems.extend(review_backlog_reasons(pending))
    if pending:
        notes.append(f"REVIEW QUEUE  {len(pending)} sampled digest(s) awaiting review")

    tail = ("\n" + "\n".join(notes)) if notes else ""
    if not problems:
        return f"whale-agent healthy ({on.isoformat()}){tail}", False
    return "whale-agent problems:\n" + "\n".join(problems) + tail, True


def main() -> None:
    ap = argparse.ArgumentParser(description="whale-agent watchdog")
    ap.add_argument("--check", action="store_true", help="report only, do not alert")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    load_env_file()
    settings = get_settings()
    store = Store(settings.db_path)
    try:
        report, unhealthy = build_report(store)
        print(report)
        if unhealthy and not args.check:
            alert_operator("whale-agent: attention needed", report, settings)
    finally:
        store.close()


if __name__ == "__main__":
    main()
