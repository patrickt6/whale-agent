"""Tier-1 instant alerts: the few events worth interrupting someone for.

The gate is deliberately narrow. An alert that fires often is an alert that gets muted,
at which point the channel is worth nothing on the day it matters. Exactly three
conditions qualify:

  * a position above `WHALE_INSTANT_USD` (default $100M);
  * a first-time activist 13D by a filer in the notability registry;
  * a cluster buy -- three or more insiders buying the same issuer in the window.

Alerts are deduplicated against storage, so re-running ingestion (a backfill, a retry, a
cron overlap) cannot page the operator twice for the same filing.

Usage:
    python -m whale_agent.jobs.instant_alerts               # scan today's stored events
    python -m whale_agent.jobs.instant_alerts --demo        # offline fixture demo
    python -m whale_agent.jobs.instant_alerts --dry-run     # show what would page, don't send
    python -m whale_agent.jobs.instant_alerts --date 2026-07-24
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from whale_agent.config import Settings, get_settings, load_env_file
from whale_agent.delivery.base import DeliveryResult
from whale_agent.delivery.sms_twilio import send_sms
from whale_agent.delivery.telegram import send_telegram
from whale_agent.enrichment.entity_resolution import notability_for
from whale_agent.ingestion.us_sec import UsSecForm4Adapter
from whale_agent.models.enums import TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.profiles import ProfileError, resolve_config
from whale_agent.storage.db import Store
from whale_agent.summarization.render import format_usd

log = logging.getLogger(__name__)

# A filer must be at least this notable for a first-time 13D to page anyone. 1.0 is the
# default for an unrecognized name, so this means "in the registry at all".
NOTABLE_THRESHOLD = 1.2
CLUSTER_MIN = 3


def is_instant_worthy(event: NormalizedEvent, settings: Settings | None = None) -> bool:
    """The Tier-1 gate. Pure and boundary-tested; no side effects."""
    s = settings or get_settings()
    value = event.effective_usd or 0.0

    if value >= s.instant_usd:
        return True

    if (
        event.transaction_type == TransactionType.ACTIVIST_13D
        and event.is_first_time_filer
        and notability_for(event) >= NOTABLE_THRESHOLD
    ):
        return True

    return (
        event.transaction_type == TransactionType.OPEN_MARKET_BUY
        and event.cluster_size >= CLUSTER_MIN
    )


def alert_text(event: NormalizedEvent, settings: Settings | None = None) -> str:
    """Terse alert body.

    Built from `format_usd` -- the same formatter the digest uses -- so an alert and the
    digest row it corresponds to can never disagree about a number.
    """
    s = settings or get_settings()
    action = {
        TransactionType.OPEN_MARKET_BUY: "buy",
        TransactionType.ACTIVIST_13D: "activist stake",
        TransactionType.PASSIVE_13G: "stake",
        TransactionType.FUND_NEW_POSITION: "new position",
        TransactionType.CRYPTO_TRANSFER: "transfer",
    }.get(event.transaction_type, event.transaction_type.value.replace("_", " "))

    estimate = " (est.)" if event.usd_value_is_estimate else ""
    text = (
        f"WHALE: {event.filer_name} disclosed {format_usd(event.effective_usd)}{estimate} "
        f"{action} in {event.issuer_name} ({event.jurisdiction.value})."
    )
    if event.cluster_size >= CLUSTER_MIN:
        text += f" Cluster of {event.cluster_size} insiders."
    if s.digest_url:
        text += f" {s.digest_url}"
    return text


def send_instant_alerts(
    events: list[NormalizedEvent],
    store: Store,
    settings: Settings | None = None,
    on: date | None = None,
) -> list[DeliveryResult]:
    """Fire SMS + Telegram for every qualifying, not-yet-alerted event."""
    s = settings or get_settings()
    on = on or date.today()
    results: list[DeliveryResult] = []

    for event in events:
        if not is_instant_worthy(event, s):
            continue
        # One alert per event, ever: the delivery log is keyed by the event id.
        if store.digest_sent(on, kind=f"instant:{event.event_id}"):
            continue

        text = alert_text(event, s)
        sent_any = False
        for result in (send_telegram(text, s), send_sms(text, s)):
            results.append(result)
            sent_any = sent_any or result.ok
            store.record_digest_run(
                on, f"instant:{event.event_id}", result.channel, result.ok, result.detail
            )
        if not sent_any:
            log.warning("Instant alert had no working channel: %s", text)

    return results


def _demo_events() -> list[NormalizedEvent]:
    """Offline fixture events, so `--demo` works with no network and no keys.

    Same fixture `digest_daily --demo` uses, for the same reason: a $12.4M purchase
    clears the $5M gate but not the $100M instant threshold on its own, so this alone
    will not page anyone -- it exists to prove the command runs end to end, not to
    demonstrate a page.
    """
    fixtures = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
    adapter = UsSecForm4Adapter()
    raw = (fixtures / "form4_purchase.xml").read_text()
    return [adapter.normalize(r) for r in adapter.parse(raw)]


def main() -> None:
    ap = argparse.ArgumentParser(description="whale-agent instant alerts")
    ap.add_argument("--demo", action="store_true", help="offline fixture demo, no network")
    ap.add_argument(
        "--dry-run", action="store_true", help="show what would page, but send nothing"
    )
    ap.add_argument(
        "--date", default="", help="scan events disclosed on/after this date (YYYY-MM-DD)"
    )
    ap.add_argument(
        "--profile",
        default="",
        help="named profile (profiles/<name>.toml) or a path to a .toml file",
    )
    ap.add_argument("--show-config", action="store_true", help="print config and exit")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

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
    on = date.fromisoformat(args.date) if args.date else date.today()
    # The demo must never touch the real database, same reasoning as digest_daily: it
    # would pollute the dedup log with fixture event ids.
    store = Store(":memory:" if args.demo else settings.db_path)
    try:
        events = _demo_events() if args.demo else store.events_since(on)
        if args.dry_run:
            worthy = [ev for ev in events if is_instant_worthy(ev, settings)]
            print(f"{len(worthy)} instant-worthy event(s) as of {on.isoformat()} (dry run):")
            for ev in worthy:
                print(f"  {alert_text(ev, settings)}")
            return
        results = send_instant_alerts(events, store, settings, on=on)
        for result in results:
            status = "sent" if result.ok else "NOT sent"
            print(f"[{result.channel}] {status}: {result.detail}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
