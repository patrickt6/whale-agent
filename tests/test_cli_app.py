from pathlib import Path

import pytest

from whale_agent.cli import app


def _setting(key):
    return next(s for s in app.SETTINGS if s.key == key)


def test_update_env_file_keeps_comments_and_other_lines(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("# header\nWHALE_THRESHOLD_USD=5000000\nOTHER=keep  # note\n")
    app.update_env_file(env, {"WHALE_THRESHOLD_USD": "1000000", "NEW_KEY": "a b"})
    assert env.read_text() == (
        '# header\nWHALE_THRESHOLD_USD=1000000\nOTHER=keep  # note\nNEW_KEY="a b"\n'
    )
    assert app.read_env_values(env) == {
        "WHALE_THRESHOLD_USD": "1000000",
        "OTHER": "keep",
        "NEW_KEY": "a b",
    }


def test_update_env_file_creates_file_private(tmp_path: Path):
    env = tmp_path / ".env"
    app.update_env_file(env, {"SMTP_PASSWORD": "abcd efgh ijkl mnop"})
    assert app.read_env_values(env)["SMTP_PASSWORD"] == "abcd efgh ijkl mnop"
    assert env.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "raw,expected",
    [("5M", "5000000"), ("$250,000", "250000"), ("1.5m", "1500000"), ("750k", "750000")],
)
def test_money_normalize(raw, expected):
    assert app.normalize(_setting("WHALE_THRESHOLD_USD"), raw) == expected


@pytest.mark.parametrize("raw", ["abc", "0", "-5M"])
def test_money_rejects_bad_values(raw):
    with pytest.raises(ValueError):
        app.normalize(_setting("WHALE_THRESHOLD_USD"), raw)


def test_choice_and_flag_normalize():
    assert app.normalize(_setting("WHALE_LLM_PROVIDER"), "None") == "none"
    with pytest.raises(ValueError):
        app.normalize(_setting("WHALE_LLM_PROVIDER"), "gpt")
    assert app.normalize(_setting("WHALE_ENABLE_JAPAN"), "off") == "0"


def test_secret_display_never_shows_whole_value():
    shown = app.display(_setting("SMTP_PASSWORD"), "abcdefghijklmnop")
    assert "abcdefghijkl" not in shown and shown.endswith("mnop)")


def test_banner_plain_when_no_color():
    text = app.render_banner({"WHALE_THRESHOLD_USD": "2000000"}, color=False)
    assert "\033[" not in text
    assert "Whale Agent" in text and "by Patrick Taylor" in text
    assert "$2M threshold" in text


def test_whale_pixels_render_in_color_and_plain():
    assert len({len(r) for r in app.WHALE_PIXELS}) == 1
    assert len(app.WHALE_PIXELS) % 2 == 0
    colored = app.render_whale(color=True)
    assert len(colored) == len(app.WHALE_PIXELS) // 2
    assert "\u2580" in "".join(colored) and "38;2;111;160;230" in "".join(colored)
    assert all("\033[" not in r for r in app.render_whale(color=False))


def test_use_color_respects_no_color():
    class Tty:
        def isatty(self):
            return True

    assert app.use_color(Tty(), {}) is True
    assert app.use_color(Tty(), {"NO_COLOR": "1"}) is False


def test_settings_menu_saves_threshold(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WHALE_THRESHOLD_USD", raising=False)
    env = tmp_path / ".env"
    index = [s.key for s in app.SETTINGS].index("WHALE_THRESHOLD_USD") + 1
    answers = iter([str(index), "2M", ""])
    out: list[str] = []
    app.settings_menu(env, lambda _p: next(answers), out.append, color=False)
    assert app.read_env_values(env)["WHALE_THRESHOLD_USD"] == "2000000"
    assert any("Saved: Dollar threshold = $2M" in line for line in out)


def test_main_quits_cleanly(tmp_path: Path):
    out: list[str] = []
    answers = iter(["q"])
    code = app.main(ask=lambda _p: next(answers), say=out.append, env_path=tmp_path / ".env")
    assert code == 0
    assert any("Whale Agent" in line for line in out)


def _visible(line: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", line)


@pytest.mark.parametrize("width", [20, 40, 50, 60, 80, 100, 160])
def test_banner_never_wider_than_terminal(width):
    for color in (True, False):
        text = app.render_banner(
            {"WHALE_EMAIL_TO": "someone.with.a.long.address@example.com"},
            color=color,
            cwd=Path("/very/long/path/" + "x" * 80),
            width=width,
        )
        assert all(len(_visible(line)) <= width for line in text.split("\n"))
        assert "Whale Agent"[: max(width - 3, 1)] in _visible(text)


def test_banner_layout_follows_width():
    wide = app.render_banner({}, color=False, width=160).split("\n")
    assert len(wide) == len(app.WHALE_PIXELS) // 2
    narrow = app.render_banner({}, color=False, width=50).split("\n")
    assert len(narrow) > len(app.WHALE_PIXELS_SMALL) // 2
