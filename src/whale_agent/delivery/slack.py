"""Slack incoming-webhook delivery."""

from __future__ import annotations

import logging

from whale_agent.config import Settings, get_settings
from whale_agent.delivery.base import DeliveryResult
from whale_agent.errors import SourceUnavailableError
from whale_agent.ingestion._http import request_text

log = logging.getLogger(__name__)


def send_slack(text: str, settings: Settings | None = None) -> DeliveryResult:
    s = settings or get_settings()
    if not s.slack_webhook_url:
        return DeliveryResult("slack", False, "slack webhook not configured")
    try:
        # Slack webhooks answer with the literal string "ok", not JSON.
        request_text(
            s.slack_webhook_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            json_body={"text": text},
            settings=s,
        )
    except SourceUnavailableError as exc:
        log.warning("Slack delivery failed: %s", exc)
        return DeliveryResult("slack", False, str(exc))
    return DeliveryResult("slack", True)
