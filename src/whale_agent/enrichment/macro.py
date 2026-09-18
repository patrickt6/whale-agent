"""Macro backdrop for a thesis: policy rate, long yield, credit, sector employment.

Why the digest wants this at all: "the CEO bought $8M of a regional bank" reads
differently in a quarter when bank credit is contracting than in one when it is not.
The macro layer exists to let the weekly report say which quarter it is, and nothing
more -- it is background, never a signal on its own.

Deliberately parallel to `price.PriceProvider` and `fx.FxProvider`: a small Protocol, a
static implementation for tests and offline demos, and live implementations the pipeline
only constructs when the settings say they are usable.

**Every observation carries its own date, and there is no code path that returns a bare
float.** This is not tidiness. FRED's H.8 bank-credit series lags by roughly a week,
nonfarm payrolls by a month, and Treasury's average-interest table is monthly and posts
after the month closes. A figure printed as "current" that is actually six weeks old is
a wrong number in a document whose whole claim is that its numbers are checkable.

Sources and licensing:

- **Treasury Fiscal Data** (`api.fiscaldata.treasury.gov`): keyless, US public domain,
  works today. Verified live. Note the `page[size]` parameter's brackets must be
  percent-encoded; `request_json` passes params through httpx, which does that.
- **FRED** (Federal Reserve Bank of St. Louis): needs a free API key that this project
  does not have yet, so `FredMacroProvider` is built, key-gated, and off. Its parsing is
  written against the documented `observations` envelope and has *not* been checked
  against a live response -- the first run with a real key should be treated as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from whale_agent.config import Settings, get_settings
from whale_agent.errors import SourceUnavailableError
from whale_agent.ingestion._http import request_json

# The named series the rest of the app asks for. Callers use these constants rather than
# raw FRED ids so a series being renamed or replaced is a one-line change here.
POLICY_RATE = "policy_rate"
TEN_YEAR_YIELD = "ten_year_yield"
BANK_CREDIT = "bank_credit"
SECTOR_EMPLOYMENT = "sector_employment"

MACRO_SERIES = (POLICY_RATE, TEN_YEAR_YIELD, BANK_CREDIT, SECTOR_EMPLOYMENT)

# FRED series ids behind each name, with the units they are published in.
FRED_SERIES_IDS: dict[str, tuple[str, str]] = {
    POLICY_RATE: ("DFF", "percent"),  # effective fed funds rate, daily
    TEN_YEAR_YIELD: ("DGS10", "percent"),  # 10-year constant maturity, daily
    BANK_CREDIT: ("TOTBKCR", "USD billions"),  # H.8 bank credit, all commercial banks
    SECTOR_EMPLOYMENT: ("MANEMP", "thousands of persons"),  # manufacturing payrolls
}

# Treasury's average-interest table reports by security type, so it can stand in for
# two of the four names and cannot cover the other two at all.
_TREASURY_SECURITY_FOR: dict[str, str] = {
    POLICY_RATE: "Treasury Bills",  # bill yield as a policy-rate proxy, not the target
    TEN_YEAR_YIELD: "Treasury Notes",  # notes average 2-10y, so this is an approximation
}


@dataclass(frozen=True)
class MacroObservation:
    """One macro figure, with the day it is actually for and where it came from."""

    series: str
    value: float
    as_of: date
    units: str = ""
    source: str = ""
    is_proxy: bool = False  # true when the series stands in for what was asked for

    def describe(self) -> str:
        """One-line rendering for the report, dated so it cannot be read as today's."""
        unit = f" {self.units}" if self.units else ""
        proxy = " (proxy)" if self.is_proxy else ""
        return f"{self.series} {self.value:g}{unit} as of {self.as_of.isoformat()}{proxy}"


class MacroProvider(Protocol):
    def observe(self, series: str) -> MacroObservation | None:
        """Latest observation for a named series, or None when unavailable.

        None means "we do not know", and callers must render it as absent rather than
        substituting a previous run's figure.
        """
        ...

    def snapshot(self) -> dict[str, MacroObservation]:
        """Every series this provider can supply right now, keyed by name."""
        ...


class StaticMacroProvider:
    """In-memory observations for tests and offline demos.

    Takes `{series: (value, as_of)}` so a test cannot accidentally construct an undated
    figure -- the thing this module exists to prevent.
    """

    def __init__(self, values: dict[str, tuple[float, date]] | None = None) -> None:
        self._values = dict(values or {})

    def observe(self, series: str) -> MacroObservation | None:
        entry = self._values.get(series)
        if entry is None:
            return None
        value, as_of = entry
        units = FRED_SERIES_IDS.get(series, ("", ""))[1]
        return MacroObservation(
            series=series, value=value, as_of=as_of, units=units, source="static"
        )

    def snapshot(self) -> dict[str, MacroObservation]:
        out: dict[str, MacroObservation] = {}
        for name in self._values:
            obs = self.observe(name)
            if obs is not None:
                out[name] = obs
        return out


def parse_fred_observations(payload: object) -> tuple[float, date] | None:
    """Latest real observation from a FRED `observations` envelope.

    FRED marks missing data with the string ".", which `float()` would reject and a
    careless `or 0` would turn into a fabricated zero, so those rows are skipped and the
    most recent genuine reading wins. Written to the documented shape; unverified live.
    """
    if not isinstance(payload, dict):
        return None
    rows = payload.get("observations")
    if not isinstance(rows, list):
        return None
    best: tuple[float, date] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_value, raw_date = row.get("value"), row.get("date")
        if raw_value in (None, "", "."):
            continue
        try:
            value = float(raw_value)
            day = date.fromisoformat(str(raw_date)[:10])
        except (TypeError, ValueError):
            continue
        if best is None or day > best[1]:
            best = (value, day)
    return best


class FredMacroProvider:
    """FRED series, gated on `FRED_API_KEY`. Inert until a key exists.

    Constructed only when `settings.fred_enabled`; without a key `observe()` returns
    None for everything rather than raising, so wiring it up early costs nothing.
    """

    source = "fred"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: dict[str, MacroObservation | None] = {}

    def observe(self, series: str) -> MacroObservation | None:
        if series in self._cache:
            return self._cache[series]
        result = self._fetch(series)
        self._cache[series] = result
        return result

    def _fetch(self, series: str) -> MacroObservation | None:
        entry = FRED_SERIES_IDS.get(series)
        if entry is None or not self.settings.fred_enabled:
            return None
        series_id, units = entry
        try:
            payload = request_json(
                f"{self.settings.fred_base_url.rstrip('/')}/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": self.settings.fred_api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 10,
                },
                settings=self.settings,
            )
        except SourceUnavailableError:
            return None
        parsed = parse_fred_observations(payload)
        if parsed is None:
            return None
        value, as_of = parsed
        return MacroObservation(
            series=series, value=value, as_of=as_of, units=units, source=self.source
        )

    def snapshot(self) -> dict[str, MacroObservation]:
        out: dict[str, MacroObservation] = {}
        for name in MACRO_SERIES:
            obs = self.observe(name)
            if obs is not None:
                out[name] = obs
        return out


def parse_treasury_rates(payload: object) -> dict[str, tuple[float, date]]:
    """Parse Treasury's average-interest-rates response into `{security: (rate, date)}`.

    Every field in this API arrives as a string, including the numbers, and rows are
    returned oldest-first or newest-first depending on `sort`, so the latest record per
    security is picked by date rather than by position.
    """
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("data")
    if not isinstance(rows, list):
        return {}
    out: dict[str, tuple[float, date]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        desc = row.get("security_desc")
        try:
            rate = float(row.get("avg_interest_rate_amt"))
            day = date.fromisoformat(str(row.get("record_date"))[:10])
        except (TypeError, ValueError):
            continue
        if not desc:
            continue
        current = out.get(desc)
        if current is None or day > current[1]:
            out[desc] = (rate, day)
    return out


class TreasuryMacroProvider:
    """Treasury Fiscal Data average interest rates. Keyless, and works today.

    Covers the rate end of the macro picture only, and honestly: these are averages
    across outstanding debt, not market yields, so what it can supply for
    `POLICY_RATE` and `TEN_YEAR_YIELD` is flagged `is_proxy=True`. Bank credit and
    sector employment are not in this dataset and come back None -- an empty slot the
    report can leave blank, rather than a plausible wrong number.
    """

    source = "treasury_fiscal_data"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._rates: dict[str, tuple[float, date]] | None = None

    def _load(self) -> dict[str, tuple[float, date]]:
        if self._rates is not None:
            return self._rates
        if not self.settings.treasury_enabled:
            self._rates = {}
            return self._rates
        try:
            payload = request_json(
                f"{self.settings.treasury_base_url.rstrip('/')}"
                "/v2/accounting/od/avg_interest_rates",
                params={"page[size]": 20, "sort": "-record_date"},
                settings=self.settings,
            )
        except SourceUnavailableError:
            self._rates = {}
            return self._rates
        self._rates = parse_treasury_rates(payload)
        return self._rates

    def observe(self, series: str) -> MacroObservation | None:
        security = _TREASURY_SECURITY_FOR.get(series)
        if security is None:
            return None
        entry = self._load().get(security)
        if entry is None:
            return None
        value, as_of = entry
        return MacroObservation(
            series=series,
            value=value,
            as_of=as_of,
            units="percent",
            source=self.source,
            is_proxy=True,
        )

    def snapshot(self) -> dict[str, MacroObservation]:
        out: dict[str, MacroObservation] = {}
        for name in _TREASURY_SECURITY_FOR:
            obs = self.observe(name)
            if obs is not None:
                out[name] = obs
        return out


class ChainedMacroProvider:
    """Ask each provider in order and keep the first answer for each series.

    Order is preference, so put FRED first: when a key eventually exists its real
    market yields displace Treasury's proxies, and until then the proxies still fill the
    slots. Nothing here raises; a provider that fails simply contributes nothing.
    """

    def __init__(self, *providers: MacroProvider) -> None:
        self.providers = providers

    def observe(self, series: str) -> MacroObservation | None:
        for provider in self.providers:
            try:
                obs = provider.observe(series)
            except Exception:  # a context feed must never sink the digest
                continue
            if obs is not None:
                return obs
        return None

    def snapshot(self) -> dict[str, MacroObservation]:
        out: dict[str, MacroObservation] = {}
        for name in MACRO_SERIES:
            obs = self.observe(name)
            if obs is not None:
                out[name] = obs
        return out


def build_macro_provider(settings: Settings | None = None) -> MacroProvider:
    """The provider the pipeline should use, given what is configured.

    Always returns something: with no FRED key and Treasury switched off, that is an
    empty `StaticMacroProvider`, which answers None to everything.
    """
    s = settings or get_settings()
    providers: list[MacroProvider] = []
    if s.fred_enabled:
        providers.append(FredMacroProvider(s))
    if s.treasury_enabled:
        providers.append(TreasuryMacroProvider(s))
    if not providers:
        return StaticMacroProvider()
    return ChainedMacroProvider(*providers)
