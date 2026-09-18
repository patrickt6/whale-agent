"""Per-recipient sender for the public newsletter.

`send_digest_email` in `email.py` sends one message to the operator's own list and has no
way to set per-recipient headers. The newsletter needs a personal unsubscribe link and
RFC 8058 `List-Unsubscribe` headers on every message, so it has its own sender here.

This module never decides whether a body may go out. The caller (`jobs/newsletter.py`)
runs `find_egress_violations` on every assembled message before it calls `send`.

Cloudflare Email Service is deliberately not a backend here: its docs limit it to
transactional mail, and a newsletter is bulk mail. See docs/NEWSLETTER.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

RESEND_URL = "https://api.resend.com/emails"
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: str
    headers: dict[str, str] = field(default_factory=dict)


class SendError(Exception):
    """A send that failed. `retryable` tells the caller whether backoff can help."""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class NewsletterSender(Protocol):
    name: str

    def send(self, message: OutgoingEmail) -> str:
        """Send one message. Return a provider message id. Raise SendError on failure."""


class ResendNewsletterSender:
    """Resend REST API, one request per recipient so each message has its own headers."""

    name = "resend"

    def __init__(
        self,
        api_key: str,
        sender: str,
        client: httpx.Client | None = None,
        url: str = RESEND_URL,
        timeout: float = 20.0,
        reply_to: str = "",
    ) -> None:
        if not api_key:
            raise ValueError("RESEND_API_KEY is not set")
        if not sender:
            raise ValueError("WHALE_NEWSLETTER_FROM is not set")
        self._key = api_key
        self._from = sender
        self._client = client or httpx.Client(timeout=timeout)
        self._url = url
        self._reply_to = reply_to

    def send(self, message: OutgoingEmail) -> str:
        payload = {
            "from": self._from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
            "headers": dict(message.headers),
        }
        if self._reply_to:
            payload["reply_to"] = self._reply_to
        try:
            resp = self._client.post(
                self._url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise SendError(f"transport error: {type(exc).__name__}", retryable=True) from exc
        if resp.status_code >= 400:
            # Never echo the body: it can carry the recipient address.
            raise SendError(
                f"HTTP {resp.status_code}", retryable=resp.status_code in RETRYABLE_STATUS
            )
        try:
            return str((resp.json() or {}).get("id", ""))
        except ValueError:
            return ""
