"""Should the weekly send right now?

The first version of this asked "is the local hour exactly 07:00", and two crons were
registered so that exactly one matched in either half of the DST year. That was wrong,
and 2026-08-03 proved it: GitHub started the scheduled run at 13:43 UTC, which is 09:43
in Toronto. The hour check said no, every later step was skipped, the job went green, and
no email was sent and no alarm raised. The daily ingest the same morning ran three and a
half hours behind its cron.

Scheduled runs on a low-activity private repo are routinely hours late. Any design that
depends on *when* a run starts will keep failing that way, and it fails silently, which
is the worst property a delivery system can have.

So the question is no longer "is it the right time" but "does this week still owe a
report". Lateness stops mattering: whichever run gets there first does the work, and the
rest find it already done. Several crons can then be registered purely as redundancy, and
DST stops being a special case, because "after 07:00 local" is true for all of them.

Usage:
    python -m whale_agent.jobs.schedule_gate     # prints "true" or "false"
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from whale_agent.config import get_settings, load_env_file
from whale_agent.storage.db import Store

log = logging.getLogger(__name__)

TZ_NAME = "America/Toronto"

# Nothing goes out before breakfast. This is the only remaining time condition, and it is
# a floor rather than an equality, so a late run still satisfies it.
MIN_LOCAL_HOUR = 7

# How far back to look for an already-delivered weekly. Six days rather than seven so a
# report that went out slightly late last week cannot suppress this week's.
WEEKLY_COOLDOWN_DAYS = 6


def should_send_weekly(
    store: Store,
    now_utc: datetime,
    *,
    tz_name: str = TZ_NAME,
    min_hour: int = MIN_LOCAL_HOUR,
    cooldown_days: int = WEEKLY_COOLDOWN_DAYS,
) -> tuple[bool, str]:
    """Return (send?, the reason), so a skipped run can say why in its log."""
    if now_utc.tzinfo is None:
        raise ValueError("should_send_weekly requires a timezone-aware datetime")
    local = now_utc.astimezone(ZoneInfo(tz_name))

    if local.hour < min_hour:
        return False, (
            f"local time is {local:%H:%M} {tz_name}, before the {min_hour:02d}:00 floor"
        )

    since = local.date() - timedelta(days=cooldown_days)
    if store.digest_sent_since(since, "weekly"):
        return False, f"a weekly was already delivered on or after {since}"

    return True, f"no weekly delivered since {since}; this run owes one"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_env_file()
    settings = get_settings()
    if not settings.weekly_enabled:
        log.info("weekly gate: skip (WHALE_CADENCE=%s)", settings.cadence)
        print("false")
        return
    store = Store(settings.db_path)
    try:
        send, reason = should_send_weekly(store, datetime.now(UTC))
        log.info("weekly gate: %s (%s)", "SEND" if send else "skip", reason)
        print("true" if send else "false")
    finally:
        store.close()


if __name__ == "__main__":
    main()
