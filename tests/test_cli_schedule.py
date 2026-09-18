"""whale schedule: pure builders plus a fake runner. Nothing touches launchctl or crontab."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from whale_agent.cli import schedule
from whale_agent.cli.schedule import Result

ROOT = Path("/repo/whale agent")


class FakeRunner:
    def __init__(self, crontab: str | None = None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.crontab = crontab

    def __call__(self, args, stdin=None):
        args = list(args)
        self.calls.append((args, stdin))
        if args[:2] == ["crontab", "-l"]:
            return Result(0, self.crontab) if self.crontab is not None else Result(1)
        if args[:2] == ["crontab", "-"]:
            self.crontab = stdin
        return Result(0)


def test_parse_time():
    assert schedule.parse_time("7:05") == (7, 5)
    for bad in ("24:00", "7", "07:60", "x"):
        with pytest.raises(ValueError):
            schedule.parse_time(bad)


def test_jobs_follow_cadence():
    assert [j.command for j in schedule.jobs_for("both")] == ["digest", "weekly"]
    assert [j.command for j in schedule.jobs_for("daily")] == ["digest"]
    assert schedule.jobs_for("weekly")[0].weekday == 1


def test_plist_is_valid_and_runs_launcher():
    weekly = schedule.jobs_for("weekly")[0]
    data = plistlib.loads(schedule.build_plist(weekly, ROOT, 6, 30).encode())
    assert data["Label"] == "com.whaleagent.digest.weekly"
    assert data["ProgramArguments"] == [str(ROOT / "whale"), "weekly"]
    assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 30, "Weekday": 1}
    assert data["StandardOutPath"] == str(ROOT / "logs" / "schedule-weekly.log")
    daily = plistlib.loads(
        schedule.build_plist(schedule.jobs_for("daily")[0], ROOT, 7, 0).encode()
    )
    assert "Weekday" not in daily["StartCalendarInterval"]


def test_cron_block_and_strip():
    block = schedule.build_cron_block(schedule.jobs_for("both"), ROOT, 7, 5)
    lines = block.splitlines()
    assert lines[0] == schedule.CRON_BEGIN and lines[-1] == schedule.CRON_END
    assert lines[1].startswith("5 7 * * * '/repo/whale agent/whale' digest >> ")
    assert lines[2].startswith("5 7 * * 1 '/repo/whale agent/whale' weekly")
    other = "0 1 * * * backup\n"
    assert schedule.strip_cron_block(other + block) == other


def test_cron_on_status_off_keeps_other_lines(tmp_path):
    fake = FakeRunner("0 1 * * * backup\n")
    schedule.turn_on("cron", "both", "07:00", fake, tmp_path, root=tmp_path)
    assert "0 1 * * * backup" in fake.crontab and schedule.CRON_BEGIN in fake.crontab
    schedule.turn_on("cron", "daily", "08:00", fake, tmp_path, root=tmp_path)
    assert fake.crontab.count(schedule.CRON_BEGIN) == 1
    assert schedule.installed("cron", fake, tmp_path) == ["digest"]
    schedule.turn_off("cron", fake, tmp_path)
    assert fake.crontab == "0 1 * * * backup\n"
    assert schedule.status("cron", fake, tmp_path) == ["schedule is off"]


def test_launchd_on_off_uses_runner_and_temp_home(tmp_path):
    fake = FakeRunner()
    msgs = schedule.turn_on("launchd", "both", "07:00", fake, tmp_path, root=tmp_path)
    agents = tmp_path / "Library" / "LaunchAgents"
    assert (agents / "com.whaleagent.digest.plist").exists()
    assert (agents / "com.whaleagent.digest.weekly.plist").exists()
    assert any(c[0][:2] == ["launchctl", "bootstrap"] for c in fake.calls)
    assert any("Mondays" in m for m in msgs)
    assert schedule.installed("launchd", fake, tmp_path) == [
        schedule.DAILY_LABEL,
        schedule.WEEKLY_LABEL,
    ]
    schedule.turn_off("launchd", fake, tmp_path)
    assert not list(agents.iterdir())
    assert any(c[0][:2] == ["launchctl", "bootout"] for c in fake.calls)


def test_unsupported_platform_message(tmp_path):
    out: list[str] = []
    code = schedule.main(
        ["status"], runner=FakeRunner(), home=tmp_path, platform="win32", say=out.append
    )
    assert code == 1 and "macOS" in out[0]


def test_bad_time_exits_2(tmp_path):
    out: list[str] = []
    assert (
        schedule.main(
            ["on", "--time", "25:00"],
            runner=FakeRunner(),
            home=tmp_path,
            platform="linux",
            say=out.append,
        )
        == 2
    )


def test_doctor_schedule_check_warns_only():
    from whale_agent.cli import doctor

    off = doctor.check_schedule(lambda: [])
    on = doctor.check_schedule(lambda: ["digest"])
    if schedule.platform_kind() != "unsupported":
        assert not off.ok and not off.blocking
        assert on.ok
