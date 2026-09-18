"""Email delivery: SMTP message construction, Resend payload, HTML conversion, and the
"never lose the digest" guarantee. No sockets are opened and no API is called."""

from __future__ import annotations

from dataclasses import replace

import pytest

from whale_agent.config import Settings
from whale_agent.delivery.base import digest_subject, digest_to_html
from whale_agent.delivery.email import send_digest_email
from whale_agent.delivery.email_resend import build_payload
from whale_agent.delivery.email_smtp import parse_recipients, send_via_smtp
from whale_agent.errors import NotConfiguredError

DIGEST = """WHALE DIGEST — 2026-07-25
Top move: Jane Example — OPEN-MARKET BUY $12.4M in TestCo.
Coverage note: Japan EDINET not included (not configured).

TIER 2 — NOTABLE
1. Jane Example — OPEN-MARKET BUY $12.4M in TestCo 🇺🇸 (US) — 2026-07-25, 1-day lag
   Why: first-time filer. [https://example.com/filing]
   Skeptic's note: routine grant, low signal.

Skeptic's note: every figure traces to a filing field."""


class FakeSMTP:
    """Stands in for smtplib.SMTP. Records everything, connects to nothing."""

    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.tls = False
        self.login_args = None
        self.messages = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.messages.append(msg)


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSMTP.instances = []
    yield


def _smtp_settings(**overrides) -> Settings:
    base = Settings(
        email_provider="smtp",
        email_to="me@example.com",
        smtp_username="bot@gmail.com",
        smtp_password="app-password",
    )
    return replace(base, **overrides) if overrides else base


# -- SMTP ----------------------------------------------------------------------
def test_smtp_builds_a_multipart_message_and_logs_in():
    result = send_via_smtp(
        "Subject line", DIGEST, digest_to_html(DIGEST), _smtp_settings(), FakeSMTP
    )
    assert result.ok
    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.gmail.com" and smtp.port == 587
    assert smtp.tls is True
    assert smtp.login_args == ("bot@gmail.com", "app-password")

    msg = smtp.messages[0]
    assert msg["Subject"] == "Subject line"
    assert msg["To"] == "me@example.com"
    # From defaults to the SMTP account when WHALE_EMAIL_FROM is unset.
    assert msg["From"] == "bot@gmail.com"
    types = {part.get_content_type() for part in msg.walk()}
    assert {"text/plain", "text/html"} <= types


def test_smtp_supports_multiple_recipients():
    settings = _smtp_settings(email_to="a@x.com, b@y.com; c@z.com")
    send_via_smtp("s", DIGEST, "<p>h</p>", settings, FakeSMTP)
    assert FakeSMTP.instances[0].messages[0]["To"] == "a@x.com, b@y.com, c@z.com"


def test_smtp_transport_failure_is_returned_not_raised():
    class ExplodingSMTP(FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection reset")

    result = send_via_smtp("s", DIGEST, "<p>h</p>", _smtp_settings(), ExplodingSMTP)
    assert result.ok is False
    assert "connection reset" in result.detail


def test_smtp_without_credentials_raises_a_named_error():
    with pytest.raises(NotConfiguredError, match="smtp_password"):
        send_via_smtp("s", DIGEST, "<p>h</p>", _smtp_settings(smtp_password=""), FakeSMTP)


def test_smtp_attaches_a_file_when_given_one():
    """The full report, attached so the reader has it even while the report site is
    down -- the point of the attachment support this test locks in."""
    report_html = "<html><body>full report</body></html>"
    result = send_via_smtp(
        "s",
        DIGEST,
        "<p>h</p>",
        _smtp_settings(),
        FakeSMTP,
        attachments=[("report.html", report_html.encode("utf-8"), "html")],
    )
    assert result.ok
    msg = FakeSMTP.instances[0].messages[0]
    attachments = [p for p in msg.iter_attachments()]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "report.html"
    assert attachments[0].get_content() == report_html


def test_smtp_sends_no_attachment_when_none_given():
    send_via_smtp("s", DIGEST, "<p>h</p>", _smtp_settings(), FakeSMTP)
    msg = FakeSMTP.instances[0].messages[0]
    assert list(msg.iter_attachments()) == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a@x.com", ["a@x.com"]),
        ("a@x.com,b@y.com", ["a@x.com", "b@y.com"]),
        (" a@x.com ; b@y.com ", ["a@x.com", "b@y.com"]),
        ("", []),
    ],
)
def test_parse_recipients(raw, expected):
    assert parse_recipients(raw) == expected


# -- Resend ---------------------------------------------------------------------
def test_resend_payload_shape():
    settings = Settings(
        email_provider="resend",
        resend_api_key="re_test",
        email_to="me@example.com",
        email_from="alerts@mydomain.com",
    )
    payload = build_payload("Subject", DIGEST, "<p>h</p>", settings)
    assert payload["from"] == "alerts@mydomain.com"
    assert payload["to"] == ["me@example.com"]
    assert payload["subject"] == "Subject"
    assert payload["html"] == "<p>h</p>"
    assert payload["text"] == DIGEST


def test_resend_requires_a_verified_from_address():
    settings = Settings(
        email_provider="resend", resend_api_key="re", email_to="me@example.com"
    )
    with pytest.raises(NotConfiguredError, match="email_from"):
        build_payload("s", DIGEST, "<p>h</p>", settings)


# -- Dispatcher -----------------------------------------------------------------
def test_unconfigured_email_is_skipped_not_fatal():
    result = send_digest_email(DIGEST, Settings())
    assert result.ok is False
    assert "disabled" in result.detail


def test_send_digest_email_passes_the_attachment_through_to_smtp(monkeypatch):
    import whale_agent.delivery.email as email_mod

    captured = {}

    def fake_send_via_smtp(subject, text, html, settings, attachments=None):
        captured["attachments"] = attachments
        from whale_agent.delivery.base import DeliveryResult

        return DeliveryResult("smtp", True, "ok")

    monkeypatch.setattr(email_mod, "send_via_smtp", fake_send_via_smtp)
    result = send_digest_email(
        DIGEST,
        _smtp_settings(),
        attachment=("report.html", b"<html></html>"),
    )
    assert result.ok
    assert captured["attachments"] == [("report.html", b"<html></html>", "html")]


def test_email_provider_none_is_respected():
    settings = _smtp_settings(email_provider="none")
    assert send_digest_email(DIGEST, settings).ok is False


# -- HTML conversion -------------------------------------------------------------
def test_digest_to_html_structure():
    html = digest_to_html(DIGEST)
    assert html.startswith("<!doctype html>")
    assert "WHALE DIGEST" in html
    assert "<h2>TIER 2 — NOTABLE</h2>" in html
    assert 'class="coverage"' in html
    assert '<a href="https://example.com/filing">source</a>' in html
    # Both colour schemes are styled: the reader's client picks.
    assert "prefers-color-scheme: dark" in html


def test_digest_to_html_escapes_untrusted_text():
    """Filer names come from third-party feeds and land inside the email body."""
    hostile = "WHALE DIGEST — 2026-07-25\n1. <script>alert('x')</script> — BUY $5.0M in Co"
    html = digest_to_html(hostile)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_digest_subject_uses_the_first_line():
    assert digest_subject(DIGEST) == "WHALE DIGEST — 2026-07-25"
