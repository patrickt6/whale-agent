"""The `whale` console script.

    whale                 the app in a terminal, the daily digest otherwise (CI)
    whale app | digest | weekly | watchdog | doctor | settings | test-email | config
    whale preview-email [--theme X] [--out file.html]   email design, offline, no send

Job subcommands hand the remaining arguments to the job's own argparse, so
`whale digest --dry-run` behaves like `python -m whale_agent.jobs.digest_daily --dry-run`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

COMMANDS = (
    "app",
    "digest",
    "weekly",
    "watchdog",
    "doctor",
    "settings",
    "test-email",
    "config",
    "schedule",
    "preview-email",
    "newsletter",
)


def _job(module: str, prog: str, args: list[str]) -> int:
    import importlib

    job = importlib.import_module(module)
    saved = sys.argv
    sys.argv = [prog, *args]
    try:
        result = job.main()
    finally:
        sys.argv = saved
    return int(result or 0)


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def main(argv: list[str] | None = None, is_tty: Callable[[], bool] = _is_tty) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["app"] if is_tty() else ["digest"]
    command, rest = argv[0], argv[1:]
    # Installed from a wheel there is no checkout: settings live in
    # ~/.config/whale-agent/.env unless ./.env exists. Loaded first; real env wins.
    from whale_agent.cli.schedule import default_env_path
    from whale_agent.config import load_env_file as _load_env

    user_env = default_env_path()
    _load_env(str(user_env))
    installed = user_env != Path.cwd() / ".env"

    if command == "app":
        from whale_agent.cli import app

        if installed:
            user_env.parent.mkdir(parents=True, exist_ok=True)
            return app.main(rest, env_path=user_env)
        return app.main(rest)
    if command == "digest":
        return _job("whale_agent.jobs.digest_daily", "whale digest", rest)
    if command == "weekly":
        return _job("whale_agent.jobs.digest_weekly", "whale weekly", rest)
    if command == "watchdog":
        return _job("whale_agent.jobs.watchdog", "whale watchdog", rest)
    if command == "doctor":
        from whale_agent.cli import doctor

        if installed and not any(a == "--env" or a.startswith("--env=") for a in rest):
            rest = ["--env", str(user_env), *rest]
        return doctor.main(rest)
    if command == "settings":
        from whale_agent.cli import app
        from whale_agent.config import load_env_file

        env_path = user_env
        env_path.parent.mkdir(parents=True, exist_ok=True)
        load_env_file(str(env_path))
        from whale_agent.cli import picker

        try:
            if picker.supported():
                sys.stdout.write(picker.ALT_SCREEN_ON)
                try:
                    app.settings_picker(env_path, app.use_color())
                finally:
                    sys.stdout.write(picker.ALT_SCREEN_OFF)
            else:
                app.settings_menu(env_path, input, print, app.use_color())
        except (EOFError, KeyboardInterrupt):
            print("")
        return 0
    if command == "test-email":
        from whale_agent.cli import app
        from whale_agent.config import load_env_file

        load_env_file()
        print(app.send_test_email())
        return 0
    if command == "config":
        return _job("whale_agent.jobs.digest_daily", "whale config", ["--show-config", *rest])
    if command == "schedule":
        from whale_agent.cli import schedule

        return schedule.main(rest)
    if command == "preview-email":
        from whale_agent.config import load_env_file

        load_env_file()
        try:
            print(preview_email(rest))
        except ValueError as exc:
            print(f"Not rendered: {exc}", file=sys.stderr)
            return 2
        return 0
    if command == "newsletter":
        # whale newsletter send [--dry-run] [--cadence daily|weekly]
        return _job("whale_agent.jobs.newsletter", "whale newsletter", rest)
    if command in {"-h", "--help", "help"}:
        print(__doc__.strip())
        return 0
    print(f"Unknown command: {command}", file=sys.stderr)
    print("Try: " + " | ".join(COMMANDS), file=sys.stderr)
    return 2


def render_preview(theme) -> str:
    """The offline demo digest as themed HTML. No network, no real database, no send."""
    from datetime import date

    from whale_agent.config import Settings
    from whale_agent.jobs.digest_daily import _demo_events
    from whale_agent.jobs.pipeline import build_ranked
    from whale_agent.storage.db import Store
    from whale_agent.summarization.render_html import render_digest_html

    on = date(2026, 7, 26)
    store = Store(":memory:")
    try:
        ranked = build_ranked(_demo_events(), store, Settings(), on=on)
    finally:
        store.close()
    return render_digest_html(ranked, on, theme=theme)


def preview_email(args: list[str]) -> str:
    """Write the demo digest in the chosen theme to an HTML file; return the path.

    The egress screen runs on the preview too, so a theme that somehow changed a figure
    shows up here before it reaches a real send."""
    import argparse
    import os

    from whale_agent.delivery.email import find_egress_violations
    from whale_agent.delivery.theme import build_theme, theme_from_env

    ap = argparse.ArgumentParser(prog="whale preview-email")
    ap.add_argument("--theme", default="", help="whale | minimal | dark | newspaper")
    ap.add_argument("--out", default="whale-email-preview.html")
    ns = ap.parse_args(args)
    if ns.theme:
        theme = build_theme(
            ns.theme,
            os.environ.get("WHALE_THEME_ACCENT", ""),
            os.environ.get("WHALE_BRAND_NAME", ""),
            os.environ.get("WHALE_EMAIL_LENGTH", ""),
            os.environ.get("WHALE_EMAIL_SECTIONS", ""),
        )
    else:
        theme = theme_from_env()
    markup = render_preview(theme)
    violations = find_egress_violations(markup)
    if violations:
        raise ValueError("egress screen blocked the preview: " + "; ".join(violations))
    out = Path(ns.out).resolve()
    out.write_text(markup, encoding="utf-8")
    return str(out)


if __name__ == "__main__":
    raise SystemExit(main())
