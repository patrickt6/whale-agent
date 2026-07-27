"""Equity price lookup: the seam that turns share counts and percentages into dollars.

This is deliberately parallel to `fx.py`'s `FxProvider`, not an extension of it: FX
converts a currency, this prices a security, and conflating them makes both harder to
stub. `valuation.py` accepts an optional `PriceProvider` and only calls it when an
event has no filing-stated price of its own.

A quote carries its own `PriceSource` because provenance matters downstream: a close on
the transaction date and a stale most-recent close are both estimates, but the digest
must be able to say which one it used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from whale_agent.config import Settings, get_settings
from whale_agent.errors import SourceUnavailableError
from whale_agent.ingestion._http import request_json
from whale_agent.models.enums import PriceSource


@dataclass(frozen=True)
class PriceQuote:
    """A price plus where it came from and what day it is actually for."""

    price: float
    source: PriceSource
    as_of: date


class PriceProvider(Protocol):
    def close_on(self, ticker: str, on: date) -> PriceQuote | None:
        """Closing price for `ticker` on `on`, or the most recent close before it.

        Returns None when the ticker is unknown or no bar is available; callers must
        treat that as "cannot value this event" rather than guessing.
        """
        ...

    def shares_outstanding(self, ticker: str) -> float | None:
        """Share count for percentage-of-company valuation. None if unknown."""
        ...


def pick_close(bars: dict[date, float], on: date) -> PriceQuote | None:
    """Choose a close from `{date: close}` using 's preference order.

    1. The close on the transaction date itself.
    2. Otherwise the most recent close *before* it (weekends, holidays, halts).

    Never looks forward: a price from after the transaction date is information the
    filer did not have, and using it would silently overstate or understate the move.
    """
    if not bars:
        return None
    if on in bars:
        return PriceQuote(bars[on], PriceSource.CLOSE_ON_DATE, on)
    earlier = [d for d in bars if d < on]
    if not earlier:
        return None
    latest = max(earlier)
    return PriceQuote(bars[latest], PriceSource.MOST_RECENT_CLOSE, latest)


class StaticPriceProvider:
    """In-memory prices for tests and offline demos. `{ticker: {date: close}}`."""

    def __init__(
        self,
        bars: dict[str, dict[date, float]] | None = None,
        shares: dict[str, float] | None = None,
    ) -> None:
        self._bars = {k.upper(): v for k, v in (bars or {}).items()}
        self._shares = {k.upper(): v for k, v in (shares or {}).items()}

    def close_on(self, ticker: str, on: date) -> PriceQuote | None:
        return pick_close(self._bars.get(ticker.upper(), {}), on)

    def shares_outstanding(self, ticker: str) -> float | None:
        return self._shares.get(ticker.upper())


def parse_fmp_bars(payload: object) -> dict[date, float]:
    """Parse an FMP end-of-day price response into `{date: close}`.

    Tolerates both response shapes FMP has shipped: the legacy v3 envelope
    (`{"symbol": ..., "historical": [...]}`) and the newer flat list of bars. Rows
    missing a date or close are skipped rather than defaulted -- a fabricated zero
    would flow straight into a dollar figure.
    """
    rows: list[dict] = []
    if isinstance(payload, dict):
        candidate = payload.get("historical") or payload.get("results") or []
        if isinstance(candidate, list):
            rows = [r for r in candidate if isinstance(r, dict)]
    elif isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]

    bars: dict[date, float] = {}
    for row in rows:
        raw_date = row.get("date") or row.get("priceDate")
        close = row.get("close") if row.get("close") is not None else row.get("adjClose")
        if not raw_date or close is None:
            continue
        try:
            day = date.fromisoformat(str(raw_date)[:10])
            bars[day] = float(close)
        except (ValueError, TypeError):
            continue
    return bars


class FmpPriceProvider:
    """Prices and share counts from Financial Modeling Prep.

    Optional: the pipeline only constructs one when `settings.fmp_enabled` is true.
    Results are cached per (ticker, window) for the life of the object so a digest run
    over many events at one issuer costs one request, not one per event.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        lookback_days: int = 10,
    ) -> None:
        self.settings = settings or get_settings()
        self.lookback_days = lookback_days
        self._bar_cache: dict[tuple[str, str], dict[date, float]] = {}
        self._shares_cache: dict[str, float | None] = {}

    # -- requests ---------------------------------------------------------------
    def _get(self, path: str, params: dict) -> object:
        key = self.settings.require("fmp_api_key")
        return request_json(
            f"{self.settings.fmp_base_url.rstrip('/')}{path}",
            params={**params, "apikey": key},
            settings=self.settings,
        )

    def _bars(self, ticker: str, on: date) -> dict[date, float]:
        from datetime import timedelta

        start = on - timedelta(days=self.lookback_days)
        cache_key = (ticker.upper(), start.isoformat())
        if cache_key in self._bar_cache:
            return self._bar_cache[cache_key]
        payload = self._get(
            "/stable/historical-price-eod/full",
            {"symbol": ticker.upper(), "from": start.isoformat(), "to": on.isoformat()},
        )
        bars = parse_fmp_bars(payload)
        self._bar_cache[cache_key] = bars
        return bars

    # -- PriceProvider ----------------------------------------------------------
    def close_on(self, ticker: str, on: date) -> PriceQuote | None:
        if not ticker:
            return None
        try:
            return pick_close(self._bars(ticker, on), on)
        except SourceUnavailableError:
            # A price lookup failure must not sink the whole digest; the event simply
            # stays unvalued and is reported as "not disclosed".
            return None

    def shares_outstanding(self, ticker: str) -> float | None:
        """Share count for percentage-of-company valuation.

        Asks `/stable/shares-float` first, because that endpoint reports
        `outstandingShares` as a real field. `/stable/profile` does **not** carry a share
        count at all despite what the documentation implies, so the profile path can only
        ever *derive* one as market cap over price -- which compounds two rounded figures
        and drifts intraday as the price moves.

        That distinction matters more here than it looks: this number is the denominator
        for every percent-of-company filing, which is how Taiwan and Japan get valued at
        all. An approximate denominator makes an approximate dollar figure, and the
        product's whole claim is about knowing which figures are exact.
        """
        if not ticker:
            return None
        key = ticker.upper()
        if key in self._shares_cache:
            return self._shares_cache[key]

        value: float | None = None
        try:
            payload = self._get("/stable/shares-float", {"symbol": key})
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if isinstance(row, dict) and row.get("outstandingShares"):
                    value = float(row["outstandingShares"])
                    break
        except (SourceUnavailableError, ValueError, TypeError):
            value = None

        if value is None:
            # Derived fallback, for tickers shares-float does not cover.
            try:
                payload = self._get("/stable/profile", {"symbol": key})
                rows = payload if isinstance(payload, list) else [payload]
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    cap, px = row.get("marketCap"), row.get("price")
                    if cap and px:
                        value = float(cap) / float(px)
                        break
            except (SourceUnavailableError, ValueError, TypeError):
                value = None

        self._shares_cache[key] = value
        return value
