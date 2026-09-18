"""The `--publish` one-command path: deploy then send, so a human never holds the
ordering in their head. No wrangler is actually invoked -- subprocess.run is stubbed.
"""

from __future__ import annotations

import subprocess

import pytest

from whale_agent.jobs.digest_weekly import _publish_to_cloudflare


def test_publish_requires_a_wrangler_project_directory(tmp_path):
    """--publish only knows how to deploy Cloudflare's asset directory (deploy/public
    by default); a caller pointing --articles-dir elsewhere gets a clear refusal
    instead of wrangler silently deploying the wrong thing."""
    articles_dir = tmp_path / "somewhere" / "public"
    articles_dir.mkdir(parents=True)
    with pytest.raises(SystemExit, match="wrangler.toml"):
        _publish_to_cloudflare(articles_dir)


def test_publish_runs_wrangler_deploy_from_the_deploy_directory(tmp_path, monkeypatch):
    deploy_dir = tmp_path / "deploy"
    (deploy_dir / "public").mkdir(parents=True)
    (deploy_dir / "wrangler.toml").write_text('name = "x"\n')

    calls = []

    def fake_run(cmd, cwd=None, check=None):
        calls.append((cmd, cwd, check))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    _publish_to_cloudflare(deploy_dir / "public")

    assert len(calls) == 1
    cmd, cwd, check = calls[0]
    assert cmd == ["npx", "--yes", "wrangler@latest", "deploy"]
    assert cwd == deploy_dir
    assert check is True


def test_publish_aborts_without_sending_when_wrangler_deploy_fails(tmp_path, monkeypatch):
    """A failed deploy must never be followed by a send whose links go nowhere."""
    deploy_dir = tmp_path / "deploy"
    (deploy_dir / "public").mkdir(parents=True)
    (deploy_dir / "wrangler.toml").write_text('name = "x"\n')

    def fake_run(cmd, cwd=None, check=None):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="wrangler deploy failed"):
        _publish_to_cloudflare(deploy_dir / "public")
