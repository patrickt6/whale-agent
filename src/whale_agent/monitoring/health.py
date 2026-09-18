"""Freshness watchdog and dead-man's switch.

The failure this guards against is the quiet one: a scraper breaks, the digest still
sends, and it just stops mentioning Japan. Nothing errors, so nothing alerts, and the
gap is only noticed weeks later. Recording every adapter attempt and comparing against
an expected cadence turns that silence into a signal.

The dead-man's switch is deliberately a *separate* check from the digest job. A digest
job that crashes cannot report its own absence, so something else has to notice the
missing delivery row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from whale_agent.config import Settings, get_settings
from whale_agent.storage.db import Store

# How long a source may go without a successful run before it is considered stale.
# Generous by design: these are daily-to-quarterly sources, and a false alarm every
# weekend would train an operator to ignore the alerts.
EXPECTED_MAX_AGE_HOURS: dict[str, int] = {
    "sec_form4": 36,
    "sec_13dg": 36,
    "fmp_insider": 36,
    "fmp_congress": 24 * 7,  # congressional disclosure runs on a 45-day lag anyway
    "quiver_congress": 24 * 7,
    "taiwan_mops": 24 * 3,  # monthly balance file, but the endpoint should answer daily
    "japan_edinet": 36,
    "arkham": 24,
    "whale_alert": 24,
    "dataroma": 24 * 3,
}
DEFAULT_MAX_AGE_HOURS = 48

# The sources whose silence invalidates a weekly rather than merely thinning it.
# These three produce the great majority of threshold-clearing US events; a report
# missing them is not a degraded report, it is a different report that does not say so.
# Everything else degrades coverage honestly, through a note in the email.
CORE_SOURCES: frozenset[str] = frozenset({"sec_form4", "sec_13dg", "fmp_insider"})

# Freshness is not completeness, and the difference is not academic. On 2026-08-02 the
# corpus had a seven-day hole: ingest had stopped on 07-27, and a single daily run the
# next morning would have marked every source fresh again while the reporting window
# still held 9 events. The freshness gate would have passed and the weekly would have
# sent, reading as a very quiet week rather than as a broken pipeline.
#
# 100 is chosen against the real distribution rather than guessed: full weeks in the
# corpus held 1844, 3232 and 4466 events, and the broken window held 9. Anything in
# between is a judgement call that should be made by looking, not by sending.
MIN_WINDOW_EVENTS = 100

# What each source should produce in a normal week. Deliberately low: this catches a
# source at zero, not a source having a slow week. The corpus-wide floor cannot see
# these, because one source supplying 95% of the rows hides every other source's silence
# behind a healthy total -- which is exactly how the 13D/G outage went unnoticed.
EXPECTED_WEEKLY_EVENTS: dict[str, int] = {
    "fmp_insider": 100,
    "sec_edgar_13dg": 5,
    "sec_form4": 5,
    "fmp_congress": 5,
}


@dataclass(frozen=True)
class StaleSource:
    name: str
    last_success_at: str | None
    age_hours: float | None
    last_error: str | None

    def describe(self) -> str:
        if self.last_success_at is None:
            return f"{self.name}: no successful run on record"
        return f"{self.name}: last success {self.age_hours:.0f}h ago"


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stale_sources(
    store: Store,
    *,
    now: datetime | None = None,
    only: set[str] | None = None,
) -> list[StaleSource]:
    """Return every source whose last success is older than its expected cadence.

    `only` restricts the check to sources that were actually supposed to run -- a
    source switched off on purpose is not stale, it is off.
    """
    now = now or datetime.now(UTC)
    out: list[StaleSource] = []
    for name, row in store.source_runs().items():
        if only is not None and name not in only:
            continue
        max_age = EXPECTED_MAX_AGE_HOURS.get(name, DEFAULT_MAX_AGE_HOURS)
        last = _parse_stamp(row.get("last_success_at"))
        if last is None:
            out.append(StaleSource(name, None, None, row.get("last_error")))
            continue
        age_hours = (now - last).total_seconds() / 3600.0
        if age_hours > max_age:
            out.append(
                StaleSource(name, row.get("last_success_at"), age_hours, row.get("last_error"))
            )
    return out


def core_sources_stale(store: Store, *, now: datetime | None = None) -> list[StaleSource]:
    """Stale sources among CORE_SOURCES only -- the set that blocks a send."""
    return stale_sources(store, now=now, only=set(CORE_SOURCES))


def non_core_stale(store: Store, *, now: datetime | None = None) -> list[StaleSource]:
    """Stale sources outside the core set: degraded coverage, not a blocked send."""
    return [s for s in stale_sources(store, now=now) if s.name not in CORE_SOURCES]


def coverage_notes_for_stale_sources(
    store: Store, *, now: datetime | None = None
) -> list[str]:
    """One reader-facing line per non-core source that has gone quiet.

    Deliberately excludes core sources: if one of those is stale the weekly does not
    send at all, so a note about it would never be read.
    """
    return [
        f"{s.name} is not reporting ({s.describe()}); coverage from that source is "
        f"incomplete for this window."
        for s in non_core_stale(store, now=now)
    ]


def thin_window(
    store: Store,
    *,
    on: date | None = None,
    days: int = 7,
    minimum: int = MIN_WINDOW_EVENTS,
) -> int | None:
    """Return the window's event count if it is implausibly thin, else None.

    Answers "is there enough here to report on", which freshness cannot answer. A source
    that succeeded this morning says nothing about whether the six days before it were
    collected.
    """
    on = on or date.today()
    count = len(store.events_since(on - timedelta(days=days)))
    return count if count < minimum else None


def silent_sources(store: Store, *, on: date | None = None, days: int = 7) -> list[str]:
    """Sources below their expected weekly volume in the window.

    Reported, never blocking. A quiet source is a coverage note; only a stale core
    source stops the send.
    """
    on = on or date.today()
    counts: dict[str, int] = {}
    for event in store.events_since(on - timedelta(days=days)):
        counts[event.source] = counts.get(event.source, 0) + 1
    return sorted(
        name for name, floor in EXPECTED_WEEKLY_EVENTS.items() if counts.get(name, 0) < floor
    )


def digest_overdue(
    store: Store,
    *,
    on: date | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
    kind: str = "daily",
) -> bool:
    """True when today's digest should have shipped by now and no delivery is recorded."""
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    on = on or now.date()
    deadline = datetime(on.year, on.month, on.day, s.digest_deadline_hour_utc, tzinfo=UTC)
    if now < deadline:
        return False
    return not store.digest_sent(on, kind)


def next_deadline(settings: Settings | None = None, now: datetime | None = None) -> datetime:
    """The next digest deadline in UTC, for logging and for the scheduled check job."""
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    today = datetime(now.year, now.month, now.day, s.digest_deadline_hour_utc, tzinfo=UTC)
    return today if now < today else today + timedelta(days=1)
