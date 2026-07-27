"""The single email entry point the jobs call, and the egress gate that guards it.

Chooses SMTP or Resend from `settings.email_provider`, converts the digest to HTML, and
never raises: an unconfigured or failing mailbox returns a `DeliveryResult(ok=False)`
so the caller can still print the digest and log the reason. Losing the email is
recoverable; losing the digest is not.

This module is also the last line. Every other numeric guard in the pipeline is opt-in
per call site: a new report, a refactor, or an ad-hoc script can reach delivery without
ever calling `screen()`, and that is precisely how a $1.1 quadrillion figure once reached
a client-facing table. There is exactly one `send_digest_email()`, so a check placed
*inside* it cannot be routed around.

The gate fails closed. A body carrying a figure larger than any real disclosure is not
sent, the operator finds out from an ERROR log, and the caller still holds the digest
text. Swallowing the check and sending anyway would be worse than having no gate.
"""

from __future__ import annotations

import logging
import re

from whale_agent.config import Settings, get_settings
from whale_agent.delivery.base import DeliveryResult, digest_subject, digest_to_html
from whale_agent.delivery.email_resend import send_via_resend
from whale_agent.delivery.email_smtp import send_via_smtp
from whale_agent.enrichment.plausibility import ABSOLUTE_CEILING_USD, check_event
from whale_agent.errors import NotConfiguredError
from whale_agent.models.event import NormalizedEvent
from whale_agent.monitoring.quarantine import STAGE_EGRESS, record_quarantine

log = logging.getLogger(__name__)

# The same ceiling `plausibility.py` enforces upstream, restated here on purpose. The
# egress gate is a second, independent reading of the *rendered* body: it re-derives the
# magnitudes from the text a human will actually see, rather than trusting that some
# earlier stage looked at the objects behind it.
EGRESS_CEILING_USD = ABSOLUTE_CEILING_USD

# A rendered money token: "$12.4M", "$250B", "$1600000.0B" (which is how
# `format_usd` spells $1.6 quadrillion, since it has no larger suffix). The trailing
# `(?![A-Za-z])` is the same token-boundary fix the provenance gate needed: without it a
# scale suffix can be claimed from the first letter of the next word.
_MONEY_TOKEN = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([KMBT])?(?![A-Za-z])", re.IGNORECASE)
_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

# Identifiers inside a link are not magnitude claims. Stripped for the same reason
# `provenance.py` strips them: an SEC accession number reads as money.
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def figures_in(text: str) -> list[tuple[str, float]]:
    """Every money token in a rendered body, paired with the USD value it reads as."""
    found: list[tuple[str, float]] = []
    for raw, suffix in _MONEY_TOKEN.findall(_URL.sub(" ", text or "")):
        try:
            magnitude = float(raw.replace(",", "")) * _SCALE[suffix.upper()]
        except (ValueError, KeyError):  # pragma: no cover - regex admits neither
            continue
        found.append((f"${raw}{suffix}", magnitude))
    return found


def find_egress_violations(
    *bodies: str | None,
    events: list[NormalizedEvent] | None = None,
) -> list[str]:
    """Reasons this message must not be sent. Empty list means it may go.

    Two independent readings, because they fail differently. The text scan catches a
    figure however it got into the body, including one an upstream stage never saw as a
    number. The event scan catches a row whose figures are contradictory in a way the
    rendered string alone cannot show, such as a position worth more than its issuer.
    """
    violations: list[str] = []

    for body in bodies:
        for token, magnitude in figures_in(body or ""):
            if magnitude > EGRESS_CEILING_USD:
                violations.append(
                    f"rendered figure {token} reads as {magnitude:,.0f} USD, above the "
                    f"{EGRESS_CEILING_USD:,.0f} ceiling for any real disclosure"
                )

    for event in events or []:
        verdict = check_event(event)
        if not verdict.plausible:
            violations.append(
                f"{event.filer_name} / {event.issuer_name}: {'; '.join(verdict.reasons)}"
            )

    return violations


def send_digest_email(
    digest: str,
    settings: Settings | None = None,
    subject: str | None = None,
    html: str | None = None,
    events: list[NormalizedEvent] | None = None,
) -> DeliveryResult:
    """Deliver a rendered digest by email. Safe to call unconditionally.

    `digest` is always the plain-text alternative. Pass `html` to send the richer
    event-driven layout from `render_html.py`; callers that only hold text (the weekly
    report, operator alerts) omit it and get the generic text-to-HTML conversion.

    Pass `events` to have the rows behind the render re-screened here as well. It is
    optional because operator alerts have no events, and the text scan alone is already
    a complete guarantee for the figures a reader can see.
    """
    s = settings or get_settings()
    if not s.email_enabled:
        reason = (
            f"email disabled (provider={s.email_provider}, "
            f"to={'set' if s.email_to else 'unset'})"
        )
        log.info("Skipping email: %s", reason)
        return DeliveryResult(s.email_provider or "email", False, reason)

    subject = subject or digest_subject(digest)
    html_body = html or digest_to_html(digest, subject)

    # Fail closed, and before either provider is touched.
    violations = find_egress_violations(subject, digest, html_body, events=events)
    if violations:
        log.error("EGRESS BLOCKED: %s", "; ".join(violations))
        for reason in violations:
            # STAGE_EGRESS is load-bearing: a single record at this stage escalates,
            # because a figure reaching the last line means every upstream check missed
            # it. That is a different failure from one bad vendor row being caught early.
            record_quarantine(STAGE_EGRESS, reason, source=s.email_provider or "email")
        return DeliveryResult(
            s.email_provider or "email",
            False,
            "egress blocked: " + "; ".join(violations),
        )

    try:
        if s.email_provider == "resend":
            return send_via_resend(subject, digest, html_body, s)
        return send_via_smtp(subject, digest, html_body, s)
    except NotConfiguredError as exc:
        log.warning("Email not configured: %s", exc)
        return DeliveryResult(s.email_provider, False, str(exc))
