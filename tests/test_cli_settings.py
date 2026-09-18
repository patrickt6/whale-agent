from pathlib import Path

from whale_agent.cli import app
from whale_agent.config import Settings


def _setting(key):
    return next(s for s in app.SETTINGS if s.key == key)


class _Ev:
    def __init__(self, filer_name, ticker=None, filer_id=None):
        self.filer_name, self.ticker, self.filer_id = filer_name, ticker, filer_id


def test_watchlist_and_cadence_from_env(monkeypatch):
    monkeypatch.setenv("WHALE_WATCH_FILERS", "Berkshire, Pershing Square")
    monkeypatch.setenv("WHALE_WATCH_TICKERS", "aapl,msft")
    monkeypatch.setenv("WHALE_CADENCE", "Weekly")
    s = Settings.from_env()
    assert s.watch_filers == ["Berkshire", "Pershing Square"]
    assert s.watch_tickers == ["AAPL", "MSFT"]
    assert s.cadence == "weekly" and s.weekly_enabled and not s.daily_enabled


def test_defaults_keep_current_behaviour(monkeypatch):
    for key in ("WHALE_WATCH_FILERS", "WHALE_WATCH_TICKERS", "WHALE_CADENCE"):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert s.watch_filers == [] and s.watch_tickers == []
    assert s.daily_enabled and s.weekly_enabled
    assert s.on_watchlist(_Ev("Anyone"))


def test_on_watchlist_matching():
    s = Settings(watch_filers=["berkshire"], watch_tickers=["AAPL"])
    assert s.on_watchlist(_Ev("BERKSHIRE HATHAWAY INC"))
    assert s.on_watchlist(_Ev("Someone", ticker="aapl"))
    assert not s.on_watchlist(_Ev("Someone", ticker="MSFT"))
    assert Settings(watch_filers=["0001067983"]).on_watchlist(_Ev("X", filer_id="0001067983"))


def test_schedule_gate_respects_cadence(monkeypatch, capsys, tmp_path):
    from whale_agent.jobs import schedule_gate

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WHALE_CADENCE", "daily")
    schedule_gate.main()
    assert capsys.readouterr().out.strip() == "false"


def test_list_setting_normalize_and_display():
    tickers = _setting("WHALE_WATCH_TICKERS")
    assert app.normalize(tickers, " aapl , msft,, ") == "AAPL,MSFT"
    assert app.display(tickers, "AAPL,MSFT") == "AAPL, MSFT"
    assert app.normalize(_setting("WHALE_CADENCE"), "Daily") == "daily"


def test_every_setting_has_a_known_section():
    assert all(s.section in app.SECTIONS for s in app.SETTINGS)


def test_render_settings_groups_by_section():
    lines = app.render_settings({}, color=False)
    text = "\n".join(lines)
    positions = [text.index(section) for section in app.SECTIONS]
    assert positions == sorted(positions)
    assert "Watchlist: tickers" in text and "\033[" not in text


def test_settings_menu_saves_and_clears_watchlist(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WHALE_WATCH_TICKERS", raising=False)
    env = tmp_path / ".env"
    index = str([s.key for s in app.SETTINGS].index("WHALE_WATCH_TICKERS") + 1)
    answers = iter([index, "nvda, tsla", index, "-", ""])
    out: list[str] = []
    app.settings_menu(env, lambda _p: next(answers), out.append, color=False)
    assert app.read_env_values(env)["WHALE_WATCH_TICKERS"] == ""
    assert any("Saved: Watchlist: tickers = NVDA, TSLA" in line for line in out)


def test_describe_config_shows_cadence_and_watchlist():
    from whale_agent.jobs.digest_daily import describe_config
    from whale_agent.profiles import ResolvedConfig

    # `describe_config` takes a layered ResolvedConfig (the profile layer), so the bare
    # Settings this test cares about is wrapped with no profile and no provenance rows.
    text = describe_config(
        ResolvedConfig(
            settings=Settings(cadence="weekly", watch_tickers=["AAPL"]),
            profile=None,
            fields=(),
        )
    )
    assert "Cadence: weekly" in text and "AAPL" in text


def test_build_ranked_applies_watchlist():
    from datetime import date

    from tests.conftest import make_event
    from whale_agent.jobs.pipeline import build_ranked
    from whale_agent.storage.db import Store

    store = Store(":memory:")
    try:
        events = [
            make_event(filer_name="Jane Example", usd_value=12_400_000.0),
            make_event(filer_name="Someone Else", usd_value=12_400_000.0),
        ]
        on = date(2026, 7, 25)
        watched = build_ranked(events, store, Settings(watch_filers=["example"]), on=on)
        assert [e.filer_name for e in watched] == ["Jane Example"]
    finally:
        store.close()
