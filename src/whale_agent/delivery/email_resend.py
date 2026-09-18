"""Email via the Resend REST API.

The alternative backend to SMTP: better deliverability and a free tier, at the cost of
signing up and verifying a sending domain. Selected with `WHALE_EMAIL_PROVIDER=resend`.
"""

from __future__ import annotations

import logging

from whale_agent.config import Settings, get_settings
from whale_agent.delivery.base import DeliveryResult
from whale_agent.delivery.email_smtp import parse_recipients
from whale_agent.errors import NotConfiguredError, SourceUnavailableError
from whale_agent.ingestion._http import request_json

log = logging.getLogger(__name__)


def build_payload(subject: str, text_body: str, html_body: str, settings: Settings) -> dict:
    recipients = parse_recipients(settings.email_to)
    if not recipients:
        raise NotConfiguredError("Settings.email_to is not set (WHALE_EMAIL_TO).")
    sender = settings.email_from
    if not sender:
        raise NotConfiguredError(
            "Settings.email_from is not set (WHALE_EMAIL_FROM); Resend requires a "
            "verified sending address."
        )
    return {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }


def send_via_resend(
    subject: str,
    text_body: str,
    html_body: str,
    settings: Settings | None = None,
) -> DeliveryResult:
    s = settings or get_settings()
    key = s.require("resend_api_key")
    payload = build_payload(subject, text_body, html_body, s)
    try:
        data = request_json(
            f"{s.resend_base_url.rstrip('/')}/emails",
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json_body=payload,
            settings=s,
        )
    except SourceUnavailableError as exc:
        log.warning("Resend delivery failed: %s", exc)
        return DeliveryResult("resend", False, str(exc))
    message_id = (data or {}).get("id", "") if isinstance(data, dict) else ""
    return DeliveryResult("resend", True, f"id={message_id}")
