"""Telegram Bot API delivery -- the free, instant secondary channel."""

from __future__ import annotations

import logging

from whale_agent.config import Settings, get_settings
from whale_agent.delivery.base import DeliveryResult
from whale_agent.errors import NotConfiguredError, SourceUnavailableError
from whale_agent.ingestion._http import request_json

log = logging.getLogger(__name__)


def send_telegram(text: str, settings: Settings | None = None) -> DeliveryResult:
    s = settings or get_settings()
    if not s.telegram_enabled:
        return DeliveryResult("telegram", False, "telegram not configured")
    try:
        token = s.require("telegram_bot_token")
        request_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            headers={"Content-Type": "application/json"},
            # No parse_mode: filer and issuer names contain characters that Markdown
            # and HTML modes would choke on, and a formatting error is not worth a
            # dropped alert.
            json_body={"chat_id": s.telegram_chat_id, "text": text},
            settings=s,
        )
    except (NotConfiguredError, SourceUnavailableError) as exc:
        log.warning("Telegram delivery failed: %s", exc)
        return DeliveryResult("telegram", False, str(exc))
    return DeliveryResult("telegram", True)
