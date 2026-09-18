"""Operator alerts: tell the operator when the machine itself is broken.

Reuses the delivery modules rather than adding a third notification path. Operator
alerts go to `OPERATOR_ALERT_EMAIL` when set (falling back to the digest recipient) and
to Telegram when configured, because the failure mode this guards against is "the email
pipeline is the thing that broke".

Never raises. An alert that fails to send must not take down the run it was reporting on.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from whale_agent.config import Settings, get_settings
from whale_agent.delivery.base import DeliveryResult
from whale_agent.delivery.email import send_digest_email
from whale_agent.delivery.telegram import send_telegram

log = logging.getLogger(__name__)


def alert_operator(
    subject: str, body: str, settings: Settings | None = None
) -> list[DeliveryResult]:
    """Send an operational alert on every configured channel. Returns what happened."""
    s = settings or get_settings()
    results: list[DeliveryResult] = []

    log.error("OPERATOR ALERT: %s -- %s", subject, body)

    if s.operator_alert_email and s.operator_alert_email != s.email_to:
        # Route to the operator address without disturbing the digest configuration.
        s_operator = replace(s, email_to=s.operator_alert_email)
    else:
        s_operator = s

    try:
        results.append(send_digest_email(f"{subject}\n\n{body}", s_operator, subject=subject))
    except Exception as exc:  # a broken alert path must not mask the original failure
        log.warning("Operator email alert failed: %s", exc)
        results.append(DeliveryResult("email", False, str(exc)))

    if s.telegram_enabled:
        try:
            results.append(send_telegram(f"{subject}\n{body}", s))
        except Exception as exc:
            log.warning("Operator telegram alert failed: %s", exc)
            results.append(DeliveryResult("telegram", False, str(exc)))

    return results
