import sys

from whale_agent.cli import main as cli


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "_job", lambda module, prog, args: calls.append((module, args)) or 0
    )
    return calls


def test_no_args_without_tty_runs_digest(monkeypatch):
    calls = _capture(monkeypatch)
    assert cli.main([], is_tty=lambda: False) == 0
    assert calls == [("whale_agent.jobs.digest_daily", [])]


def test_no_args_with_tty_opens_app(monkeypatch):
    from whale_agent.cli import app

    seen = []
    monkeypatch.setattr(app, "main", lambda argv=None: seen.append(argv) or 0)
    assert cli.main([], is_tty=lambda: True) == 0
    assert seen == [[]]


def test_job_subcommands_pass_arguments(monkeypatch):
    calls = _capture(monkeypatch)
    cli.main(["digest", "--dry-run"])
    cli.main(["weekly", "--dry-run"])
    cli.main(["watchdog", "--check"])
    cli.main(["config"])
    assert calls == [
        ("whale_agent.jobs.digest_daily", ["--dry-run"]),
        ("whale_agent.jobs.digest_weekly", ["--dry-run"]),
        ("whale_agent.jobs.watchdog", ["--check"]),
        ("whale_agent.jobs.digest_daily", ["--show-config"]),
    ]


def test_job_restores_argv(monkeypatch):
    import types

    fake = types.ModuleType("fake_job")
    seen = []
    fake.main = lambda: seen.append(list(sys.argv))
    monkeypatch.setitem(sys.modules, "fake_job", fake)
    before = list(sys.argv)
    assert cli._job("fake_job", "whale x", ["--a"]) == 0
    assert seen == [["whale x", "--a"]] and sys.argv == before


def test_doctor_and_unknown(monkeypatch, capsys):
    from whale_agent.cli import doctor

    monkeypatch.setattr(doctor, "main", lambda argv=None: 7 if argv == ["--live"] else 0)
    assert cli.main(["doctor", "--live"]) == 7
    assert cli.main(["nope"]) == 2
    assert "Unknown command" in capsys.readouterr().err


def test_console_script_declared():
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    scripts = data["project"]["scripts"]
    assert scripts["whale"] == "whale_agent.cli.main:main"
    assert {"whale-digest", "whale-weekly", "whale-watchdog"} <= set(scripts)
