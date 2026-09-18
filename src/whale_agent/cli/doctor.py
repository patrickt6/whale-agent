"""`whale doctor`: check the setup and print one fix line for each problem.

Offline by default. `--live` also logs in to the SMTP server, which is the one check that
needs the network and a real password.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from whale_agent.cli.app import _blue, _dim, use_color
from whale_agent.config import SEC_USER_AGENT_DEFAULT, Settings

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    fix: str = ""
    blocking: bool = True  # a failed non-blocking check warns but does not fail the run


def check_env_file(env_path: Path) -> Check:
    if env_path.is_file():
        return Check(".env present", True)
    return Check(
        ".env present",
        False,
        "run `whale settings`, or copy .env.example to .env",
        blocking=False,
    )


def check_email(settings: Settings) -> Check:
    from whale_agent.jobs.digest_daily import email_problems

    if settings.email_provider == "none":
        return Check(
            "email settings", True, "email is off on purpose (WHALE_EMAIL_PROVIDER=none)"
        )
    problems = email_problems(settings)
    if not problems:
        return Check("email settings", True)
    return Check("email settings", False, "; ".join(problems))


def check_sec_user_agent(settings: Settings) -> Check:
    ua = settings.sec_user_agent.strip()
    if (
        ua == SEC_USER_AGENT_DEFAULT
        or "change-me" in ua.lower()
        or "you@example.com" in ua.lower()
    ):
        return Check(
            "SEC user agent",
            False,
            "WHALE_SEC_USER_AGENT is still the placeholder, and SEC rejects it; "
            'set your own name and email, e.g. "Jane Doe jane@example.com"',
        )
    name = _EMAIL.sub("", ua).replace("contact:", "").strip(" ()<>,:")
    if _EMAIL.search(ua) and len(name) >= 2:
        return Check("SEC user agent", True)
    return Check(
        "SEC user agent",
        False,
        'set WHALE_SEC_USER_AGENT to your name and email, e.g. "Jane Doe jane@example.com"',
    )


def check_sources(settings: Settings) -> list[Check]:
    """A source switched on but missing its key is a warning: the digest still ships."""
    from whale_agent.ingestion.registry import build_sources

    checks = []
    for spec in build_sources(settings):
        if spec.enabled:
            checks.append(Check(f"source {spec.name}", True))
        elif spec.disabled_reason.endswith(" not set"):
            key = spec.disabled_reason.removesuffix(" not set")
            checks.append(
                Check(
                    f"source {spec.name}",
                    False,
                    f"{key} is empty: set it, or switch the source off",
                    blocking=False,
                )
            )
    return checks


def check_llm(settings: Settings) -> Check:
    provider = settings.llm_provider
    if provider in {"none", "manual"}:
        return Check(f"AI writer ({provider})", True)
    key = {
        "gemini": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY and OPENAI_MODEL",
    }.get(provider)
    if key is None:
        return Check(
            f"AI writer ({provider})",
            False,
            "set WHALE_LLM_PROVIDER to none, gemini, anthropic, or openai",
        )
    if settings.llm_enabled:
        return Check(f"AI writer ({provider})", True)
    return Check(
        f"AI writer ({provider})", False, f"set {key}, or set WHALE_LLM_PROVIDER=none"
    )


def check_smtp_login(settings: Settings) -> Check:
    import smtplib

    if settings.email_provider != "smtp":
        return Check("SMTP login", True, f"skipped, sender is {settings.email_provider}")
    try:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.http_timeout_seconds
        ) as server:
            if settings.smtp_use_tls:
                server.starttls()
            server.login(settings.smtp_username, settings.smtp_password)
    except (OSError, smtplib.SMTPException) as exc:
        return Check(
            "SMTP login",
            False,
            f"login failed ({type(exc).__name__}); check SMTP_USERNAME and "
            "SMTP_PASSWORD (a Google App Password, not your login)",
        )
    return Check("SMTP login", True)


def check_schedule(state: Callable[[], list[str]] | None = None) -> Check:
    """Warn only: a missing schedule is a choice, not a broken setup."""
    from whale_agent.cli import schedule

    kind = schedule.platform_kind()
    if state is None:

        def state() -> list[str]:
            return schedule.installed(kind, schedule.run_command, Path.home())

    if kind == "unsupported":
        return Check("schedule", True, "not supported on this system", blocking=False)
    try:
        found = state()
    except OSError:
        found = []
    if found:
        return Check("schedule", True, ", ".join(found), blocking=False)
    return Check(
        "schedule", False, "no automatic emails; run `whale schedule on`", blocking=False
    )


def run_checks(settings: Settings, env_path: Path, live: bool = False) -> list[Check]:
    checks = [
        check_env_file(env_path),
        check_email(settings),
        check_sec_user_agent(settings),
        check_llm(settings),
    ]
    checks += check_sources(settings)
    checks.append(check_schedule())
    if live:
        checks.append(check_smtp_login(settings))
    return checks


def run_doctor(
    settings: Settings,
    env_path: Path,
    say: Callable[[str], None] = print,
    color: bool = False,
    live: bool = False,
) -> int:
    checks = run_checks(settings, env_path, live=live)
    for c in checks:
        if c.ok:
            note = _dim(f"  ({c.fix})", color) if c.fix else ""
            say(f"  {_blue('ok  ', color)} {c.name}{note}")
        else:
            tag = "FAIL" if c.blocking else "warn"
            say(f"  {tag} {c.name}")
            say(f"       fix: {c.fix}")
    if not live:
        say(_dim("  SMTP login not tested; run `whale doctor --live` to try it", color))
    failed = [c for c in checks if not c.ok and c.blocking]
    say(f"  {len(failed)} blocking problem(s)" if failed else "  all blocking checks pass")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    from whale_agent.config import load_env_file

    ap = argparse.ArgumentParser(
        prog="whale doctor", description="check the whale-agent setup"
    )
    ap.add_argument("--live", action="store_true", help="also log in to the SMTP server")
    ap.add_argument("--env", default=".env", help="path to the .env file")
    args = ap.parse_args(argv)
    load_env_file(args.env)
    return run_doctor(Settings.from_env(), Path(args.env), color=use_color(), live=args.live)


if __name__ == "__main__":
    raise SystemExit(main())
