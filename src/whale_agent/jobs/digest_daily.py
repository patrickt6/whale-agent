"""Daily digest CLI entry point.

Runs every configured source, ranks what clears the $5M gate, optionally adds LLM prose,
emails the result, and prints it. Sources that are not configured are skipped and named
in the digest's coverage note, so a run with nothing but the free feeds is a first-class
outcome rather than a degraded one.

Usage:
    python -m whale_agent.jobs.digest_daily                  # all configured sources
    python -m whale_agent.jobs.digest_daily --demo           # offline fixture demo
    python -m whale_agent.jobs.digest_daily --dry-run        # render, don't send
    python -m whale_agent.jobs.digest_daily --no-llm         # deterministic prose only
    python -m whale_agent.jobs.digest_daily --sources sec_form4,taiwan_mops
    python -m whale_agent.jobs.digest_daily --show-config    # what is on and what is off
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from whale_agent.config import Settings, load_env_file
from whale_agent.enrichment.fx import StaticFxProvider
from whale_agent.ingestion.registry import build_sources
from whale_agent.ingestion.us_sec import UsSecForm4Adapter
from whale_agent.jobs.pipeline import run_daily_digest
from whale_agent.models.event import NormalizedEvent
from whale_agent.profiles import ProfileError, ResolvedConfig, resolve_config
from whale_agent.storage.db import Store


def _demo_events() -> list[NormalizedEvent]:
    """Offline fixture events, so `--demo` works with no network and no keys."""
    # Package data, not tests/fixtures: an installed wheel has no tests directory.
    from importlib.resources import files

    adapter = UsSecForm4Adapter()
    raw = (
        files("whale_agent.demo_data")
        .joinpath("form4_purchase.xml")
        .read_text(encoding="utf-8")
    )
    return [adapter.normalize(r) for r in adapter.parse(raw)]


def email_problems(settings: Settings) -> list[str]:
    """Name the exact env var stopping email from working, one line per problem.

    "email is off" is not a useful diagnosis when four variables have to line up. This
    also catches the specific mistake of pasting the App Password into the wrong slot:
    a 16-character `WHALE_EMAIL_FROM` with no `@` in it is almost certainly a password,
    and silently using it as a From address would produce a confusing SMTP rejection.
    """
    problems: list[str] = []
    if settings.email_provider == "none":
        return ["WHALE_EMAIL_PROVIDER=none — email is switched off on purpose"]
    if not settings.email_to:
        problems.append("WHALE_EMAIL_TO is empty — nowhere to send it")

    if settings.email_provider == "smtp":
        if not settings.smtp_username:
            problems.append("SMTP_USERNAME is empty — set it to your full Gmail address")
        if not settings.smtp_password:
            problems.append(
                "SMTP_PASSWORD is empty — needs a 16-character Google App Password "
                "(https://myaccount.google.com/apppasswords)"
            )
        sender = settings.email_from
        if sender and "@" not in sender:
            problems.append(
                f"WHALE_EMAIL_FROM is {len(sender)} characters with no '@' — that looks "
                "like an App Password in the wrong variable. It belongs in "
                "SMTP_PASSWORD; leave WHALE_EMAIL_FROM blank to default to SMTP_USERNAME"
            )
    elif settings.email_provider == "resend":
        if not settings.resend_api_key:
            problems.append("RESEND_API_KEY is empty")
        if not settings.email_from:
            problems.append("WHALE_EMAIL_FROM is empty — Resend needs a verified sender")
    else:
        problems.append(
            f"WHALE_EMAIL_PROVIDER={settings.email_provider!r} is not recognised "
            "(expected 'smtp', 'resend', or 'none')"
        )
    return problems


def describe_config(resolved: ResolvedConfig) -> str:
    """A plain-language report of what is switched on, and *why each value is what it
    is*. Answers both 'why is X missing?' and 'why is X $1M instead of $5M?'.

    The provenance block is the point of this function: a stranger who copied a profile
    file and set one environment variable should not have to read this project's source
    to find out which one won.
    """
    settings = resolved.settings
    lines = ["whale-agent configuration", ""]

    if resolved.profile is not None:
        label = resolved.profile.name or Path(resolved.profile.origin).stem
        lines.append(f"Profile: {label}  ({resolved.profile.origin})")
        if resolved.profile.description:
            lines.append(f"  {resolved.profile.description}")
    else:
        lines.append("Profile: none (dataclass defaults + environment only)")
    lines.append("")

    lines.append("Effective values and which layer set them:")
    for row in resolved.fields:
        value = ", ".join(row.value) if isinstance(row.value, list) else row.value
        shown = value if value not in ("", []) else "(none)"
        lines.append(f"  {row.field:<22} {shown!s:<30} <- {row.layer}")
    lines.append("")

    lines.append("Sources:")
    for spec in build_sources(settings):
        if spec.kind != "event":
            continue
        status = "ON " if spec.enabled else "off"
        detail = "" if spec.enabled else f"  ({spec.disabled_reason})"
        lines.append(f"  [{status}] {spec.label}{detail}")
    annotation_specs = [s for s in build_sources(settings) if s.kind == "annotation"]
    if annotation_specs:
        lines.append("")
        lines.append("Context sources (weekly-report annotations, never the daily gate):")
        for spec in annotation_specs:
            status = "ON " if spec.enabled else "off"
            detail = "" if spec.enabled else f"  ({spec.disabled_reason})"
            lines.append(f"  [{status}] {spec.label}{detail}")

    lines.append("")
    lines.append("Prose:")
    lines.append(
        f"  [{'ON ' if settings.llm_enabled else 'off'}] LLM provider={settings.llm_provider}"
        + ("" if settings.llm_enabled else "  (API key not set)")
    )
    lines.append("Delivery:")
    lines.append(
        f"  [{'ON ' if settings.email_enabled else 'off'}] email via {settings.email_provider}"
        + (f" -> {settings.email_to}" if settings.email_to else "")
    )
    for problem in email_problems(settings):
        lines.append(f"        {problem}")
    lines.append(
        f"  [{'ON ' if settings.telegram_enabled else 'off'}] telegram"
        f"    [{'ON ' if settings.twilio_enabled else 'off'}] sms"
    )
    lines.append("")
    lines.append(f"Threshold: ${settings.threshold_usd:,.0f}   DB: {settings.db_path}")
    lines.append(
        f"Cadence: {settings.cadence}   Watchlist: "
        + (", ".join(settings.watch_filers + settings.watch_tickers) or "none (all filers)")
    )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="whale-agent daily digest")
    ap.add_argument("--limit", type=int, default=50, help="max filings per SEC/vendor pull")
    ap.add_argument("--demo", action="store_true", help="offline fixture demo, no network")
    ap.add_argument("--dry-run", action="store_true", help="render but do not send email")
    ap.add_argument(
        "--test-email",
        action="store_true",
        help="send the digest even in --demo mode, to verify mail delivery only",
    )
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM prose layer")
    ap.add_argument(
        "--sources", default="", help="comma-separated source names (see --show-config)"
    )
    ap.add_argument(
        "--profile",
        default="",
        help="named profile (profiles/<name>.toml) or a path to a .toml file; "
        "see profiles/default.toml for the full list of tunables",
    )
    ap.add_argument(
        "--threshold-usd",
        type=float,
        default=None,
        help="override the gate for this run only; wins over both profile and environment",
    )
    ap.add_argument("--show-config", action="store_true", help="print config and exit")
    ap.add_argument("--date", default="", help="digest date (YYYY-MM-DD), defaults to today")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    load_env_file()
    cli_overrides = {}
    if args.sources:
        cli_overrides["sources"] = [p.strip() for p in args.sources.split(",") if p.strip()]
    if args.threshold_usd is not None:
        cli_overrides["threshold_usd"] = args.threshold_usd
    try:
        resolved = resolve_config(args.profile or None, cli_overrides)
    except ProfileError as exc:
        ap.error(str(exc))
        return
    settings = resolved.settings

    if args.show_config:
        print(describe_config(resolved))
        return

    on = date.fromisoformat(args.date) if args.date else date.today()
    # The demo must never touch the real database: it would pollute first-time-filer
    # history with fixture data.
    store = Store(":memory:" if args.demo else settings.db_path)
    try:
        digest, results = run_daily_digest(
            store,
            settings,
            on=on,
            limit=args.limit,
            events=_demo_events() if args.demo else None,
            fx=StaticFxProvider(),
            # --demo is documented as making no network calls at all, so it forces the
            # prose layer off rather than relying on there being no key configured. A
            # developer with a working key in their environment must still get the
            # offline demo they were promised, not a live API call.
            use_llm=not args.no_llm and not args.demo,
            # --demo normally never sends. --test-email is the deliberate exception:
            # it exercises the real mail path against fixture data, so mail can be
            # verified without a live SEC pull.
            deliver=not args.dry_run
            and (not args.demo or args.test_email)
            and (settings.daily_enabled or args.test_email),
        )
        print(digest)
        for result in results:
            status = "sent" if result.ok else "NOT sent"
            print(f"\n[{result.channel}] {status}: {result.detail}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
