"""Tests for the macro provider layer.

Two things are being defended. First, parsing: the Treasury fixture is a real response
and FRED's is the documented envelope, and both arrive with every number as a string.
Second, and more important, that no path can produce a macro figure without the date it
belongs to -- a six-week-old reading printed as current is a wrong number.
"""

from __future__ import annotations

import json
from datetime import date

from whale_agent.config import Settings
from whale_agent.enrichment.macro import (
    BANK_CREDIT,
    POLICY_RATE,
    SECTOR_EMPLOYMENT,
    TEN_YEAR_YIELD,
    ChainedMacroProvider,
    FredMacroProvider,
    MacroObservation,
    StaticMacroProvider,
    TreasuryMacroProvider,
    build_macro_provider,
    parse_fred_observations,
    parse_treasury_rates,
)

KEYLESS = Settings()


def test_the_static_provider_answers_only_what_it_was_given():
    provider = StaticMacroProvider({POLICY_RATE: (4.33, date(2026, 7, 20))})
    obs = provider.observe(POLICY_RATE)
    assert obs is not None and obs.value == 4.33 and obs.as_of == date(2026, 7, 20)
    assert provider.observe(BANK_CREDIT) is None
    assert set(provider.snapshot()) == {POLICY_RATE}


def test_an_observation_always_renders_with_its_own_date():
    obs = MacroObservation(
        series=TEN_YEAR_YIELD, value=4.21, as_of=date(2026, 7, 23), units="percent"
    )
    assert obs.describe() == "ten_year_yield 4.21 percent as of 2026-07-23"


def test_treasury_rates_parse_into_the_latest_record_per_security(fixtures_dir):
    raw = json.loads((fixtures_dir / "treasury_avg_interest_rates.json").read_text())
    rates = parse_treasury_rates(raw)
    assert rates["Treasury Bills"] == (3.706, date(2026, 6, 30))
    assert rates["Treasury Notes"] == (3.283, date(2026, 6, 30))


def test_the_treasury_provider_flags_its_rates_as_proxies(fixtures_dir, monkeypatch):
    raw = json.loads((fixtures_dir / "treasury_avg_interest_rates.json").read_text())
    provider = TreasuryMacroProvider(KEYLESS)
    monkeypatch.setattr(provider, "_load", lambda: parse_treasury_rates(raw))

    obs = provider.observe(TEN_YEAR_YIELD)
    assert obs is not None
    assert obs.value == 3.283
    assert obs.as_of == date(2026, 6, 30)
    # Averages across outstanding debt are not a market 10-year yield, and the report
    # has to be able to say so.
    assert obs.is_proxy is True


def test_treasury_returns_nothing_for_series_it_does_not_carry():
    provider = TreasuryMacroProvider(KEYLESS)
    assert provider.observe(BANK_CREDIT) is None
    assert provider.observe(SECTOR_EMPLOYMENT) is None


def test_fred_missing_observations_are_skipped_not_read_as_zero(fixtures_dir):
    raw = json.loads((fixtures_dir / "fred_dgs10_observations.json").read_text())
    parsed = parse_fred_observations(raw)
    # The newest row is FRED's "." placeholder for no data; the newest real one wins.
    assert parsed == (4.21, date(2026, 7, 23))


def test_a_malformed_macro_payload_yields_nothing_rather_than_raising():
    assert parse_fred_observations({"observations": "nope"}) is None
    assert parse_fred_observations(None) is None
    assert parse_treasury_rates({"data": [{"security_desc": "Bills"}]}) == {}


def test_fred_stays_completely_inert_without_a_key():
    provider = FredMacroProvider(KEYLESS)
    assert KEYLESS.fred_enabled is False
    assert provider.observe(POLICY_RATE) is None
    assert provider.snapshot() == {}


def test_fred_switches_on_only_when_the_key_is_present():
    assert Settings(fred_api_key="abc").fred_enabled is True
    assert Settings(fred_api_key="abc", enable_fred=False).fred_enabled is False


def test_the_chain_prefers_the_first_provider_that_knows_a_series():
    preferred = StaticMacroProvider({POLICY_RATE: (4.33, date(2026, 7, 20))})
    fallback = StaticMacroProvider(
        {POLICY_RATE: (3.70, date(2026, 6, 30)), BANK_CREDIT: (18.2, date(2026, 7, 1))}
    )
    chain = ChainedMacroProvider(preferred, fallback)
    assert chain.observe(POLICY_RATE).value == 4.33
    assert chain.observe(BANK_CREDIT).value == 18.2
    assert chain.observe(SECTOR_EMPLOYMENT) is None


def test_a_provider_that_blows_up_is_skipped_rather_than_sinking_the_chain():
    class Broken:
        def observe(self, series):
            raise RuntimeError("upstream is down")

        def snapshot(self):
            return {}

    chain = ChainedMacroProvider(
        Broken(), StaticMacroProvider({POLICY_RATE: (4.33, date(2026, 7, 20))})
    )
    assert chain.observe(POLICY_RATE).value == 4.33


def test_the_default_build_is_treasury_only_until_a_fred_key_exists():
    provider = build_macro_provider(KEYLESS)
    assert isinstance(provider, ChainedMacroProvider)
    assert [type(p) for p in provider.providers] == [TreasuryMacroProvider]


def test_with_everything_switched_off_the_build_still_returns_a_provider():
    off = Settings(enable_treasury=False, enable_fred=False)
    provider = build_macro_provider(off)
    assert isinstance(provider, StaticMacroProvider)
    assert provider.snapshot() == {}
