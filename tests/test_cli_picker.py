import io
from pathlib import Path

from whale_agent.cli import app, picker


def test_decode_arrow_keys():
    assert picker.decode_key("\x1b[A") == picker.UP
    assert picker.decode_key("\x1b[B") == picker.DOWN
    assert picker.decode_key("\r") == picker.ENTER
    assert picker.decode_key("q") == picker.BACK


def test_move_skips_headings_and_wraps():
    selectable = [False, True, True, False, True]
    assert picker.move(0, "", selectable) == 1
    assert picker.move(2, picker.DOWN, selectable) == 4
    assert picker.move(4, picker.DOWN, selectable) == 1
    assert picker.move(1, picker.UP, selectable) == 4
    assert picker.move(1, "3", selectable) == 4


def test_run_picker_returns_row_or_none():
    keys = iter([picker.DOWN, picker.DOWN, picker.ENTER])
    out = io.StringIO()
    got = picker.run_picker(
        lambda i: f"row {i}", [True, True, True], 0, out, lambda: next(keys)
    )
    assert got == 2 and "row 1" in out.getvalue()
    keys = iter([picker.BACK])
    assert (
        picker.run_picker(lambda i: "", [True], 0, io.StringIO(), lambda: next(keys)) is None
    )


def test_home_screen_marks_selected_item():
    text = app.render_home({}, color=False, selected=2, width=120)
    assert "> " + app.MENU[2][1] in text
    assert "> " + app.MENU[0][1] not in text
    assert "Get started" in text and "Whale Agent" in text


def test_settings_screen_fits_every_width():
    import re

    for width in (30, 60, 90, 140):
        text = app.render_settings_screen({}, color=True, selected=1, width=width)
        assert all(len(re.sub(r"\x1b\[[0-9;]*m", "", ln)) <= width for ln in text.split("\n"))


def test_settings_picker_changes_a_choice(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WHALE_LLM_PROVIDER", raising=False)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    env = tmp_path / ".env"
    rows = app.settings_rows()
    target = next(i for i, (_l, s) in enumerate(rows) if s and s.key == "WHALE_LLM_PROVIDER")
    first = next(i for i, (_l, s) in enumerate(rows) if s)
    downs = sum(1 for _l, s in rows[first:target] if s)
    # move to the AI writer row, open it (starts on current: gemini), up to none, pick, leave
    keys = iter([picker.DOWN] * downs + [picker.ENTER, picker.UP, picker.ENTER, picker.BACK])
    app.settings_picker(env, color=False, key_source=lambda: next(keys))
    assert app.read_env_values(env)["WHALE_LLM_PROVIDER"] == "none"
