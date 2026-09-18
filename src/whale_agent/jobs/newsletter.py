"""`whale newsletter send`: the free public brief, mailed to confirmed subscribers.

    whale newsletter send [--dry-run] [--cadence daily|weekly] [--date YYYY-MM-DD]

Steps, in order:
  1. Fetch confirmed subscribers from the site Worker (GET /api/subscribers, Bearer token).
  2. Render the brief ONCE, from free sources only. Paid adapters are switched off and
     their keys cleared, and the LLM is `none` unless a free Gemini key is present.
  3. Run the egress screen on the shared render. A block stops the whole run.
  4. For each recipient: add the personal unsubscribe link, the List-Unsubscribe headers
     and the legal footer, run the egress screen again on that exact message, then send.
     Sends go in batches, with a pause between messages and retry with backoff.
  5. A sent log keyed by (cadence, period, email hash) stops a rerun from sending twice.

Environment:
  WHALE_NEWSLETTER_API          site origin, e.g. https://whale-agent.com
  WHALE_NEWSLETTER_ADMIN_TOKEN  the Worker's ADMIN_TOKEN
  WHALE_NEWSLETTER_FOOTER       legal footer with a postal address (required for a real send)
  WHALE_NEWSLETTER_FROM         e.g. "Whale Agent <brief@your-domain>". Must be on your own
                                domain; a free-mail sender (gmail.com and so on) is refused.
  WHALE_NEWSLETTER_REPLY_TO     optional Reply-To address
  WHALE_NEWSLETTER_HASH_KEY     optional secret for the sent-log HMAC (falls back to the
                                admin token)
  WHALE_NEWSLETTER_PROVIDER     resend (default)
  RESEND_API_KEY                provider key
  WHALE_NEWSLETTER_SENT_LOG     path of the JSONL sent log (default newsletter_sent.jsonl)
  WHALE_NEWSLETTER_BATCH_SIZE   messages per batch (default 50)
  WHALE_NEWSLETTER_PAUSE_S      pause between messages in seconds (default 0.2)
  WHALE_NEWSLETTER_BATCH_PAUSE_S pause between batches in seconds (default 2)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html as htmllib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from whale_agent.config import Settings, get_settings, load_env_file
from whale_agent.delivery.base import digest_subject
from whale_agent.delivery.email import find_egress_violations
from whale_agent.delivery.newsletter_sender import (
    NewsletterSender,
    OutgoingEmail,
    ResendNewsletterSender,
    SendError,
)
from whale_agent.models.event import NormalizedEvent

log = logging.getLogger(__name__)

# Source-spec names from ingestion/registry.py that need a paid key or a paid plan.
PAID_SOURCE_PREFIXES = ("fmp", "quiver", "arkham", "whale_alert", "finnhub", "unusual_whales")
FREE_LLM_PROVIDERS = ("none", "gemini")
DEFAULT_FOOTER_NOTE = (
    "Not investment advice. Public filings only. Figures can be late or wrong."
)
MAX_ATTEMPTS = 4


# -- settings -------------------------------------------------------------------------


def is_paid_source(name: str) -> bool:
    n = (name or "").lower()
    return any(n.startswith(p) or p in n for p in PAID_SOURCE_PREFIXES)


def free_settings(s: Settings) -> Settings:
    """A copy of `s` that cannot reach a paid feed or a paid model.

    Flags alone are not enough: `build_price_provider` builds an FMP price provider from
    the key, whatever `enable_fmp` says. So the keys are cleared too."""
    llm = s.llm_provider if s.llm_provider in FREE_LLM_PROVIDERS else "none"
    if llm == "gemini" and not s.gemini_api_key:
        llm = "none"
    return replace(
        s,
        sources=[x for x in s.sources if not is_paid_source(x)],
        enable_fmp=False,
        enable_quiver=False,
        enable_arkham=False,
        enable_whale_alert=False,
        enable_finnhub=False,
        enable_unusual_whales=False,
        fmp_api_key="",
        quiver_api_key="",
        arkham_api_key="",
        whale_alert_api_key="",
        finnhub_api_key="",
        unusual_whales_api_key="",
        anthropic_api_key="",
        openai_api_key="",
        llm_provider=llm,
    )


# -- subscribers ----------------------------------------------------------------------


@dataclass(frozen=True)
class Subscriber:
    email: str
    token: str


def fetch_subscribers(
    api_base: str, admin_token: str, client: httpx.Client | None = None
) -> list[Subscriber]:
    if not api_base or not admin_token:
        raise ValueError(
            "WHALE_NEWSLETTER_API and WHALE_NEWSLETTER_ADMIN_TOKEN must both be set"
        )
    c = client or httpx.Client(timeout=30.0)
    url = f"{api_base.rstrip('/')}/api/subscribers"
    resp = c.get(url, headers={"Authorization": f"Bearer {admin_token}"})
    if resp.status_code != 200:
        raise RuntimeError(f"subscriber fetch failed: HTTP {resp.status_code}")
    data = resp.json()
    out: list[Subscriber] = []
    seen: set[str] = set()
    for row in data.get("subscribers", []):
        email = str(row.get("email", "")).strip().lower()
        token = str(row.get("token", "")).strip()
        if not email or not token or email in seen:
            continue
        seen.add(email)
        out.append(Subscriber(email, token))
    return out


# -- render ---------------------------------------------------------------------------


@dataclass
class Brief:
    subject: str
    text: str
    html: str
    events: list[NormalizedEvent] = field(default_factory=list)


def render_free_brief(settings: Settings, cadence: str, on: date, limit: int = 100) -> Brief:
    """Collect from free sources into a throwaway in-memory store and render once.

    A throwaway store, not the shared corpus: the corpus also holds rows from paid
    feeds, and the public copy must not show them."""
    from whale_agent.enrichment.fx import StaticFxProvider
    from whale_agent.ingestion.registry import build_sources
    from whale_agent.jobs.pipeline import (
        build_digest_html,
        build_market_provider,
        build_price_provider,
        build_ranked,
        collect_events,
        render_checked,
    )
    from whale_agent.storage.db import Store
    from whale_agent.summarization.llm import generate_digest_prose

    s = free_settings(settings)
    store = Store(":memory:")
    try:
        specs = [
            sp for sp in build_sources(s, limit=limit, on=on) if not is_paid_source(sp.name)
        ]
        collected = collect_events(specs, store)
        events = [ev for ev in collected.events if not is_paid_source(ev.source)]
        notes = collected.coverage_notes
        ranked = build_ranked(
            events,
            store,
            s,
            StaticFxProvider(),
            on,
            build_price_provider(s),
            build_market_provider(s),
            notes,
        )
        prose = None
        if s.llm_enabled:
            generated = generate_digest_prose(ranked, s, coverage_notes=notes)
            prose = None if generated.is_empty() else generated
        text = render_checked(ranked, on, prose, notes)
        markup = build_digest_html(ranked, on, prose, notes)
    finally:
        store.close()
    if cadence == "weekly":
        subject = f"Whale Agent weekly brief, week of {period_key('weekly', on)}"
    else:
        subject = digest_subject(text, on.isoformat())
    return Brief(subject, text, markup, ranked)


# -- personalise ----------------------------------------------------------------------


def unsubscribe_url(api_base: str, token: str) -> str:
    return f"{api_base.rstrip('/')}/api/unsubscribe?token={quote(token, safe='')}"


def list_unsubscribe_headers(url: str) -> dict[str, str]:
    headers = {"List-Unsubscribe": f"<{url}>"}
    # RFC 8058: one-click needs an HTTPS URI that accepts POST. The Worker does.
    if urlparse(url).scheme == "https":
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return headers


def personalise(brief: Brief, sub: Subscriber, api_base: str, footer: str) -> OutgoingEmail:
    url = unsubscribe_url(api_base, sub.token)
    note = DEFAULT_FOOTER_NOTE
    text = (
        f"{brief.text.rstrip()}\n\n--\n{note}\n{footer}\n"
        f"You get this email because you confirmed a subscription on the Whale Agent site.\n"
        f"Unsubscribe: {url}\n"
    )
    block = (
        '<div style="color:#6b7480;font-size:12px;margin:22px auto;max-width:680px;'
        "padding:14px 16px;border-top:1px solid #e3e6ea;"
        'font-family:Helvetica,Arial,sans-serif">'
        f"<p>{htmllib.escape(note)}</p><p>{htmllib.escape(footer)}</p>"
        "<p>You get this email because you confirmed a subscription on the Whale Agent site. "
        f'<a href="{htmllib.escape(url)}">Unsubscribe</a></p></div>'
    )
    lower = brief.html.lower()
    idx = lower.rfind("</body>")
    markup = brief.html[:idx] + block + brief.html[idx:] if idx >= 0 else brief.html + block
    return OutgoingEmail(sub.email, brief.subject, text, markup, list_unsubscribe_headers(url))


# -- idempotency ----------------------------------------------------------------------


def period_key(cadence: str, on: date) -> str:
    if cadence == "weekly":
        return (on - timedelta(days=on.weekday())).isoformat()
    return on.isoformat()


def email_hash(email: str, key: str = "") -> str:
    """HMAC-SHA256 of the normalised address when `key` is set, else plain SHA-256.

    A plain hash of an email address is easy to reverse with a list of likely addresses,
    and the sent log lives in the GitHub Actions cache. So real sends use a keyed hash."""
    data = email.strip().lower().encode("utf-8")
    if key:
        return hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


class SentLog:
    """Append-only JSONL. Stores a keyed hash of the address, not the address."""

    def __init__(self, path: Path, key: str = "") -> None:
        self.path = path
        self.key = key
        self._done: set[tuple[str, str, str]] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                digest = row.get("email_hash") or row.get("email_sha256")
                if row.get("status") == "sent" and digest:
                    self._done.add((row["cadence"], row["period"], digest))

    def already_sent(self, cadence: str, period: str, email: str) -> bool:
        return (cadence, period, email_hash(email, self.key)) in self._done

    def record(
        self, cadence: str, period: str, email: str, status: str, detail: str = ""
    ) -> None:
        row = {
            "cadence": cadence,
            "period": period,
            "email_hash": email_hash(email, self.key),
            "hash": "hmac-sha256" if self.key else "sha256",
            "status": status,
            "detail": detail,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        if status == "sent":
            self._done.add((cadence, period, row["email_hash"]))


# -- send loop ------------------------------------------------------------------------


@dataclass
class SendReport:
    subscribers: int = 0
    sent: int = 0
    skipped_already_sent: int = 0
    failed: int = 0
    blocked: int = 0
    stopped_early: bool = False


def send_with_retry(
    sender: NewsletterSender,
    msg: OutgoingEmail,
    sleep: Callable[[float], None],
    base_delay: float = 1.0,
) -> str:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return sender.send(msg)
        except SendError as exc:
            if not exc.retryable or attempt == MAX_ATTEMPTS:
                raise
            sleep(base_delay * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")  # pragma: no cover


def deliver(
    brief: Brief,
    subscribers: Iterable[Subscriber],
    sender: NewsletterSender,
    sent_log: SentLog,
    *,
    cadence: str,
    on: date,
    api_base: str,
    footer: str,
    batch_size: int = 50,
    pause_s: float = 0.2,
    batch_pause_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    max_consecutive_failures: int = 5,
) -> SendReport:
    subs = list(subscribers)
    report = SendReport(subscribers=len(subs))
    period = period_key(cadence, on)
    pending = [s for s in subs if not sent_log.already_sent(cadence, period, s.email)]
    report.skipped_already_sent = len(subs) - len(pending)
    consecutive = 0
    for i, sub in enumerate(pending):
        if i and batch_size > 0 and i % batch_size == 0:
            sleep(batch_pause_s)
        msg = personalise(brief, sub, api_base, footer)
        violations = find_egress_violations(
            msg.subject, msg.text, msg.html, events=brief.events
        )
        if violations:
            # Same render for everyone, so a block here is a block for the whole run.
            log.error("EGRESS BLOCKED on a personalised message: %s", "; ".join(violations))
            report.blocked += 1
            report.stopped_early = True
            break
        try:
            send_with_retry(sender, msg, sleep)
        except SendError as exc:
            report.failed += 1
            consecutive += 1
            sent_log.record(cadence, period, sub.email, "failed", str(exc))
            if consecutive >= max_consecutive_failures:
                # A provider outage or a daily quota. Stop; the sent log lets a rerun resume.
                log.error("Stopping after %d consecutive failures", consecutive)
                report.stopped_early = True
                break
            continue
        consecutive = 0
        report.sent += 1
        sent_log.record(cadence, period, sub.email, "sent")
        if pause_s:
            sleep(pause_s)
    return report


FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "yandex.com",
        "mail.com",
    }
)


def sender_domain_problem(sender: str) -> str:
    """Empty when `sender` is an address on the operator's own domain, else the reason.

    The newsletter must not go out from a personal mailbox: that would show the
    operator's own address to every subscriber, and free-mail DMARC policies make
    such mail fail anyway when sent through a provider."""
    addr = sender.strip()
    if "<" in addr and addr.endswith(">"):
        addr = addr[addr.rindex("<") + 1 : -1]
    if "@" not in addr:
        return "WHALE_NEWSLETTER_FROM is not an email address"
    domain = addr.rsplit("@", 1)[1].strip().lower()
    if domain in FREE_MAIL_DOMAINS:
        return (
            f"WHALE_NEWSLETTER_FROM uses the free-mail domain {domain}; "
            "use an address on your own domain"
        )
    return ""


def build_sender(env: dict[str, str]) -> NewsletterSender:
    provider = (env.get("WHALE_NEWSLETTER_PROVIDER") or "resend").lower()
    sender = env.get("WHALE_NEWSLETTER_FROM", "")
    if sender:
        problem = sender_domain_problem(sender)
        if problem:
            raise ValueError(problem)
    reply_to = env.get("WHALE_NEWSLETTER_REPLY_TO", "").strip()
    if reply_to and "@" not in reply_to:
        raise ValueError("WHALE_NEWSLETTER_REPLY_TO is not an email address")
    if provider == "resend":
        return ResendNewsletterSender(env.get("RESEND_API_KEY", ""), sender, reply_to=reply_to)
    raise ValueError(f"unknown WHALE_NEWSLETTER_PROVIDER {provider!r}")


# -- entry point ----------------------------------------------------------------------


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    settings: Settings | None = None,
    render: Callable[[Settings, str, date], Brief] | None = None,
    client: httpx.Client | None = None,
    sender: NewsletterSender | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    ap = argparse.ArgumentParser(prog="whale newsletter")
    sub = ap.add_subparsers(dest="action", required=True)
    sp = sub.add_parser("send", help="send the free brief to confirmed subscribers")
    sp.add_argument("--dry-run", action="store_true", help="render and count, send nothing")
    sp.add_argument("--cadence", choices=("daily", "weekly"), default="weekly")
    sp.add_argument("--date", default="", help="brief date (YYYY-MM-DD), defaults to today")
    ns = ap.parse_args(args)

    e = dict(os.environ if env is None else env)
    s = free_settings(settings or get_settings())
    on = date.fromisoformat(ns.date) if ns.date else date.today()
    api_base = e.get("WHALE_NEWSLETTER_API", "").strip()
    token = e.get("WHALE_NEWSLETTER_ADMIN_TOKEN", "").strip()
    footer = e.get("WHALE_NEWSLETTER_FOOTER", "").strip()

    if not ns.dry_run:
        problems = []
        if not api_base.startswith("https://"):
            problems.append("WHALE_NEWSLETTER_API must be an https:// URL")
        if not token:
            problems.append("WHALE_NEWSLETTER_ADMIN_TOKEN is not set")
        if not footer:
            problems.append(
                "WHALE_NEWSLETTER_FOOTER is not set (it must carry a postal address)"
            )
        if problems:
            for p in problems:
                out(f"NOT sent: {p}")
            return 2

    if api_base and token:
        subscribers = fetch_subscribers(api_base, token, client)
    else:
        subscribers = []
        out("No subscriber API configured; counting 0 subscribers.")

    brief = (render or render_free_brief)(s, ns.cadence, on)
    violations = find_egress_violations(
        brief.subject, brief.text, brief.html, events=brief.events
    )
    if violations:
        out("NOT sent: egress screen blocked the brief: " + "; ".join(violations))
        return 3

    period = period_key(ns.cadence, on)
    log_path = Path(e.get("WHALE_NEWSLETTER_SENT_LOG") or "newsletter_sent.jsonl")
    hash_key = e.get("WHALE_NEWSLETTER_HASH_KEY", "").strip() or token
    sent_log = SentLog(log_path, key=hash_key)
    already = sum(1 for x in subscribers if sent_log.already_sent(ns.cadence, period, x.email))

    out(f"Subject: {brief.subject}")
    out(f"Cadence: {ns.cadence}, period {period}, LLM: {s.llm_provider}")
    out(
        f"Confirmed subscribers: {len(subscribers)}; already sent this period: {already}; "
        f"to send: {len(subscribers) - already}"
    )
    if ns.dry_run:
        out("Dry run: nothing sent.")
        return 0

    try:
        snd = sender or build_sender(e)
    except ValueError as exc:
        out(f"NOT sent: {exc}")
        return 2
    report = deliver(
        brief,
        subscribers,
        snd,
        sent_log,
        cadence=ns.cadence,
        on=on,
        api_base=api_base,
        footer=footer,
        batch_size=int(e.get("WHALE_NEWSLETTER_BATCH_SIZE") or 50),
        pause_s=float(e.get("WHALE_NEWSLETTER_PAUSE_S") or 0.2),
        batch_pause_s=float(e.get("WHALE_NEWSLETTER_BATCH_PAUSE_S") or 2.0),
        sleep=sleep,
    )
    out(
        f"Sent: {report.sent}; failed: {report.failed}; skipped (already sent): "
        f"{report.skipped_already_sent}; blocked: {report.blocked}"
    )
    if report.blocked:
        return 3
    return 1 if (report.failed or report.stopped_early) else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_env_file()
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
