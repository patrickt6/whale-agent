import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"


def test_install_script_parses():
    subprocess.run(["sh", "-n", str(INSTALL)], check=True)
    if shutil.which("dash"):
        subprocess.run(["dash", "-n", str(INSTALL)], check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_install_clones_and_writes_blank_env(tmp_path: Path):
    home = tmp_path / "whale-home"
    env = {
        **os.environ,
        "WHALE_HOME": str(home),
        "WHALE_REPO_URL": ROOT.as_uri(),
        "WHALE_SKIP_PIP": "1",
        "HOME": str(tmp_path),
        "WHALE_INSTALL_MODE": "source",
    }
    first = subprocess.run(["sh", str(INSTALL)], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert (home / ".git").is_dir() and (home / ".venv" / "bin" / "python").exists()
    assert f"cd {home} && ./whale" in first.stdout

    written = (home / ".env").read_text()
    assert oct((home / ".env").stat().st_mode & 0o777) == "0o600"
    for line in written.splitlines():
        if line and not line.startswith("#"):
            value = line.partition("=")[2].split("  #", 1)[0]
            assert "@" not in value, line
    assert "WHALE_EMAIL_TO=" in written

    (home / ".env").write_text("KEEP=1\n")
    second = subprocess.run(["sh", str(INSTALL)], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert (home / ".env").read_text() == "KEEP=1\n"


FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$UV_LOG"
if [ "$1 $2" = "tool list" ]; then
    [ -f "$UV_STATE" ] && echo "whale-agent v0.1.0" && echo "- whale"
    exit 0
fi
if [ "$1 $2" = "tool install" ]; then touch "$UV_STATE"; fi
if [ "$1 $2" = "tool dir" ]; then echo "$HOME/.local/bin"; fi
exit 0
"""


def _fake_uv_env(tmp_path: Path, **extra) -> dict:
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    uv = stub_dir / "uv"
    uv.write_text(FAKE_UV)
    uv.chmod(0o755)
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "UV_LOG": str(tmp_path / "uv.log"),
        "UV_STATE": str(tmp_path / "installed"),
        **extra,
    }


def test_tool_install_uses_uv_then_upgrades_on_rerun(tmp_path: Path):
    env = _fake_uv_env(tmp_path)
    first = subprocess.run(["sh", str(INSTALL)], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "Run: whale" in first.stdout
    assert "export PATH=" in first.stdout  # the stub bin dir is not on PATH
    second = subprocess.run(["sh", str(INSTALL)], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    calls = (tmp_path / "uv.log").read_text().splitlines()
    assert "tool install whale-agent" in calls
    assert "tool upgrade whale-agent" in calls
    assert calls.count("tool install whale-agent") == 1
    assert not (tmp_path / "home" / "whale-agent").exists()  # no clone in tool mode


def test_tool_install_source_override(tmp_path: Path):
    src = "git+https://github.com/patrickt6/whale-agent.git"
    env = _fake_uv_env(tmp_path, WHALE_INSTALL_SOURCE=src)
    res = subprocess.run(["sh", str(INSTALL)], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert f"tool install {src}" in (tmp_path / "uv.log").read_text()


def test_bad_mode_fails(tmp_path: Path):
    env = _fake_uv_env(tmp_path, WHALE_INSTALL_MODE="nope")
    res = subprocess.run(["sh", str(INSTALL)], env=env, capture_output=True, text=True)
    assert res.returncode != 0 and "WHALE_INSTALL_MODE" in res.stderr
