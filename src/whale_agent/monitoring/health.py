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
