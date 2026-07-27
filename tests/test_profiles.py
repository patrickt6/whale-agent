"""The profile layer: loading, precedence, and the four shipped profiles.

The one claim that matters most: the default profile changes nothing. Everything else
here is secondary to proving that a stranger who never touches profiles at all gets
exactly today's behaviour.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from whale_agent.config import Settings
from whale_agent.ingestion.registry import UnknownSourceError
from whale_agent.profiles import (
    PROFILES_DIR,
    ProfileError,
    apply_profile,
    load_profile,
    resolve_config,
    resolve_profile_path,
)

# -- default profile round-trips to plain from_env() --------------------------------


def test_default_profile_matches_plain_from_env():
    """The proof the task demands: loading `default.toml` must be a no-op."""
    plain = Settings.from_env()
    via_profile = resolve_config("default").settings
    assert via_profile == plain


def test_no_profile_at_all_matches_plain_from_env():
    assert resolve_config(None).settings == Settings.from_env()


# -- resolving a name or a path -------------------------------------------------------


def test_resolve_bare_name_finds_repo_profile():
    path = resolve_profile_path("default")
    assert path == PROFILES_DIR / "default.toml"
    assert path.is_file()


def test_resolve_path_like_argument(tmp_path):
    custom = tmp_path / "mine.toml"
    custom.write_text("threshold_usd = 42.0\n")
    assert resolve_profile_path(str(custom)) == custom


def test_resolve_unknown_bare_name_lists_available():
    with pytest.raises(ProfileError, match="no profile named"):
        resolve_profile_path("does-not-exist")


def test_resolve_missing_path_is_an_error(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        resolve_profile_path(str(tmp_path / "nope.toml"))


# -- unknown keys are a hard error, with a suggestion ---------------------------------


def test_unknown_top_level_key_rejected_with_suggestion(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("threshhold_usd = 1000000.0\n")
    with pytest.raises(ProfileError, match="threshold_usd"):
        load_profile(str(bad))


def test_unknown_filters_key_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[filters]\njuridictions = ["US"]\n')
    with pytest.raises(ProfileError, match="jurisdictions"):
        load_profile(str(bad))


def test_unknown_sources_key_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[sources]\nenable = ["sec_form4"]\n')
    with pytest.raises(ProfileError, match="enabled"):
        load_profile(str(bad))


def test_wrong_type_is_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('threshold_usd = "five million"\n')
    with pytest.raises(ProfileError, match="must be a number"):
        load_profile(str(bad))


def test_invalid_toml_is_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not [ valid toml\n")
    with pytest.raises(ProfileError, match="invalid TOML"):
        load_profile(str(bad))


def test_unknown_source_name_in_profile_is_hard_error(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[sources]\nenabled = ["sec_form_4"]\n')  # typo: underscore before 4
    with pytest.raises(UnknownSourceError, match="sec_form4"):
        apply_profile(Settings(), load_profile(str(bad)))


# -- precedence: defaults -> profile -> environment -> CLI ---------------------------


def test_profile_raises_the_floor_above_defaults():
    settings = resolve_config("us-insiders").settings
    assert settings.threshold_usd == 1_000_000.0
    assert settings.sources == ["sec_form4"]


def test_environment_overrides_profile(monkeypatch):
    monkeypatch.setenv("WHALE_THRESHOLD_USD", "42.0")
    settings = resolve_config("us-insiders").settings
    assert settings.threshold_usd == 42.0  # env wins over the profile's 1,000,000


def test_environment_unset_leaves_profile_value_in_place():
    settings = resolve_config("us-insiders").settings
    assert settings.threshold_usd == 1_000_000.0  # unset env: profile floor stands


def test_cli_overrides_win_over_everything():
    resolved = resolve_config("us-insiders", cli_overrides={"threshold_usd": 7.0})
    assert resolved.settings.threshold_usd == 7.0
    row = next(f for f in resolved.fields if f.field == "threshold_usd")
    assert row.layer == "cli"


def test_provenance_reports_default_when_nothing_set():
    resolved = resolve_config(None)
    row = next(f for f in resolved.fields if f.field == "threshold_usd")
    assert row.layer == "default"


def test_provenance_reports_environment_layer(monkeypatch):
    monkeypatch.setenv("WHALE_THRESHOLD_USD", "9.0")
    resolved = resolve_config(None)
    row = next(f for f in resolved.fields if f.field == "threshold_usd")
    assert row.layer == "environment"
    assert row.value == 9.0


def test_profile_does_not_affect_untracked_fields():
    """A profile only ever moves the fields it is schema'd to move."""
    settings = resolve_config("us-insiders").settings
    assert settings.sec_user_agent == Settings().sec_user_agent
    assert settings.email_provider == Settings().email_provider


# -- apply_profile composes with dataclasses.replace, not just from_env --------------


def test_apply_profile_is_a_plain_settings_transform():
    base = Settings()
    profile = load_profile("crypto")
    updated = apply_profile(base, profile)
    assert updated.filter_jurisdictions == ["CRYPTO"]
    assert updated.threshold_usd == 10_000_000.0
    # every untouched field is unchanged
    assert updated.db_path == base.db_path


def test_settings_from_env_base_layering_is_symmetric():
    """`Settings.from_env(base=...)` with no env vars set is the identity on `base`."""
    base = replace(Settings(), threshold_usd=123.0, sources=["sec_form4"])
    assert Settings.from_env(base=base) == base


# -- the four shipped profiles parse and produce sane settings -----------------------


@pytest.mark.parametrize(
    "name,expected_threshold,expected_sources",
    [
        ("default", 5_000_000.0, []),
        ("us-insiders", 1_000_000.0, ["sec_form4"]),
        ("activist", 500_000.0, ["sec_13dg"]),
        ("crypto", 10_000_000.0, ["arkham", "whale_alert"]),
    ],
)
def test_shipped_profile_parses_and_applies(name, expected_threshold, expected_sources):
    settings = resolve_config(name).settings
    assert settings.threshold_usd == expected_threshold
    assert settings.sources == expected_sources


def test_all_shipped_profiles_are_discoverable():
    names = {p.stem for p in PROFILES_DIR.glob("*.toml")}
    assert names == {"default", "us-insiders", "activist", "crypto"}
