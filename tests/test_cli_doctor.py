from dataclasses import replace
from pathlib import Path

from whale_agent.cli import doctor
from whale_agent.config import Settings

GOOD = Settings(
    sec_user_agent="Jane Doe jane@example.com",
    email_provider="smtp",
    email_to="jane@example.com",
    smtp_username="jane@example.com",
    smtp_password="abcdabcdabcdabcd",
    llm_provider="none",
    enable_fmp=False,
    enable_quiver=False,
    enable_japan=False,
    enable_arkham=False,
    enable_whale_alert=False,
)


def _by_name(checks):
    return {c.name: c for c in checks}


def test_good_settings_pass(tmp_path: Path, monkeypatch):
    # the schedule state belongs to the host machine; pin it so the test is stable
    monkeypatch.setattr(doctor, "check_schedule", lambda: doctor.Check("schedule", True))
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    out: list[str] = []
    assert doctor.run_doctor(GOOD, env, say=out.append) == 0
    assert not any(line.lstrip().startswith(("FAIL", "warn")) for line in out)
    assert any("SMTP login not tested" in line for line in out)


def test_missing_env_file_warns_but_does_not_block(tmp_path: Path):
    check = doctor.check_env_file(tmp_path / ".env")
    assert not check.ok and not check.blocking and "whale settings" in check.fix


def test_email_problems_block(tmp_path: Path):
    bad = replace(GOOD, smtp_password="")
    check = doctor.check_email(bad)
    assert not check.ok and check.blocking and "SMTP_PASSWORD" in check.fix
    assert doctor.run_doctor(bad, tmp_path / ".env", say=lambda _l: None) == 1


def test_email_off_is_ok():
    assert doctor.check_email(replace(GOOD, email_provider="none", email_to="")).ok


def test_sec_user_agent_format():
    assert doctor.check_sec_user_agent(GOOD).ok
    assert not doctor.check_sec_user_agent(replace(GOOD, sec_user_agent="whale-agent")).ok
    assert not doctor.check_sec_user_agent(replace(GOOD, sec_user_agent="a@b.co")).ok
    default = doctor.check_sec_user_agent(Settings())
    assert not default.ok and default.blocking
    example = doctor.check_sec_user_agent(
        replace(GOOD, sec_user_agent="Your Name you@example.com")
    )
    assert not example.ok and example.blocking


def test_llm_provider_needs_its_key():
    assert doctor.check_llm(GOOD).ok
    gemini = doctor.check_llm(replace(GOOD, llm_provider="gemini"))
    assert not gemini.ok and "GEMINI_API_KEY" in gemini.fix
    assert doctor.check_llm(replace(GOOD, llm_provider="gemini", gemini_api_key="k")).ok
    anthropic = doctor.check_llm(replace(GOOD, llm_provider="anthropic"))
    assert not anthropic.ok and "ANTHROPIC_API_KEY" in anthropic.fix
    assert not doctor.check_llm(replace(GOOD, llm_provider="gpt")).ok


def test_enabled_source_without_key_warns():
    checks = _by_name(doctor.check_sources(replace(GOOD, enable_fmp=True)))
    fmp = checks["source fmp_insider"]
    assert not fmp.ok and not fmp.blocking and "FMP_API_KEY" in fmp.fix
    assert checks["source sec_form4"].ok
    assert "source fmp_insider" not in _by_name(doctor.check_sources(GOOD))


def test_live_flag_adds_smtp_check(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        doctor, "check_smtp_login", lambda s: doctor.Check("SMTP login", False, "login failed")
    )
    names = [c.name for c in doctor.run_checks(GOOD, tmp_path / ".env")]
    assert "SMTP login" not in names
    out: list[str] = []
    assert doctor.run_doctor(GOOD, tmp_path / ".env", say=out.append, live=True) == 1
    assert any("login failed" in line for line in out)
