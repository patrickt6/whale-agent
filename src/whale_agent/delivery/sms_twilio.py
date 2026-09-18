"""Twilio SMS for Tier-1 instant alerts.

Twilio's message endpoint takes form-encoded bodies and HTTP Basic auth, which is why
this posts directly with httpx rather than going through `_http.request_json`.
"""

from __future__ import annotations

import logging

import httpx

from whale_agent.config import Settings, get_settings
from whale_agent.delivery.base import DeliveryResult

log = logging.getLogger(__name__)

SMS_MAX_CHARS = 320  # two segments; instant alerts should be a glance, not a read


def send_sms(text: str, settings: Settings | None = None) -> DeliveryResult:
    s = settings or get_settings()
    if not s.twilio_enabled:
        return DeliveryResult("twilio", False, "twilio not configured")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
    try:
        resp = httpx.post(
            url,
            data={
                "From": s.twilio_from_number,
                "To": s.twilio_to_number,
                "Body": text[:SMS_MAX_CHARS],
            },
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            timeout=s.http_timeout_seconds,
        )
        if resp.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    except httpx.HTTPError as exc:
        log.warning("Twilio delivery failed: %s", exc)
        return DeliveryResult("twilio", False, str(exc))
    return DeliveryResult("twilio", True)
