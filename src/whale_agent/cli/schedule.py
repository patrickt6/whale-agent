"""`whale schedule on|off|status [--time HH:MM]`: send the briefs automatically.

macOS uses launchd (a plist per job in ~/Library/LaunchAgents), Linux uses a marked
block in the user crontab. The text builders are pure functions; every call to
launchctl or crontab goes through `runner`, which tests replace.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

REPO_ROOT = Path(__file__).resolve().parents[3]


def in_repo(root: Path) -> bool:
    """True for a source checkout (the `./whale` launcher is there), False for a wheel.

    From an installed wheel `parents[3]` is some site-packages ancestor, which has no
    `whale` launcher next to a `pyproject.toml`."""
    return (root / "whale").is_file() and (root / "pyproject.toml").is_file()


def user_config_dir(home: Path | None = None) -> Path:
    """~/.config/whale-agent (XDG_CONFIG_HOME honoured). Holds .env and whale.db when
    the package is installed rather than cloned."""
    home = home or Path.home()
    base = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(base) / "whale-agent"


def user_log_dir(home: Path | None = None, platform: str | None = None) -> Path:
    """Where scheduled runs log when installed: ~/Library/Logs on macOS, the XDG state
    dir elsewhere. Stdlib only, no platformdirs."""
    home = home or Path.home()
    if (platform or sys.platform) == "darwin":
        return home / "Library" / "Logs" / "whale-agent"
    base = os.environ.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / "whale-agent"


def default_env_path(cwd: Path | None = None, home: Path | None = None) -> Path:
    """The .env to read and write: ./.env if it exists or this is a checkout, else
    ~/.config/whale-agent/.env."""
    cwd = cwd or Path.cwd()
    if (cwd / ".env").is_file() or in_repo(cwd):
        return cwd / ".env"
    return user_config_dir(home) / ".env"


# The checkout this module runs from, or None when it was installed from a wheel. A
# `root` of None anywhere below means "installed": no launcher script, no repo logs.
INSTALL_ROOT: Path | None = REPO_ROOT if in_repo(REPO_ROOT) else None


def launcher(root: Path | None) -> list[str]:
    """The argv prefix a scheduled job runs: the repo launcher, else the installed
    `whale` console script, else this interpreter with `-m whale_agent.cli.main`."""
    if root is not None:
        return [str(root / "whale")]
    found = shutil.which("whale")
    if found:
        return [found]
    return [sys.executable, "-m", "whale_agent.cli.main"]


def work_dir(root: Path | None) -> Path:
    return root if root is not None else user_config_dir()


DAILY_LABEL = "com.whaleagent.digest"
WEEKLY_LABEL = "com.whaleagent.digest.weekly"
CRON_BEGIN = "# BEGIN whale-agent schedule"
CRON_END = "# END whale-agent schedule"
DEFAULT_TIME = "07:00"

Runner = Callable[[Sequence[str], str | None], "Result"]


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class Job:
    label: str
    command: str  # "digest" or "weekly"
    weekday: int | None  # None every day; 1 is Monday (launchd and cron agree)


def run_command(args: Sequence[str], stdin: str | None = None) -> Result:
    proc = subprocess.run(list(args), input=stdin, capture_output=True, text=True, check=False)
    return Result(proc.returncode, proc.stdout, proc.stderr)


def parse_time(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"time must be HH:MM (24 hour), not {text!r}")
    return int(m.group(1)), int(m.group(2))


def jobs_for(cadence: str) -> list[Job]:
    jobs = []
    if cadence in {"daily", "both"}:
        jobs.append(Job(DAILY_LABEL, "digest", None))
    if cadence in {"weekly", "both"}:
        jobs.append(Job(WEEKLY_LABEL, "weekly", 1))
    return jobs


def log_dir(root: Path | None) -> Path:
    return root / "logs" if root is not None else user_log_dir()


def log_path(root: Path | None, job: Job) -> Path:
    return log_dir(root) / f"schedule-{job.command}.log"


def build_plist(job: Job, root: Path | None, hour: int, minute: int) -> str:
    interval = (
        f"        <key>Hour</key><integer>{hour}</integer>\n"
        f"        <key>Minute</key><integer>{minute}</integer>\n"
    )
    if job.weekday is not None:
        interval += f"        <key>Weekday</key><integer>{job.weekday}</integer>\n"
    log = escape(str(log_path(root, job)))
    program = "".join(f"        <string>{escape(a)}</string>\n" for a in launcher(root))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"    <key>Label</key><string>{job.label}</string>\n"
        "    <key>ProgramArguments</key>\n    <array>\n"
        f"{program}"
        f"        <string>{job.command}</string>\n"
        "    </array>\n"
        f"    <key>WorkingDirectory</key><string>{escape(str(work_dir(root)))}</string>\n"
        "    <key>StartCalendarInterval</key>\n    <dict>\n"
        f"{interval}"
        "    </dict>\n"
        f"    <key>StandardOutPath</key><string>{log}</string>\n"
        f"    <key>StandardErrorPath</key><string>{log}</string>\n"
        "</dict>\n</plist>\n"
    )


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def build_cron_block(jobs: Sequence[Job], root: Path | None, hour: int, minute: int) -> str:
    lines = [CRON_BEGIN]
    cmd = " ".join(_quote(Path(a)) for a in launcher(root))
    if root is None:
        cmd = f"cd {_quote(work_dir(root))} && {cmd}"
    for job in jobs:
        dow = "*" if job.weekday is None else str(job.weekday)
        lines.append(
            f"{minute} {hour} * * {dow} {cmd} {job.command} "
            f">> {_quote(log_path(root, job))} 2>&1"
        )
    lines.append(CRON_END)
    return "\n".join(lines) + "\n"


def strip_cron_block(crontab: str) -> str:
    out, inside = [], False
    for line in crontab.splitlines():
        if line.strip() == CRON_BEGIN:
            inside = True
        elif line.strip() == CRON_END:
            inside = False
        elif not inside:
            out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def platform_kind(platform: str | None = None) -> str:
    platform = platform or sys.platform
    if platform == "darwin":
        return "launchd"
    if platform.startswith("linux"):
        return "cron"
    return "unsupported"


def _agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def _read_crontab(runner: Runner) -> str:
    res = runner(["crontab", "-l"], None)
    return res.stdout if res.returncode == 0 else ""  # no crontab yet exits 1


def turn_off(kind: str, runner: Runner, home: Path) -> list[str]:
    if kind == "launchd":
        msgs = []
        for label in (DAILY_LABEL, WEEKLY_LABEL):
            plist = _agents_dir(home) / f"{label}.plist"
            runner(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], None)
            if plist.exists():
                plist.unlink()
                msgs.append(f"removed {label}")
        return msgs or ["no schedule was installed"]
    if kind == "cron":
        current = _read_crontab(runner)
        if CRON_BEGIN not in current:
            return ["no schedule was installed"]
        runner(["crontab", "-"], strip_cron_block(current))
        return ["removed the whale-agent block from your crontab"]
    return [UNSUPPORTED]


UNSUPPORTED = (
    "automatic scheduling works on macOS (launchd) and Linux (cron) only; "
    "on this system run `whale digest` from your own scheduler"
)


def turn_on(
    kind: str,
    cadence: str,
    time: str,
    runner: Runner,
    home: Path,
    root: Path | None = INSTALL_ROOT,
) -> list[str]:
    hour, minute = parse_time(time)
    jobs = jobs_for(cadence)
    if kind == "unsupported":
        return [UNSUPPORTED]
    if not jobs:
        return [f"cadence {cadence!r} sends nothing; set WHALE_CADENCE first"]
    turn_off(kind, runner, home)
    log_dir(root).mkdir(parents=True, exist_ok=True)
    if root is None:
        work_dir(root).mkdir(parents=True, exist_ok=True)
    msgs = []
    if kind == "launchd":
        _agents_dir(home).mkdir(parents=True, exist_ok=True)
        for job in jobs:
            plist = _agents_dir(home) / f"{job.label}.plist"
            plist.write_text(build_plist(job, root, hour, minute), encoding="utf-8")
            res = runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], None)
            if res.returncode != 0:
                msgs.append(f"launchctl could not load {job.label}: {res.stderr.strip()}")
    else:
        current = strip_cron_block(_read_crontab(runner))
        res = runner(["crontab", "-"], current + build_cron_block(jobs, root, hour, minute))
        if res.returncode != 0:
            msgs.append(f"crontab update failed: {res.stderr.strip()}")
    for job in jobs:
        when = "Mondays" if job.weekday == 1 else "every day"
        msgs.append(
            f"whale {job.command}: {when} at {hour:02d}:{minute:02d}, "
            f"log {log_path(root, job)}"
        )
    return msgs


def installed(kind: str, runner: Runner, home: Path) -> list[str]:
    """Names of the installed jobs, read only (no launchctl call)."""
    if kind == "launchd":
        return [
            label
            for label in (DAILY_LABEL, WEEKLY_LABEL)
            if (_agents_dir(home) / f"{label}.plist").exists()
        ]
    if kind == "cron":
        text = _read_crontab(runner)
        if CRON_BEGIN not in text:
            return []
        block = text.split(CRON_BEGIN, 1)[1].split(CRON_END, 1)[0]
        return re.findall(r"(?:/whale'?|whale_agent\.cli\.main'?) (\w+)", block)
    return []


def status(kind: str, runner: Runner, home: Path) -> list[str]:
    if kind == "unsupported":
        return [UNSUPPORTED]
    found = installed(kind, runner, home)
    if not found:
        return ["schedule is off"]
    return [f"schedule is on ({kind}): " + ", ".join(found)]


def main(
    argv: list[str] | None = None,
    runner: Runner = run_command,
    home: Path | None = None,
    platform: str | None = None,
    say: Callable[[str], None] = print,
) -> int:
    import argparse

    from whale_agent.config import Settings, load_env_file

    ap = argparse.ArgumentParser(
        prog="whale schedule", description="send the briefs automatically"
    )
    ap.add_argument("action", choices=["on", "off", "status"])
    ap.add_argument("--time", default=DEFAULT_TIME, help="HH:MM, 24 hour, local time")
    args = ap.parse_args(argv)
    home = home or Path.home()
    kind = platform_kind(platform)
    try:
        if args.action == "on":
            load_env_file(str(default_env_path()))
            msgs = turn_on(kind, Settings.from_env().cadence, args.time, runner, home)
        elif args.action == "off":
            msgs = turn_off(kind, runner, home)
        else:
            msgs = status(kind, runner, home)
    except ValueError as exc:
        say(f"  {exc}")
        return 2
    for m in msgs:
        say("  " + m)
    return 1 if kind == "unsupported" else 0


if __name__ == "__main__":
    raise SystemExit(main())
