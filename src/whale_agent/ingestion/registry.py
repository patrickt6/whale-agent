"""The source registry: which adapters run today, and why the others did not.

This is the one place that answers "where does the digest get its data". Adding a
jurisdiction or vendor means decorating one builder function with `@register_source`
here; nothing else in the pipeline changes, which is the whole point of the `Adapter`
ABC.

Every source carries an explicit enabled/disabled decision with a human-readable reason.
A disabled source is not silently missing -- it becomes a coverage note on the digest, so
an empty Taiwan section reads as "Taiwan is switched off" rather than "nothing happened
in Taiwan".

`@register_source` replaces what used to be a hardcoded list literal in `build_sources`.
The list literal meant adding a source touched two files (the adapter itself, and this
one); the decorator collapses that back to one, since the registration lives next to the
builder that needs it. A profile's `[sources] enabled = [...]` (see `profiles.py`)
selects by the names registered here, and asking for a name that was never registered is
a hard error rather than a silent no-op -- the same reasoning the profile schema applies
to unknown keys.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from difflib import get_close_matches
from typing import Protocol

from whale_agent.config import Settings, get_settings
from whale_agent.ingestion.crypto_arkham import ArkhamAdapter
from whale_agent.ingestion.crypto_whale_alert import WhaleAlertAdapter
from whale_agent.ingestion.dataroma import DataromaInsiderBuysAdapter
from whale_agent.ingestion.gov.lobbying import SenateLdaAdapter
from whale_agent.ingestion.gov.usaspending import UsaSpendingAdapter
from whale_agent.ingestion.japan_edinet import JapanEdinetAdapter
from whale_agent.ingestion.taiwan_mops import TaiwanMopsAdapter
from whale_agent.ingestion.us_sec import UsSecForm4Adapter
from whale_agent.ingestion.us_sec_13d import UsSec13DGAdapter
from whale_agent.ingestion.vendors.finnhub import FinnhubInsiderAdapter
from whale_agent.ingestion.vendors.fmp import FmpCongressAdapter, FmpInsiderAdapter
from whale_agent.ingestion.vendors.quiver import (
    QuiverCongressAdapter,
    QuiverGovContractsAdapter,
    QuiverLobbyingAdapter,
)
from whale_agent.ingestion.vendors.unusual_whales import UnusualWhalesCongressAdapter

# Looks up the last successful run for a source name, so an adapter can fetch forward
# from it instead of re-reading the same head of the index every run. Returns None when
# there is no watermark yet, which means "fetch unbounded".
SinceFn = Callable[[str], "date | None"]


class SourceRunsReader(Protocol):
    """The one thing `build_sources` needs from a Store: the per-source watermark."""

    def source_runs(self) -> dict[str, dict]: ...


@dataclass
class SourceSpec:
    """One runnable data source plus the decision about whether it runs.

    `kind` distinguishes the two shapes a source can produce. "event" sources feed the
    daily $5M gate via `run() -> list[NormalizedEvent]`; "annotation" sources (quarterly
    lobbying spend, federal contract awards) are company-level context with no filer
    taking a position, produce `list[ContextAnnotation]` from `run()` instead, and are
    never routed into `collect_events`'s event list -- see `jobs/pipeline.py`.
    """

    name: str
    label: str  # human-readable, used in coverage notes
    enabled: bool
    run: Callable[[], list]
    disabled_reason: str = ""
    kind: str = "event"
    jurisdiction: str = "us"
    requires: tuple[str, ...] = ()


@dataclass
class _Registration:
    """A builder function plus the metadata `@register_source` attached to it."""

    name: str
    jurisdiction: str
    requires: tuple[str, ...]
    kind: str
    build: Callable[[Settings, int, date, SinceFn], SourceSpec]


_REGISTRY: dict[str, _Registration] = {}


class UnknownSourceError(ValueError):
    """Raised when a profile or WHALE_SOURCES names a source that was never registered."""

    def __init__(self, name: str) -> None:
        known = sorted(_REGISTRY)
        suggestion = get_close_matches(name, known, n=1)
        msg = f"unknown source {name!r}."
        if suggestion:
            msg += f" Did you mean {suggestion[0]!r}?"
        msg += f" Registered sources: {', '.join(known)}"
        super().__init__(msg)


def register_source(
    name: str, *, jurisdiction: str = "us", requires: tuple[str, ...] = (), kind: str = "event"
) -> Callable:
    """Decorator: register a `Settings, limit, on, since -> SourceSpec` builder.

    `requires` is documentation, not enforcement: it names the Settings attributes a
    source needs (e.g. `("arkham_api_key",)`), surfaced by `registered_sources()` for
    `--show-config` and for anyone reading this file to add a source of their own. The
    actual enable-flag-AND-credential gating stays inside each builder, via the existing
    `*_enabled` properties on `Settings` -- that is the one mechanism that must keep
    working exactly as it does today, so it is deliberately not duplicated here.
    """

    def decorator(
        build: Callable[[Settings, int, date, SinceFn], SourceSpec],
    ) -> Callable[[Settings, int, date, SinceFn], SourceSpec]:
        if name in _REGISTRY:
            raise ValueError(f"source {name!r} is already registered")
        _REGISTRY[name] = _Registration(
            name=name, jurisdiction=jurisdiction, requires=requires, kind=kind, build=build
        )
        return build

    return decorator


def registered_source_names() -> list[str]:
    """Every registered source name, in registration order."""
    return list(_REGISTRY)


def registered_sources() -> list[_Registration]:
    return list(_REGISTRY.values())


def _spec(
    name: str,
    label: str,
    enabled: bool,
    run: Callable[[], list],
    disabled_reason: str = "",
    kind: str = "event",
    jurisdiction: str = "us",
    requires: tuple[str, ...] = (),
) -> SourceSpec:
    return SourceSpec(name, label, enabled, run, disabled_reason, kind, jurisdiction, requires)


# -- US ------------------------------------------------------------------------------


@register_source("sec_form4", jurisdiction="us")
def _build_sec_form4(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "sec_form4",
        "SEC Form 4",
        True,  # edgartools needs no key; only a contact string in User-Agent
        lambda: UsSecForm4Adapter().ingest(
            limit=limit,
            form="4",
            user_agent=s.sec_user_agent,
            since=since("sec_form4"),
        ),
    )


@register_source("sec_13dg", jurisdiction="us")
def _build_sec_13dg(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "sec_13dg",
        "SEC SC 13D/G",
        True,
        lambda: UsSec13DGAdapter().ingest(
            limit=limit,
            form=["SC 13D", "SC 13G"],
            user_agent=s.sec_user_agent,
            since=since("sec_13dg"),
        ),
    )


@register_source("fmp_insider", jurisdiction="us", requires=("fmp_api_key",))
def _build_fmp_insider(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "fmp_insider",
        "FMP insider trades",
        s.fmp_enabled,
        lambda: FmpInsiderAdapter(s).ingest(limit=limit),
        "FMP_API_KEY not set" if s.enable_fmp else "disabled via WHALE_ENABLE_FMP",
    )


@register_source("fmp_congress", jurisdiction="us", requires=("fmp_api_key",))
def _build_fmp_congress(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "fmp_congress",
        "FMP congressional trades",
        s.fmp_enabled,
        lambda: (
            FmpCongressAdapter(s).ingest(chamber="senate")
            + FmpCongressAdapter(s).ingest(chamber="house")
        ),
        "FMP_API_KEY not set" if s.enable_fmp else "disabled via WHALE_ENABLE_FMP",
    )


@register_source("quiver_congress", jurisdiction="us", requires=("quiver_api_key",))
def _build_quiver_congress(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "quiver_congress",
        "Quiver congressional trades",
        s.quiver_enabled,
        lambda: QuiverCongressAdapter(s).ingest(),
        "QUIVER_QUANT_API_KEY not set"
        if s.enable_quiver
        else "disabled via WHALE_ENABLE_QUIVER",
    )


# -- Other jurisdictions ---------------------------------------------------------------


@register_source("taiwan_mops", jurisdiction="tw")
def _build_taiwan_mops(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "taiwan_mops",
        "Taiwan MOPS",
        s.taiwan_enabled,
        lambda: TaiwanMopsAdapter(s).ingest(dataset="insider_holdings"),
        "disabled via WHALE_ENABLE_TAIWAN",
    )


@register_source("japan_edinet", jurisdiction="jp", requires=("japan_edinet_api_key",))
def _build_japan_edinet(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "japan_edinet",
        "Japan EDINET",
        s.japan_enabled,
        lambda: JapanEdinetAdapter(s).ingest(on=on),
        "JAPAN_EDINET_API_KEY not set"
        if s.enable_japan
        else "disabled via WHALE_ENABLE_JAPAN",
    )


# -- Crypto ------------------------------------------------------------------------


@register_source("arkham", jurisdiction="crypto", requires=("arkham_api_key",))
def _build_arkham(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "arkham",
        "Arkham on-chain",
        s.arkham_enabled,
        lambda: ArkhamAdapter(s).ingest(min_usd=s.threshold_usd),
        "ARKHAM_API_KEY not set" if s.enable_arkham else "disabled via WHALE_ENABLE_ARKHAM",
    )


@register_source("whale_alert", jurisdiction="crypto", requires=("whale_alert_api_key",))
def _build_whale_alert(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "whale_alert",
        "Whale Alert on-chain",
        s.whale_alert_enabled,
        lambda: WhaleAlertAdapter(s).ingest(min_value=s.whale_alert_min_usd),
        "WHALE_ALERT_API_KEY not set"
        if s.enable_whale_alert
        else "disabled via WHALE_ENABLE_WHALE_ALERT",
    )


@register_source("dataroma", jurisdiction="us")
def _build_dataroma(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "dataroma",
        "Dataroma insider buys",
        s.dataroma_enabled,
        lambda: DataromaInsiderBuysAdapter(s).ingest(),
        "disabled via WHALE_ENABLE_DATAROMA",
    )


# -- Annotation-only sources -----------------------------------------------------------
# Quarterly, company-level context with no filer taking a position (lobbying spend,
# federal contract awards). These were fully built and tested but never wired into
# `build_sources`, so they ran for nobody -- registering them here makes that an
# explicit, visible "off by default" instead of an invisible gap. They stay disabled
# unconditionally: `collect_events` (jobs/pipeline.py) only ever calls `.run()` on
# `kind == "event"` specs, and there is deliberately no daily-digest wiring for
# annotations yet (see `digest_weekly.run_weekly_digest`'s `annotations` parameter for
# the one place that shape is consumed today). A profile can still name them in
# `[sources] enabled = [...]` without error -- that is what "selectable" means here --
# but doing so has no effect until a caller actually asks for their `.annotations()`.


@register_source("senate_lda", jurisdiction="us", kind="annotation")
def _build_senate_lda(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "senate_lda",
        "Senate LDA lobbying disclosures",
        False,
        lambda: SenateLdaAdapter(s).annotations([]),
        "context annotation source, not part of the daily event pipeline",
        kind="annotation",
    )


@register_source("usaspending", jurisdiction="us", kind="annotation")
def _build_usaspending(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "usaspending",
        "USASpending.gov federal contract awards",
        False,
        lambda: UsaSpendingAdapter(s).annotations([]),
        "context annotation source, not part of the daily event pipeline",
        kind="annotation",
    )


@register_source(
    "quiver_lobbying", jurisdiction="us", requires=("quiver_api_key",), kind="annotation"
)
def _build_quiver_lobbying(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "quiver_lobbying",
        "Quiver lobbying spend",
        False,
        lambda: QuiverLobbyingAdapter(s).annotations([]),
        "context annotation source, not part of the daily event pipeline",
        kind="annotation",
    )


@register_source(
    "quiver_gov_contracts", jurisdiction="us", requires=("quiver_api_key",), kind="annotation"
)
def _build_quiver_gov_contracts(
    s: Settings, limit: int, on: date, since: SinceFn
) -> SourceSpec:
    return _spec(
        "quiver_gov_contracts",
        "Quiver federal contract awards",
        False,
        lambda: QuiverGovContractsAdapter(s).annotations([]),
        "context annotation source, not part of the daily event pipeline",
        kind="annotation",
    )


@register_source("finnhub_insider", jurisdiction="us", requires=("finnhub_api_key",))
def _build_finnhub_insider(s: Settings, limit: int, on: date, since: SinceFn) -> SourceSpec:
    return _spec(
        "finnhub_insider",
        "Finnhub insider transactions",
        s.finnhub_enabled,
        lambda: FinnhubInsiderAdapter(s).ingest(symbols=s.watch_tickers or [""]),
        "FINNHUB_API_KEY not set" if s.enable_finnhub else "disabled via WHALE_ENABLE_FINNHUB",
    )


@register_source(
    "unusual_whales_congress", jurisdiction="us", requires=("unusual_whales_api_key",)
)
def _build_unusual_whales_congress(
    s: Settings, limit: int, on: date, since: SinceFn
) -> SourceSpec:
    return _spec(
        "unusual_whales_congress",
        "Unusual Whales congressional trades",
        s.unusual_whales_enabled,
        lambda: UnusualWhalesCongressAdapter(s).ingest(),
        "UNUSUAL_WHALES_API_KEY not set"
        if s.enable_unusual_whales
        else "disabled via WHALE_ENABLE_UNUSUAL_WHALES",
    )


def build_sources(
    settings: Settings | None = None,
    *,
    limit: int = 50,
    on: date | None = None,
    store: SourceRunsReader | None = None,
) -> list[SourceSpec]:
    """Build every source spec, honouring the enable flags and credential presence.

    `store`, when given, supplies each EDGAR adapter's watermark: the `last_success_at`
    recorded in `source_runs` for that source name. Without it (e.g. in tests that don't
    pass a Store) the adapters fall back to their old unbounded `head(limit)` behaviour.
    """
    s = settings or get_settings()
    on = on or date.today()

    def _since(source_name: str) -> date | None:
        """Last recorded success for `source_name`, as a date, or None on first run."""
        if store is None:
            return None
        try:
            runs = store.source_runs()
        except Exception:  # noqa: BLE001 - a broken watermark must not block the fetch
            return None
        run = runs.get(source_name)
        stamp = run.get("last_success_at") if run else None
        if not stamp:
            return None
        try:
            return datetime.fromisoformat(stamp).date()
        except ValueError:
            return None

    specs = [reg.build(s, limit, on, _since) for reg in _REGISTRY.values()]

    # An explicit WHALE_SOURCES (or profile `[sources] enabled`) list narrows the run
    # without changing any other flag. Unknown names are a hard error rather than a
    # silent no-op, symmetrically with the profile schema's unknown-key check.
    if s.sources:
        unknown = set(s.sources) - set(_REGISTRY)
        if unknown:
            raise UnknownSourceError(sorted(unknown)[0])
        wanted = set(s.sources)
        for spec in specs:
            if spec.name not in wanted:
                spec.enabled = False
                # Deliberately does not name WHALE_SOURCES: the same narrowing can come
                # from a profile's `[sources] enabled`, and a reason that blames the env
                # var sends a reader to edit the wrong layer.
                spec.disabled_reason = "not in the selected source list"
    return specs


def enabled_sources(specs: list[SourceSpec]) -> list[SourceSpec]:
    return [spec for spec in specs if spec.enabled]
