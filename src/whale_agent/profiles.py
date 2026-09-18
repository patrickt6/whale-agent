"""Named operator profiles: readable TOML files a stranger can copy instead of a wall of
`export`s.

Precedence, lowest to highest: dataclass defaults -> profile TOML file -> environment
variables -> CLI flags. A profile only ever raises the floor; an environment variable set
by the deploying operator (or the test suite, which never sets a profile at all) always
wins, so every existing single-operator deployment is unaffected by this module's
existence. See `Settings.from_env(base=...)` for how that layering actually happens: a
profile is applied to plain dataclass defaults first, and the result is passed in as
`base` so `from_env` only overrides what an environment variable actually sets.

Unknown keys are a hard error, on purpose. A typo like `threshhold_usd` that silently
left the $5M default in place would be a config that appears to apply but does not --
exactly the failure this project's error-handling philosophy exists to catch elsewhere
(`errors.py`'s categorized failures, the provenance gate in `summarization/provenance.py`).
A profile file gets the same treatment.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from whale_agent.config import Settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"

# The only top-level keys a profile file may set, and the only keys allowed inside each
# of its two tables. Kept as plain dicts (name -> one-line purpose) rather than a bigger
# schema library so the "what is valid here" question has exactly one place to look, and
# so the unknown-key error can name a peer to suggest without importing anything heavier.
_TOP_LEVEL_KEYS = {
    "name": "a short label for --show-config to report",
    "description": "a longer note; never applied to Settings, documentation only",
    "threshold_usd": "the $ gate for daily digest inclusion",
    "near_threshold_usd": "the 'almost made it' note threshold",
    "instant_usd": "the Tier-1 instant-alert $ gate",
    "sources": "table: which registered sources run",
    "filters": "table: which events, once collected, survive to the digest",
}
_SOURCES_KEYS = {"enabled": "list of registered source names to run (see registry.py)"}
_FILTERS_KEYS = {
    "jurisdictions": "keep only these Jurisdiction codes (e.g. 'US', 'TW')",
    "event_types": "keep only these TransactionType values (e.g. 'activist_13d')",
    "filer_roles": "keep only these filer_role values (e.g. 'Officer', 'Director')",
    "exclude_filer_roles": "drop these filer_role values even if filer_roles allows them",
}


class ProfileError(ValueError):
    """A profile file is malformed: unknown key, wrong type, or unresolvable path."""


def _reject_unknown_keys(data: dict, allowed: dict, section: str) -> None:
    for key in data:
        if key not in allowed:
            where = f"[{section}]" if section else "top level"
            suggestion = get_close_matches(key, list(allowed), n=1)
            msg = f"unknown profile key {key!r} in {where}."
            if suggestion:
                msg += f" Did you mean {suggestion[0]!r}?"
            msg += f" Valid keys here: {', '.join(sorted(allowed))}"
            raise ProfileError(msg)


def _as_str_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProfileError(f"{key!r} must be a list of strings, got {value!r}")
    return list(value)


def _as_float(value: Any, key: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ProfileError(f"{key!r} must be a number, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class ProfileFilters:
    jurisdictions: list[str]
    event_types: list[str]
    filer_roles: list[str]
    exclude_filer_roles: list[str]


@dataclass(frozen=True)
class Profile:
    """A validated profile: only the fields it actually sets are non-None."""

    origin: str  # the resolved path, for provenance reporting
    name: str | None = None
    description: str | None = None
    threshold_usd: float | None = None
    near_threshold_usd: float | None = None
    instant_usd: float | None = None
    sources_enabled: list[str] | None = None
    filters: ProfileFilters | None = None

    def set_fields(self) -> list[str]:
        """Which Settings-affecting keys this profile actually supplied, for provenance."""
        fields = []
        if self.threshold_usd is not None:
            fields.append("threshold_usd")
        if self.near_threshold_usd is not None:
            fields.append("near_threshold_usd")
        if self.instant_usd is not None:
            fields.append("instant_usd")
        if self.sources_enabled is not None:
            fields.append("sources")
        if self.filters is not None:
            f = self.filters
            if f.jurisdictions:
                fields.append("filters.jurisdictions")
            if f.event_types:
                fields.append("filters.event_types")
            if f.filer_roles:
                fields.append("filters.filer_roles")
            if f.exclude_filer_roles:
                fields.append("filters.exclude_filer_roles")
        return fields


def resolve_profile_path(name_or_path: str) -> Path:
    """`--profile us-insiders` -> `profiles/us-insiders.toml`; a path-shaped argument
    (contains `/` or ends in `.toml`) resolves as a path instead.

    A bare name (no path separator, no `.toml` suffix requirement) is looked up in the
    repo's `profiles/` directory. Anything containing a `/` or `\\`, or ending in
    `.toml`, is treated as a path -- relative to the current working directory, same as
    every other path-shaped CLI argument in this project.
    """
    looks_like_path = (
        "/" in name_or_path or "\\" in name_or_path or name_or_path.endswith(".toml")
    )
    path = Path(name_or_path) if looks_like_path else PROFILES_DIR / f"{name_or_path}.toml"
    if not path.is_file():
        if looks_like_path:
            raise ProfileError(f"profile file not found: {path}")
        available = (
            sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))
            if PROFILES_DIR.is_dir()
            else []
        )
        raise ProfileError(
            f"no profile named {name_or_path!r} in {PROFILES_DIR}. "
            f"Available: {', '.join(available) or '(none)'}"
        )
    return path


def load_profile(name_or_path: str) -> Profile:
    """Resolve, parse, and validate a profile file. Raises `ProfileError` on any problem."""
    path = resolve_profile_path(name_or_path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{path}: invalid TOML: {exc}") from exc

    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "")

    sources_enabled: list[str] | None = None
    if "sources" in data:
        sources_data = data["sources"]
        if not isinstance(sources_data, dict):
            raise ProfileError("[sources] must be a table")
        _reject_unknown_keys(sources_data, _SOURCES_KEYS, "sources")
        if "enabled" in sources_data:
            sources_enabled = _as_str_list(sources_data["enabled"], "sources.enabled")

    filters: ProfileFilters | None = None
    if "filters" in data:
        filters_data = data["filters"]
        if not isinstance(filters_data, dict):
            raise ProfileError("[filters] must be a table")
        _reject_unknown_keys(filters_data, _FILTERS_KEYS, "filters")
        filters = ProfileFilters(
            jurisdictions=_as_str_list(
                filters_data.get("jurisdictions", []), "filters.jurisdictions"
            ),
            event_types=_as_str_list(
                filters_data.get("event_types", []), "filters.event_types"
            ),
            filer_roles=_as_str_list(
                filters_data.get("filer_roles", []), "filters.filer_roles"
            ),
            exclude_filer_roles=_as_str_list(
                filters_data.get("exclude_filer_roles", []), "filters.exclude_filer_roles"
            ),
        )

    threshold_usd = (
        _as_float(data["threshold_usd"], "threshold_usd") if "threshold_usd" in data else None
    )
    near_threshold_usd = (
        _as_float(data["near_threshold_usd"], "near_threshold_usd")
        if "near_threshold_usd" in data
        else None
    )
    instant_usd = (
        _as_float(data["instant_usd"], "instant_usd") if "instant_usd" in data else None
    )
    return Profile(
        origin=str(path),
        name=data.get("name"),
        description=data.get("description"),
        threshold_usd=threshold_usd,
        near_threshold_usd=near_threshold_usd,
        instant_usd=instant_usd,
        sources_enabled=sources_enabled,
        filters=filters,
    )


def apply_profile(base: Settings, profile: Profile) -> Settings:
    """Layer a validated profile onto a `Settings` (normally plain dataclass defaults).

    Source names are validated against the registry here, at load time, rather than
    left to fail later inside `build_sources` -- a profile with a typo'd source name
    should refuse to load, the same way an unknown top-level key does.
    """
    updates: dict[str, Any] = {}
    if profile.threshold_usd is not None:
        updates["threshold_usd"] = profile.threshold_usd
    if profile.near_threshold_usd is not None:
        updates["near_threshold_usd"] = profile.near_threshold_usd
    if profile.instant_usd is not None:
        updates["instant_usd"] = profile.instant_usd
    if profile.sources_enabled is not None:
        from whale_agent.ingestion.registry import UnknownSourceError, registered_source_names

        known = set(registered_source_names())
        for name in profile.sources_enabled:
            if name not in known:
                raise UnknownSourceError(name)
        updates["sources"] = list(profile.sources_enabled)
    if profile.filters is not None:
        updates["filter_jurisdictions"] = list(profile.filters.jurisdictions)
        updates["filter_event_types"] = list(profile.filters.event_types)
        updates["filter_filer_roles"] = list(profile.filters.filer_roles)
        updates["filter_exclude_roles"] = list(profile.filters.exclude_filer_roles)
    return replace(base, **updates)


def load_settings(profile_name: str | None) -> Settings:
    """The layered build: defaults -> profile (if given) -> environment.

    CLI flags are layered on top of this result by each job's `main()`, via
    `dataclasses.replace`, since only the caller knows which flags it parsed.
    """
    base = Settings()
    if profile_name:
        base = apply_profile(base, load_profile(profile_name))
    return Settings.from_env(base=base)


# -- provenance: "--show-config" support ------------------------------------------

# The Settings fields a profile (or CLI flag) can actually move. Limited to these on
# purpose: tracing all ~80 fields through three layers would bury the handful that
# matter under noise, and every field outside this list is untouched by the profile
# layer, so it is always "default" or "environment" by construction.
TRACKED_FIELDS = (
    "threshold_usd",
    "near_threshold_usd",
    "instant_usd",
    "sources",
    "filter_jurisdictions",
    "filter_event_types",
    "filter_filer_roles",
    "filter_exclude_roles",
)


@dataclass(frozen=True)
class FieldProvenance:
    field: str
    value: Any
    layer: str  # "default" | "profile" | "environment" | "cli"


@dataclass(frozen=True)
class ResolvedConfig:
    """A fully layered `Settings`, plus where each tracked field's value came from."""

    settings: Settings
    profile: Profile | None
    fields: tuple[FieldProvenance, ...]


def resolve_config(
    profile_name: str | None, cli_overrides: dict[str, Any] | None = None
) -> ResolvedConfig:
    """Build settings through all four layers and report which layer each field settled at.

    This is the implementation behind `--show-config`'s provenance output: for each
    tracked field, compares its value across the defaults -> profile -> environment ->
    CLI chain to find the last layer that actually changed it, rather than guessing from
    which layer is "supposed to" win.
    """
    cli_overrides = cli_overrides or {}
    defaults = Settings()
    profile = load_profile(profile_name) if profile_name else None
    after_profile = apply_profile(defaults, profile) if profile is not None else defaults
    after_env = Settings.from_env(base=after_profile)
    after_cli = replace(after_env, **cli_overrides) if cli_overrides else after_env

    rows = []
    for name in TRACKED_FIELDS:
        value = getattr(after_cli, name)
        if name in cli_overrides:
            layer = "cli"
        elif getattr(after_env, name) != getattr(after_profile, name):
            layer = "environment"
        elif profile is not None and getattr(after_profile, name) != getattr(defaults, name):
            layer = "profile"
        else:
            layer = "default"
        rows.append(FieldProvenance(name, value, layer))

    return ResolvedConfig(settings=after_cli, profile=profile, fields=tuple(rows))
