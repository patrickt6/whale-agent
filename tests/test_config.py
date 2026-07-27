"""Settings, the enable flags, and .env loading.

The behaviour under test is the promise the whole design rests on: a source with no
credential is OFF, not broken. Everything downstream assumes that.
"""

from __future__ import annotations

import pytest

from whale_agent.config import Settings, load_env_file
from whale_agent.errors import NotConfiguredError


def test_defaults_boot_with_no_environment(monkeypatch):
    for key in ("FMP_API_KEY", "QUIVER_QUANT_API_KEY", "GEMINI_API_KEY", "WHALE_EMAIL_TO"):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert s.threshold_usd == 5_000_000.0
    assert s.fmp_enabled is False
    assert s.quiver_enabled is False
    assert s.llm_enabled is False
    assert s.email_enabled is False
    # The keyless sources are the ones that still work with nothing configured.
    assert s.taiwan_enabled is True
    assert s.dataroma_enabled is True


def test_vendor_enabled_requires_both_flag_and_key(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "abc123")
    assert Settings.from_env().fmp_enabled is True

    # An explicit off-switch beats a present key: that is the "I bought it but don't
    # want to spend calls today" case.
    monkeypatch.setenv("WHALE_ENABLE_FMP", "0")
    assert Settings.from_env().fmp_enabled is False


def test_blank_key_disables_without_error(monkeypatch):
    monkeypatch.setenv("QUIVER_QUANT_API_KEY", "   ")
    s = Settings.from_env()
    assert s.quiver_enabled is False


def test_require_names_the_missing_setting():
    s = Settings()
    with pytest.raises(NotConfiguredError, match="fmp_api_key"):
        s.require("fmp_api_key")
    assert s.require("sec_user_agent")


def test_email_enabled_per_provider(monkeypatch):
    monkeypatch.setenv("WHALE_EMAIL_TO", "me@example.com")
    monkeypatch.setenv("WHALE_EMAIL_PROVIDER", "smtp")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    assert Settings.from_env().email_enabled is False

    monkeypatch.setenv("SMTP_USERNAME", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app password")
    assert Settings.from_env().email_enabled is True

    monkeypatch.setenv("WHALE_EMAIL_PROVIDER", "resend")
    assert Settings.from_env().email_enabled is False
    monkeypatch.setenv("RESEND_API_KEY", "re_123")
    assert Settings.from_env().email_enabled is True


def test_llm_provider_switch(monkeypatch):
    monkeypatch.setenv("WHALE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert Settings.from_env().llm_enabled is True

    monkeypatch.setenv("WHALE_LLM_PROVIDER", "anthropic")
    assert Settings.from_env().llm_enabled is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    assert Settings.from_env().llm_enabled is True


def test_load_env_file_does_not_override_real_environment(tmp_path, monkeypatch, clean_env):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "FMP_API_KEY=from_file\n"
        'SMTP_PASSWORD="quoted secret"\n'
        "WHALE_THRESHOLD_USD=1000  # inline comment\n"
        "malformed line\n"
    )
    monkeypatch.setenv("FMP_API_KEY", "from_shell")
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("WHALE_THRESHOLD_USD", raising=False)

    load_env_file(str(env))
    s = Settings.from_env()
    assert s.fmp_api_key == "from_shell"  # the shell wins
    assert s.smtp_password == "quoted secret"
    assert s.threshold_usd == 1000.0


def test_sources_allowlist_parsing(monkeypatch):
    monkeypatch.setenv("WHALE_SOURCES", "sec_form4, taiwan_mops ,")
    assert Settings.from_env().sources == ["sec_form4", "taiwan_mops"]


# -- email diagnostics ---------------------------------------------------------
# `--show-config` has to name the exact variable at fault; "email is off" is not an
# actionable diagnosis when four settings have to line up.


def test_email_problems_names_each_missing_smtp_field():
    from whale_agent.jobs.digest_daily import email_problems

    problems = " ".join(
        email_problems(Settings(email_provider="smtp", email_to="me@example.com"))
    )
    assert "SMTP_USERNAME" in problems
    assert "SMTP_PASSWORD" in problems


def test_email_problems_detects_an_app_password_in_the_from_field():
    from whale_agent.jobs.digest_daily import email_problems

    problems = " ".join(
        email_problems(
            Settings(
                email_provider="smtp",
                email_to="me@example.com",
                smtp_username="me@gmail.com",
                smtp_password="abcdefghijklmnop",
                email_from="qwertyuiopasdfgh",  # 16 chars, no '@'
            )
        )
    )
    assert "WHALE_EMAIL_FROM" in problems
    assert "SMTP_PASSWORD" in problems


def test_email_problems_is_empty_when_correctly_configured():
    from whale_agent.jobs.digest_daily import email_problems

    assert (
        email_problems(
            Settings(
                email_provider="smtp",
                email_to="me@example.com",
                smtp_username="me@gmail.com",
                smtp_password="abcdefghijklmnop",
            )
        )
        == []
    )


def test_email_problems_flags_an_unrecognised_provider():
    from whale_agent.jobs.digest_daily import email_problems

    problems = " ".join(email_problems(Settings(email_provider="sendgrid", email_to="a@b.c")))
    assert "not recognised" in problems
