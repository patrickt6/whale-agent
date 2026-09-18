"""Behaviour when whale-agent runs from a wheel rather than a checkout."""

import plistlib
from pathlib import Path

from whale_agent.cli import schedule

ROOT = Path(__file__).resolve().parents[1]


def test_demo_data_is_package_data():
    from importlib.resources import files

    shipped = files("whale_agent.demo_data").joinpath("form4_purchase.xml").read_text()
    assert shipped == (ROOT / "tests" / "fixtures" / "form4_purchase.xml").read_text()


def test_demo_events_do_not_need_tests_dir():
    from whale_agent.jobs import digest_daily

    assert '"fixtures"' not in Path(digest_daily.__file__).read_text()
    assert digest_daily._demo_events()


def test_pyproject_and_manifest_ship_demo_data():
    assert "demo_data/*.xml" in (ROOT / "pyproject.toml").read_text()
    assert "demo_data" in (ROOT / "MANIFEST.in").read_text()


def test_in_repo_detection(tmp_path: Path):
    assert schedule.in_repo(ROOT)
    assert not schedule.in_repo(tmp_path)


def test_installed_launcher_uses_whale_on_path(monkeypatch):
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/opt/bin/whale")
    assert schedule.launcher(None) == ["/opt/bin/whale"]
    monkeypatch.setattr(schedule.shutil, "which", lambda name: None)
    assert schedule.launcher(None)[1:] == ["-m", "whale_agent.cli.main"]


def test_installed_plist_and_cron_use_user_dirs(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/opt/bin/whale")
    monkeypatch.setattr(schedule.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    job = schedule.jobs_for("daily")[0]
    data = plistlib.loads(schedule.build_plist(job, None, 7, 0).encode())
    assert data["ProgramArguments"] == ["/opt/bin/whale", "digest"]
    assert data["WorkingDirectory"] == str(tmp_path / ".config" / "whale-agent")
    block = schedule.build_cron_block([job], None, 7, 0)
    assert (
        "cd '" + str(tmp_path / ".config" / "whale-agent") + "' && '/opt/bin/whale' digest"
        in block
    )


def test_user_log_dir_per_platform(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert schedule.user_log_dir(tmp_path, "darwin") == tmp_path / "Library/Logs/whale-agent"
    assert schedule.user_log_dir(tmp_path, "linux") == tmp_path / ".local/state/whale-agent"


def test_installed_cron_status_finds_jobs(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/opt/bin/whale")
    block = schedule.build_cron_block(schedule.jobs_for("both"), None, 7, 0)
    runner = lambda args, stdin=None: schedule.Result(0, block)  # noqa: E731
    assert schedule.installed("cron", runner, tmp_path) == ["digest", "weekly"]


def test_default_env_path(tmp_path: Path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    assert schedule.default_env_path(work, home).parent.name == "whale-agent"
    (work / ".env").write_text("A=1\n")
    assert schedule.default_env_path(work, home) == work / ".env"
    assert schedule.default_env_path(ROOT, home) == ROOT / ".env"
