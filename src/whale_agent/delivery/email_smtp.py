"""Email over SMTP -- the zero-signup path (Gmail App Password).

Gmail requires an App Password, not the account password: 2-Step Verification must be
on, and the 16-character App Password goes in `SMTP_PASSWORD`.

`smtplib` is injected as a parameter so tests can substitute a fake SMTP class and
assert on the built message without opening a socket.
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.delivery.base import DeliveryResult
from whale_agent.errors import NotConfiguredError

log = logging.getLogger(__name__)


def build_message(
    subject: str,
    text_body: str,
    html_body: str,
    sender: str,
    recipients: Sequence[str],
    attachments: Sequence[tuple[str, bytes, str]] | None = None,
) -> EmailMessage:
    """Build a multipart/alternative message: plain text first, HTML as the upgrade.

    `attachments` is a list of (filename, content_bytes, subtype) -- e.g. the full
    report page, attached so the reader still has it while the report site is down
    (the article-base-url deep links degrade to nothing rather than a dead
    file:// link in that case; see delivery/email.py).
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    for filename, content, subtype in attachments or []:
        msg.add_attachment(content, maintype="text", subtype=subtype, filename=filename)
    return msg


def parse_recipients(raw: str) -> list[str]:
    return [addr.strip() for addr in raw.replace(";", ",").split(",") if addr.strip()]


def send_via_smtp(
    subject: str,
    text_body: str,
    html_body: str,
    settings: Settings | None = None,
    smtp_factory: Callable[..., Any] | None = None,
    attachments: Sequence[tuple[str, bytes, str]] | None = None,
) -> DeliveryResult:
    """Send one email. Raises `NotConfiguredError` only when credentials are absent;
    transport failures are returned as a failed `DeliveryResult`."""
    s = settings or get_settings()
    username = s.require("smtp_username")
    password = s.require("smtp_password")
    recipients = parse_recipients(s.email_to)
    if not recipients:
        raise NotConfiguredError("Settings.email_to is not set (WHALE_EMAIL_TO).")
    sender = s.email_from or username

    msg = build_message(subject, text_body, html_body, sender, recipients, attachments)
    factory = smtp_factory or smtplib.SMTP
    try:
        with factory(s.smtp_host, s.smtp_port, timeout=s.http_timeout_seconds) as server:
            if s.smtp_use_tls:
                server.starttls()
            server.login(username, password)
            server.send_message(msg)
    except Exception as exc:  # smtplib raises a wide family; none should be fatal here
        log.warning("SMTP delivery failed: %s", exc)
        return DeliveryResult("smtp", False, str(exc))
    return DeliveryResult("smtp", True, f"sent to {len(recipients)} recipient(s)")
