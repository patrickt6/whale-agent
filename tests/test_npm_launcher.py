"""npm/bin/whale.js picks uvx, then uv, then pipx, else prints the curl installer."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "npm" / "bin" / "whale.js"
node = shutil.which("node")


def _plan(available: list[str]):
    script = (
        f"const {{plan}}=require({json.dumps(str(JS))});"
        f"const a={json.dumps(available)};"
        "console.log(JSON.stringify(plan(['digest','--demo'],c=>a.includes(c))));"
    )
    return json.loads(
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True).stdout
    )


@pytest.mark.skipif(node is None, reason="node not installed")
def test_plan_order():
    assert _plan(["uvx", "uv", "pipx"]) == [
        "uvx",
        ["--from", "whale-agent", "whale", "digest", "--demo"],
    ]
    assert _plan(["pipx"])[0] == "pipx"
    assert _plan([]) is None


@pytest.mark.skipif(node is None, reason="node not installed")
def test_no_runner_prints_curl(tmp_path: Path):
    res = subprocess.run(
        [node, str(JS)], env={"PATH": str(tmp_path)}, capture_output=True, text=True
    )
    assert res.returncode == 1 and "| sh" in res.stderr


def test_package_json_bin():
    data = json.loads((ROOT / "npm" / "package.json").read_text())
    assert data["bin"]["whale"] == "bin/whale.js"
