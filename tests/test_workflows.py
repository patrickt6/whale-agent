"""Structural checks on the workflow files.

These do not run the workflows. They assert the few ordering properties whose violation
would be silent and expensive: uploading a corpus before the job that failed finished
writing it, or a daily ingest that quietly emails the reader every morning.

Deliberately text-based rather than YAML-parsed. PyYAML is not a dependency of this
project, and a parsed version guarded by `importorskip` would skip silently on any
machine without it -- a test that can vanish is worse than a blunter test that cannot.
"""

from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _run_lines(name: str) -> list[str]:
    """Every line inside a `run:` block, in file order.

    Handles block scalars (`run: |`) as well as one-liners: a step's command may sit on
    following lines, and an ordering assertion that silently stops matching is worse
    than no assertion.
    """
    out: list[str] = []
    in_run = False
    indent = 0
    for raw in _text(name).splitlines():
        stripped = raw.strip()
        if stripped.startswith("run:"):
            in_run = True
            indent = len(raw) - len(raw.lstrip())
            out.append(stripped)
            continue
        if in_run:
            if not stripped:
                continue
            if (len(raw) - len(raw.lstrip())) > indent:
                out.append(stripped)
                continue
            in_run = False
    return out


def _index_of(runs: list[str], needle: str) -> int:
    for i, line in enumerate(runs):
        if needle in line:
            return i
    raise AssertionError(f"no run step containing {needle!r} in {runs}")


def test_daily_ingest_runs_every_day():
    assert 'cron: "0 9 * * *"' in _text("daily-ingest.yml")


def test_daily_ingest_does_not_email_anyone():
    """The daily grows the corpus. Exactly one email leaves this system per week."""
    body = _text("daily-ingest.yml")
    assert "--dry-run" in body
    assert "digest_weekly" not in body
    assert "WHALE_EMAIL_PROVIDER: none" in body


def test_daily_uploads_the_corpus_last():
    """A crashed ingest must leave the previous good corpus in place."""
    runs = _run_lines("daily-ingest.yml")
    assert _index_of(runs, "corpus.sh upload") == len(runs) - 1


def test_daily_downloads_before_it_ingests():
    runs = _run_lines("daily-ingest.yml")
    assert _index_of(runs, "corpus.sh download") < _index_of(runs, "digest_daily")


def test_weekly_registers_several_redundant_monday_crons():
    """One cron time cannot be relied on: GitHub started the 11:00 run at 13:43 once."""
    body = _text("weekly-report.yml")
    crons = [line for line in body.splitlines() if "cron:" in line]
    assert len(crons) >= 3, crons
    assert all("* * 1" in c for c in crons), "every cron must be Monday-only"


def test_weekly_gates_the_send_on_core_source_freshness():
    runs = _run_lines("weekly-report.yml")
    assert _index_of(runs, "watchdog --gate") < _index_of(runs, "digest_weekly")


def test_weekly_downloads_the_corpus_before_the_gate_reads_it():
    """The gate decides from digest_runs, which lives in the corpus."""
    runs = _run_lines("weekly-report.yml")
    assert _index_of(runs, "corpus.sh download") < _index_of(runs, "schedule_gate")


def test_every_weekly_step_after_the_hour_check_is_conditional():
    """A missing `if:` here is how the reader gets two emails on a winter Monday."""
    body = _text("weekly-report.yml")
    after_check = body.split("id: when", 1)[1]
    conditional = after_check.count("if: steps.when.outputs.run == 'true'")
    runs_after = sum(1 for line in after_check.splitlines() if line.strip().startswith("run:"))
    # Every run step after the check is guarded, except the check's own run line.
    assert conditional == runs_after - 1


def test_weekly_uploads_the_corpus_last():
    runs = _run_lines("weekly-report.yml")
    assert _index_of(runs, "corpus.sh upload") == len(runs) - 1


def test_weekly_publishes_the_pages_before_it_emails_their_links():
    """A delivered email must never link to a page that has not been published."""
    runs = _run_lines("weekly-report.yml")
    deploy_at = _index_of(runs, "wrangler")
    # The send is the LAST digest_weekly invocation; the first is the dry render.
    send_at = max(i for i, r in enumerate(runs) if "digest_weekly" in r)
    assert deploy_at < send_at


def test_the_render_step_does_not_send():
    body = _text("weekly-report.yml")
    render_block = body.split("Render the weekly and publish its pages", 1)[1].split(
        "- name:", 1
    )[0]
    assert "--dry-run" in render_block


def test_newsletter_send_has_no_hosting_step_before_it():
    """#18: report hosting must never be able to stop the newsletter send."""
    body = _text("newsletter.yml")
    runs = _run_lines("newsletter.yml")
    send_at = _index_of(runs, "whale newsletter send")
    for i, line in enumerate(runs):
        if "wrangler" in line or "deploy" in line:
            assert i > send_at, line
    send_block = body.split("- name: Send the newsletter", 1)[1].split("- name:", 1)[0]
    assert "if:" not in send_block.split("run:", 1)[0], (
        "the send must not be conditional on another step"
    )
    assert "needs:" not in body


def test_newsletter_uses_no_paid_secret():
    body = _text("newsletter.yml")
    for name in (
        "FMP_API_KEY",
        "QUIVER",
        "ARKHAM",
        "WHALE_ALERT",
        "FINNHUB",
        "UNUSUAL_WHALES",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "WHALE_ENABLE_FMP",
    ):
        assert name not in body, name


def test_newsletter_saves_the_sent_log_even_after_a_failed_send():
    body = _text("newsletter.yml")
    save_block = body.split("- name: Save the sent log", 1)[1]
    assert "if: always()" in save_block
    assert (
        body.index("Restore the sent log")
        < body.index("Send the newsletter")
        < body.index("Save the sent log")
    )


def test_newsletter_runs_weekly_and_on_dispatch():
    body = _text("newsletter.yml")
    assert 'cron: "0 14 * * 1"' in body
    assert "workflow_dispatch:" in body
