"""`whale newsletter send`: fake HTTP, fake sender, no network, no real email."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from whale_agent.cli import main as cli_main
from whale_agent.config import Settings
from whale_agent.delivery.newsletter_sender import (
    OutgoingEmail,
    ResendNewsletterSender,
    SendError,
)
from whale_agent.jobs import newsletter as nl

API = "https://site.test"
TOKEN_A = "a" * 48
TOKEN_B = "b" * 48
FOOTER = "Whale Agent, 123 Example Street, Kingston ON, Canada"
ON = date(2026, 9, 14)  # a Monday


def subscriber_client(rows=None, status=200, seen=None):
    rows = (
        rows
        if rows is not None
        else [
            {"email": "One@Example.com", "token": TOKEN_A},
            {"email": "two@example.com", "token": TOKEN_B},
            {"email": "one@example.com", "token": TOKEN_A},  # duplicate
            {"email": "", "token": "x"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"ok": status == 200, "subscribers": rows})

    return httpx.Client(transport=httpx.MockTransport(handler))


class FakeSender:
    name = "fake"

    def __init__(self, fail_plan=None):
        self.sent: list[OutgoingEmail] = []
        self.calls = 0
        self.fail_plan = list(fail_plan or [])

    def send(self, message: OutgoingEmail) -> str:
        self.calls += 1
        if self.fail_plan:
            err = self.fail_plan.pop(0)
            if err is not None:
                raise err
        self.sent.append(message)
        return f"id-{len(self.sent)}"


def fake_render(text="WHALE BRIEF 2026-09-14\nNothing large today.", html=None):
    def render(settings, cadence, on):
        markup = html or "<!doctype html><html><body><p>brief body</p></body></html>"
        return nl.Brief(
            subject=f"Whale Agent {cadence} brief", text=text, html=markup, events=[]
        )

    return render


def env(tmp_path, **extra):
    e = {
        "WHALE_NEWSLETTER_API": API,
        "WHALE_NEWSLETTER_ADMIN_TOKEN": "admin-secret",
        "WHALE_NEWSLETTER_FOOTER": FOOTER,
        "WHALE_NEWSLETTER_SENT_LOG": str(tmp_path / "sent.jsonl"),
        "WHALE_NEWSLETTER_PAUSE_S": "0",
    }
    e.update(extra)
    return e


def run(tmp_path, args, sender=None, render=None, client=None, e=None, sleeps=None):
    lines: list[str] = []
    code = nl.run(
        args,
        env=e if e is not None else env(tmp_path),
        settings=Settings(),
        render=render or fake_render(),
        client=client or subscriber_client(),
        sender=sender,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        out=lines.append,
    )
    return code, lines


# -- free sources -----------------------------------------------------------------------


def test_free_settings_clears_paid_flags_keys_and_llm():
    s = Settings(
        fmp_api_key="k",
        quiver_api_key="k",
        arkham_api_key="k",
        whale_alert_api_key="k",
        finnhub_api_key="k",
        unusual_whales_api_key="k",
        anthropic_api_key="k",
        enable_finnhub=True,
        enable_unusual_whales=True,
        llm_provider="anthropic",
        sources=["sec_form4", "fmp_insider", "quiver_congress"],
    )
    f = nl.free_settings(s)
    for flag in (
        "enable_fmp",
        "enable_quiver",
        "enable_arkham",
        "enable_whale_alert",
        "enable_finnhub",
        "enable_unusual_whales",
    ):
        assert getattr(f, flag) is False
    for key in (
        "fmp_api_key",
        "quiver_api_key",
        "arkham_api_key",
        "whale_alert_api_key",
        "finnhub_api_key",
        "unusual_whales_api_key",
        "anthropic_api_key",
        "openai_api_key",
    ):
        assert getattr(f, key) == ""
    assert f.llm_provider == "none"
    assert f.sources == ["sec_form4"]
    assert (
        nl.free_settings(Settings(llm_provider="gemini", gemini_api_key="g")).llm_provider
        == "gemini"
    )
    assert (
        nl.free_settings(Settings(llm_provider="gemini", gemini_api_key="")).llm_provider
        == "none"
    )


def test_no_paid_spec_is_enabled_and_no_fmp_price_provider():
    from whale_agent.ingestion.registry import build_sources
    from whale_agent.jobs.pipeline import build_price_provider

    s = nl.free_settings(
        Settings(
            fmp_api_key="k",
            quiver_api_key="k",
            arkham_api_key="k",
            whale_alert_api_key="k",
            finnhub_api_key="k",
            unusual_whales_api_key="k",
            enable_finnhub=True,
            enable_unusual_whales=True,
        )
    )
    enabled = [sp.name for sp in build_sources(s, limit=1, on=ON) if sp.enabled]
    assert not [n for n in enabled if nl.is_paid_source(n)], enabled
    assert build_price_provider(s) is None


# -- subscribers ------------------------------------------------------------------------


def test_fetch_subscribers_sends_bearer_and_dedupes():
    seen: list[httpx.Request] = []
    subs = nl.fetch_subscribers(API + "/", "admin-secret", subscriber_client(seen=seen))
    assert [s.email for s in subs] == ["one@example.com", "two@example.com"]
    assert str(seen[0].url) == "https://site.test/api/subscribers"
    assert seen[0].headers["authorization"] == "Bearer admin-secret"


def test_fetch_subscribers_http_error_raises():
    with pytest.raises(RuntimeError, match="HTTP 401"):
        nl.fetch_subscribers(API, "bad", subscriber_client(status=401))


# -- dry run ----------------------------------------------------------------------------


def test_dry_run_prints_counts_and_subject_sends_nothing(tmp_path):
    sender = FakeSender()
    code, lines = run(
        tmp_path,
        ["send", "--dry-run", "--cadence", "weekly", "--date", ON.isoformat()],
        sender=sender,
    )
    assert code == 0
    assert "Subject: Whale Agent weekly brief" in lines
    assert any("Confirmed subscribers: 2" in x and "to send: 2" in x for x in lines)
    assert lines[-1] == "Dry run: nothing sent."
    assert sender.calls == 0
    assert not (tmp_path / "sent.jsonl").exists()


def test_dry_run_without_api_counts_zero(tmp_path):
    code, lines = run(
        tmp_path,
        ["send", "--dry-run"],
        e={"WHALE_NEWSLETTER_SENT_LOG": str(tmp_path / "s.jsonl")},
    )
    assert code == 0
    assert any("Confirmed subscribers: 0" in x for x in lines)


# -- real send (fake sender) ------------------------------------------------------------


def test_send_personalises_each_message_with_unsubscribe_headers_and_footer(tmp_path):
    sender = FakeSender()
    code, lines = run(
        tmp_path, ["send", "--cadence", "daily", "--date", ON.isoformat()], sender=sender
    )
    assert code == 0, lines
    assert [m.to for m in sender.sent] == ["one@example.com", "two@example.com"]
    first = sender.sent[0]
    url = f"{API}/api/unsubscribe?token={TOKEN_A}"
    assert first.headers == {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert url in first.text and FOOTER in first.text and "Not investment advice" in first.text
    assert url in first.html and FOOTER in first.html
    assert first.html.index(url) < first.html.lower().index("</body>")
    assert TOKEN_B not in first.text + first.html  # no other subscriber's token leaks
    assert "Sent: 2; failed: 0" in lines[-1]


def test_rerun_same_period_does_not_double_send(tmp_path):
    s1 = FakeSender()
    run(tmp_path, ["send", "--cadence", "weekly", "--date", "2026-09-14"], sender=s1)
    s2 = FakeSender()
    code, lines = run(
        tmp_path, ["send", "--cadence", "weekly", "--date", "2026-09-17"], sender=s2
    )  # same ISO week
    assert code == 0
    assert s2.calls == 0
    assert any("already sent this period: 2" in x for x in lines)
    s3 = FakeSender()
    run(
        tmp_path, ["send", "--cadence", "weekly", "--date", "2026-09-21"], sender=s3
    )  # next week
    assert len(s3.sent) == 2
    log_text = (tmp_path / "sent.jsonl").read_text()
    assert "one@example.com" not in log_text  # hash only


def test_retry_with_backoff_then_success(tmp_path):
    sender = FakeSender(
        fail_plan=[SendError("HTTP 429", True), SendError("HTTP 503", True), None]
    )
    sleeps: list[float] = []
    code, _ = run(tmp_path, ["send", "--date", ON.isoformat()], sender=sender, sleeps=sleeps)
    assert code == 0
    assert len(sender.sent) == 2
    assert sleeps[:2] == [1.0, 2.0]


def test_permanent_failure_is_logged_and_rerun_retries_only_that_one(tmp_path):
    sender = FakeSender(fail_plan=[SendError("HTTP 422", False)])
    code, lines = run(tmp_path, ["send", "--date", ON.isoformat()], sender=sender)
    assert code == 1
    assert [m.to for m in sender.sent] == ["two@example.com"]
    again = FakeSender()
    code2, _ = run(tmp_path, ["send", "--date", ON.isoformat()], sender=again)
    assert code2 == 0
    assert [m.to for m in again.sent] == ["one@example.com"]


def test_stops_after_consecutive_failures(tmp_path):
    rows = [{"email": f"u{i}@example.com", "token": f"{i:048x}"} for i in range(10)]
    sender = FakeSender(fail_plan=[SendError("HTTP 400", False)] * 10)
    code, lines = run(
        tmp_path,
        ["send", "--date", ON.isoformat()],
        sender=sender,
        client=subscriber_client(rows),
    )
    assert code == 1
    assert sender.calls == 5


def test_batching_pauses_between_batches(tmp_path):
    rows = [{"email": f"u{i}@example.com", "token": f"{i:048x}"} for i in range(5)]
    sleeps: list[float] = []
    e = env(tmp_path, WHALE_NEWSLETTER_BATCH_SIZE="2", WHALE_NEWSLETTER_BATCH_PAUSE_S="7")
    code, _ = run(
        tmp_path,
        ["send", "--date", ON.isoformat()],
        sender=FakeSender(),
        client=subscriber_client(rows),
        e=e,
        sleeps=sleeps,
    )
    assert code == 0
    assert sleeps.count(7.0) == 2  # before message 3 and message 5


def test_real_send_refuses_without_footer_token_or_https(tmp_path):
    sender = FakeSender()
    e = env(tmp_path, WHALE_NEWSLETTER_FOOTER="", WHALE_NEWSLETTER_API="http://site.test")
    code, lines = run(tmp_path, ["send"], sender=sender, e=e)
    assert code == 2
    assert any("FOOTER" in x for x in lines) and any("https" in x for x in lines)
    assert sender.calls == 0


# -- egress screen ----------------------------------------------------------------------


def test_egress_block_on_shared_render_sends_nothing(tmp_path):
    sender = FakeSender()
    bad = fake_render(text="WHALE BRIEF\nA fund bought $1600000.0B of stock.")
    code, lines = run(tmp_path, ["send", "--date", ON.isoformat()], sender=sender, render=bad)
    assert code == 3
    assert sender.calls == 0
    assert any("egress screen blocked" in x for x in lines)


def test_egress_runs_on_each_personalised_message(tmp_path, monkeypatch):
    calls: list[tuple] = []
    real = nl.find_egress_violations

    def spy(*bodies, **kw):
        calls.append(bodies)
        return real(*bodies, **kw)

    monkeypatch.setattr(nl, "find_egress_violations", spy)
    sender = FakeSender()
    run(tmp_path, ["send", "--date", ON.isoformat()], sender=sender)
    personal = [
        b
        for b in calls
        if any(f"token={TOKEN_A}" in (x or "") for x in b)
        or any(f"token={TOKEN_B}" in (x or "") for x in b)
    ]
    assert len(personal) == 2  # one screen per recipient message, after personalisation


def test_egress_block_on_personalised_message_stops_run(tmp_path):
    sender = FakeSender()
    e = env(tmp_path, WHALE_NEWSLETTER_FOOTER="Whale Agent owes nobody $9000000T today.")
    code, _ = run(tmp_path, ["send", "--date", ON.isoformat()], sender=sender, e=e)
    assert code == 3
    assert sender.calls == 0


def test_unsubscribe_token_in_url_is_not_read_as_money():
    assert nl.find_egress_violations(f"see {API}/api/unsubscribe?token={'9' * 48}") == []


# -- Resend sender (fake HTTP) ----------------------------------------------------------


def test_resend_sender_payload_and_errors():
    seen: list[httpx.Request] = []
    status = {"code": 200}

    def handler(request):
        seen.append(request)
        return httpx.Response(status["code"], json={"id": "re-1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    s = ResendNewsletterSender("re_key", "Whale Agent <brief@example.com>", client=client)
    msg = OutgoingEmail(
        "x@example.com", "Subj", "text", "<p>h</p>", {"List-Unsubscribe": "<https://a/u>"}
    )
    assert s.send(msg) == "re-1"
    body = json.loads(seen[0].content)
    assert body["to"] == ["x@example.com"] and body["headers"] == {
        "List-Unsubscribe": "<https://a/u>"
    }
    assert seen[0].headers["authorization"] == "Bearer re_key"
    status["code"] = 429
    with pytest.raises(SendError) as e1:
        s.send(msg)
    assert e1.value.retryable is True
    status["code"] = 422
    with pytest.raises(SendError) as e2:
        s.send(msg)
    assert e2.value.retryable is False and "x@example.com" not in str(e2.value)


def test_build_sender_requires_key_and_from(tmp_path):
    code, lines = run(tmp_path, ["send", "--date", ON.isoformat()], sender=None)
    assert code == 2
    assert any("RESEND_API_KEY" in x for x in lines)


# -- CLI --------------------------------------------------------------------------------


def test_cli_dispatches_newsletter(monkeypatch):
    got = {}

    def fake_main():
        import sys

        got["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(nl, "main", fake_main)
    assert cli_main.main(["newsletter", "send", "--dry-run"]) == 0
    assert got["argv"] == ["whale newsletter", "send", "--dry-run"]
    assert "newsletter" in cli_main.COMMANDS


def test_render_free_brief_offline_drops_paid_specs_and_rows(monkeypatch):
    from whale_agent.ingestion import registry
    from whale_agent.ingestion.registry import SourceSpec
    from whale_agent.jobs.digest_daily import _demo_events

    demo = _demo_events()
    paid_row = demo[0].model_copy(update={"source": "fmp_insider"})
    ran: list[str] = []

    def fake_build_sources(settings, *, limit=50, on=None, store=None):
        def free_run():
            ran.append("sec_form4")
            return list(demo) + [paid_row]

        def paid_run():  # pragma: no cover - must never run
            ran.append("fmp_insider")
            return []

        return [
            SourceSpec("sec_form4", "SEC Form 4", True, free_run),
            SourceSpec("fmp_insider", "FMP insider trades", True, paid_run),
        ]

    monkeypatch.setattr(registry, "build_sources", fake_build_sources)
    brief = nl.render_free_brief(Settings(llm_provider="none"), "weekly", date(2026, 7, 27))
    assert ran == ["sec_form4"]
    assert brief.subject == "Whale Agent weekly brief, week of 2026-07-27"
    assert brief.text and "<html" in brief.html.lower()
    assert all(not nl.is_paid_source(ev.source) for ev in brief.events)
    assert "FMP" not in brief.text
    assert (
        nl.find_egress_violations(brief.subject, brief.text, brief.html, events=brief.events)
        == []
    )


# -- privacy hardening ------------------------------------------------------------------


def test_sent_log_uses_a_keyed_hash_not_a_plain_sha256(tmp_path):
    import hashlib

    code, _ = run(tmp_path, ["send", "--date", ON.isoformat()], sender=FakeSender())
    assert code == 0
    text = (tmp_path / "sent.jsonl").read_text()
    plain = hashlib.sha256(b"two@example.com").hexdigest()
    assert plain not in text and "two@example.com" not in text
    assert nl.email_hash("two@example.com", "admin-secret") in text
    assert '"hash": "hmac-sha256"' in text


def test_sent_log_reads_legacy_sha256_rows(tmp_path):
    import hashlib

    path = tmp_path / "old.jsonl"
    row = {
        "cadence": "weekly",
        "period": "2026-09-14",
        "status": "sent",
        "email_sha256": hashlib.sha256(b"a@example.com").hexdigest(),
    }
    path.write_text(json.dumps(row) + "\n")
    assert nl.SentLog(path).already_sent("weekly", "2026-09-14", "a@example.com")


@pytest.mark.parametrize(
    "sender", ["Whale Agent <me@gmail.com>", "someone@Outlook.com", "nobody"]
)
def test_build_sender_refuses_free_mail_or_bad_from(sender):
    with pytest.raises(ValueError):
        nl.build_sender({"RESEND_API_KEY": "re_x", "WHALE_NEWSLETTER_FROM": sender})


def test_build_sender_accepts_own_domain_and_reply_to():
    seen: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (seen.append(r), httpx.Response(200, json={"id": "1"}))[1]
        )
    )
    s = ResendNewsletterSender(
        "re_x", "Whale Agent <brief@example.org>", client=client, reply_to="hello@example.org"
    )
    s.send(OutgoingEmail("x@example.com", "S", "t", "<p>h</p>"))
    body = json.loads(seen[0].content)
    assert body["reply_to"] == "hello@example.org" and body["from"].endswith("@example.org>")
    assert body["to"] == ["x@example.com"] and "cc" not in body and "bcc" not in body
    assert nl.build_sender(
        {
            "RESEND_API_KEY": "re_x",
            "WHALE_NEWSLETTER_FROM": "Whale Agent <brief@example.org>",
            "WHALE_NEWSLETTER_REPLY_TO": "hello@example.org",
        }
    )


def test_run_output_never_prints_a_subscriber_address(tmp_path):
    code, lines = run(tmp_path, ["send", "--date", ON.isoformat()], sender=FakeSender())
    assert code == 0
    joined = "\n".join(lines).lower()
    assert "@example.com" not in joined
