"""Email theme layer (WA-14 / #26): presets, overrides, safety and figure invariance."""

from __future__ import annotations

import pytest

from whale_agent.cli import app
from whale_agent.cli import main as cli
from whale_agent.config import Settings
from whale_agent.delivery import email as email_mod
from whale_agent.delivery.base import digest_to_html
from whale_agent.delivery.email import figures_in, send_digest_email
from whale_agent.delivery.theme import (
    PRESETS,
    WHALE_SANS,
    WHALE_SERIF,
    Theme,
    build_theme,
    theme_from_env,
    validate_colour,
)
from whale_agent.summarization import render_html as rh
from whale_agent.summarization.render_html import html_to_text

GMAIL_CLIP_BYTES = 102 * 1024


def test_whale_palette_literals_match_renderer():
    # The renderer's constants are the placeholders `Theme.restyle` maps from.
    from whale_agent.delivery.theme import _WHALE

    assert (
        _WHALE["ink"],
        _WHALE["paper"],
        _WHALE["page"],
        _WHALE["navy"],
        _WHALE["ink_soft"],
        _WHALE["ink_faint"],
        _WHALE["rule"],
        _WHALE["rule_strong"],
        _WHALE["caveat"],
    ) == (
        rh.INK,
        rh.PAPER,
        rh.PAGE,
        rh.NAVY,
        rh.INK_SOFT,
        rh.INK_FAINT,
        rh.RULE,
        rh.RULE_STRONG,
        rh.CAVEAT,
    )
    assert (WHALE_SANS, WHALE_SERIF) == (rh.SANS, rh.SERIF)


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_renders_under_gmail_clip(name):
    markup = cli.render_preview(PRESETS[name])
    assert markup.startswith("<!doctype html>")
    assert len(markup.encode("utf-8")) < GMAIL_CLIP_BYTES


def test_whale_preset_is_todays_look():
    unthemed = cli.render_preview(None)
    assert cli.render_preview(PRESETS["whale"]) == unthemed


def test_figures_identical_across_themes():
    seen = {
        name: figures_in(html_to_text(cli.render_preview(t))) for name, t in PRESETS.items()
    }
    assert seen["whale"], "demo digest must carry at least one figure"
    assert all(v == seen["whale"] for v in seen.values())


def test_short_length_keeps_row_amounts():
    detailed = figures_in(html_to_text(cli.render_preview(PRESETS["whale"])))
    short = cli.render_preview(build_theme("whale", length="short"))
    # Every amount stays; only figures quoted inside the hidden prose go with it.
    kept = figures_in(html_to_text(short))
    assert kept and all(f in detailed for f in kept)
    assert detailed[0] in kept
    assert "Why it matters:" not in short


def test_theme_colours_reach_markup():
    markup = cli.render_preview(PRESETS["dark"])
    assert "#1C2024" in markup and rh.PAPER not in markup
    assert "#ABCDEF" in cli.render_preview(build_theme("whale", accent="#abcdef"))


@pytest.mark.parametrize(
    "bad", ["red", "#12345", "#1234567", "rgb(0,0,0)", "#fff;background:url(x)", ""]
)
def test_invalid_colour_rejected(bad):
    with pytest.raises(ValueError):
        validate_colour(bad)
    with pytest.raises(ValueError):
        Theme(accent=bad)


def test_invalid_env_colour_falls_back_to_whale():
    assert (
        theme_from_env({"WHALE_THEME": "dark", "WHALE_THEME_ACCENT": "blue"})
        == PRESETS["whale"]
    )
    assert theme_from_env({"WHALE_THEME": "dark"}) == PRESETS["dark"]


def test_brand_name_is_escaped():
    markup = cli.render_preview(build_theme(brand_name='<script>x</script>"'))
    assert "<script>x" not in markup
    assert "&lt;script&gt;x&lt;/script&gt;" in markup


def test_logo_must_be_https():
    with pytest.raises(ValueError):
        Theme(logo_url="javascript:alert(1)")
    markup = cli.render_preview(Theme(logo_url="https://example.com/logo.png"))
    assert 'src="https://example.com/logo.png"' in markup


def test_sections_order_and_hiding():
    t = build_theme(sections="footer,masthead")
    assert t.sections == ("footer", "masthead", "events")  # events cannot be hidden
    markup = cli.render_preview(t)
    assert markup.index("On provenance") < markup.index("Disclosed positions above")
    with pytest.raises(ValueError):
        build_theme(sections="masthead,ads")


def test_egress_screening_runs_for_every_theme(monkeypatch):
    calls = []
    real = email_mod.find_egress_violations

    def spy(*bodies, **kw):
        calls.append(bodies)
        return real(*bodies, **kw)

    sent = []
    monkeypatch.setattr(email_mod, "find_egress_violations", spy)
    monkeypatch.setattr(email_mod, "send_via_smtp", lambda *a, **k: sent.append(a) or "ok")
    monkeypatch.setattr(email_mod, "record_quarantine", lambda *a, **k: None)
    settings = Settings(
        email_provider="smtp",
        email_to="a@example.com",
        smtp_username="u@example.com",
        smtp_password="x" * 16,
    )
    for theme in PRESETS.values():
        markup = cli.render_preview(theme)
        send_digest_email("digest", settings, subject="s", html=markup)
        assert calls[-1][2] == markup
        # An out-of-range figure is still blocked whatever the theme.
        bad = markup.replace("</body>", "<p>$9000000000000T</p></body>")
        result = send_digest_email("digest", settings, subject="s", html=bad)
        assert not result.ok and "egress blocked" in result.detail
    assert len(sent) == len(PRESETS)


def test_fallback_text_render_reads_theme():
    out = digest_to_html("Title\nline", theme=PRESETS["dark"])
    assert "#1C2024" in out and "background:#ffffff" not in out


def test_preview_email_cli_writes_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("whale_agent.config.load_env_file", lambda *a, **k: 0)
    out = tmp_path / "p.html"
    assert cli.main(["preview-email", "--theme", "newspaper", "--out", str(out)]) == 0
    assert str(out) in capsys.readouterr().out
    assert out.read_text().startswith("<!doctype html>")
    assert cli.main(["preview-email", "--theme", "nope", "--out", str(out)]) == 2


def test_settings_and_menu_rows():
    keys = {s.key for s in app.SETTINGS if s.section == "Email design"}
    assert keys == {
        "WHALE_THEME",
        "WHALE_THEME_ACCENT",
        "WHALE_BRAND_NAME",
        "WHALE_EMAIL_LENGTH",
        "WHALE_EMAIL_SECTIONS",
    }
    assert app.SECTIONS[-1] == "Email design"
    assert app.MENU[-2] == ("9", "Preview email design") and app.MENU[-1][0] == "q"
    accent = next(s for s in app.SETTINGS if s.key == "WHALE_THEME_ACCENT")
    assert app.normalize(accent, "#4A6F93") == "#4a6f93"
    with pytest.raises(ValueError):
        app.normalize(accent, "blue")
