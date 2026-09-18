"""Market-scale and price-action enrichment: the denominator behind every headline.

A disclosed dollar figure means nothing on its own. $6M is a controlling conviction bet
in a $200M micro cap and a rounding error at Apple, and a 40% run in the month before a
filing turns "somebody is buying" into "somebody is explaining last month's chart". This
module fetches the three facts that supply that judgement -- company scale, free float,
and trailing price change -- and writes them into `EventContext`.

Three design choices worth the words:

1. **A snapshot, not a bag of getters.** Everything here is per-ticker and static over a
   digest run, so one `MarketSnapshot` per ticker is both the cache unit and the return
   type. Enriching 20 events across 5 issuers costs 5 lookups, not 20, and not 60.
2. **Percent of *float*, not of shares outstanding.** Outstanding includes stock nobody
   can buy (insider lockups, strategic holders); float is the honest denominator for
   "how much of the buyable company did this person take down".
3. **None is a first-class answer.** Every method swallows a source failure and returns
   None, mirroring `FmpPriceProvider`: a vendor outage must leave events unenriched, not
   sink the digest. And a missing input never becomes a zero -- scoring distinguishes
   "we checked and it is small" from "we never learned this", so arithmetic is only
   performed when every input is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from whale_agent.config import Settings, get_settings
from whale_agent.errors import SourceUnavailableError, WhaleAgentError
from whale_agent.ingestion._http import request_json
from whale_agent.models.context import EventContext
from whale_agent.models.event import NormalizedEvent


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything we know about a ticker that does not depend on a particular event."""

    ticker: str
    company_name: str | None = None
    market_cap_usd: float | None = None
    shares_float: float | None = None
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    price_change_1m_pct: float | None = None
    price_change_3m_pct: float | None = None
    price_change_ytd_pct: float | None = None


class MarketContextProvider(Protocol):
    def snapshot(self, ticker: str) -> MarketSnapshot | None:
        """Company scale and price action for `ticker`, or None if we learned nothing.

        Never raises: an unknown ticker, an unconfigured key, and a vendor outage are
        all reported the same way, because the caller's response to all three is the
        same -- leave the context fields empty.
        """
        ...


def _float(value: Any) -> float | None:
    """Coerce a vendor field to float, treating blanks and junk as "not learned"."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _rows(payload: object) -> list[dict]:
    """FMP returns a single-element array for these symbol endpoints, but has shipped
    bare objects and `{"data": [...]}` envelopes on others; accept all three."""
    if isinstance(payload, dict):
        inner = payload.get("data") or payload.get("results")
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
        return [payload]
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


def parse_fmp_profile(payload: object) -> dict[str, Any]:
    """Extract scale and classification fields from `/stable/profile`.

    Market cap is taken from `marketCap` directly rather than derived from price times
    shares: the vendor's own figure is the one a reader could reproduce from the site.
    """
    for row in _rows(payload):
        return {
            "market_cap_usd": _float(row.get("marketCap")),
            "company_name": _text(row.get("companyName")),
            "sector": _text(row.get("sector")),
            "industry": _text(row.get("industry")),
            "exchange": _text(row.get("exchange")),
        }
    return {}


def parse_fmp_shares_float(payload: object) -> float | None:
    """Free float share count from `/stable/shares-float`.

    `floatShares` is the tradeable count; `outstandingShares` sits beside it in the same
    row and is deliberately ignored (see the module docstring on denominators).
    """
    for row in _rows(payload):
        return _float(row.get("floatShares"))
    return None


def parse_fmp_price_change(payload: object) -> dict[str, float | None]:
    """Trailing returns from `/stable/stock-price-change`.

    The keys are literally `"1M"`, `"3M"` and `"ytd"` -- note the inconsistent casing,
    which is the vendor's, not a typo. Values are already percentages.
    """
    for row in _rows(payload):
        return {
            "price_change_1m_pct": _float(row.get("1M")),
            "price_change_3m_pct": _float(row.get("3M")),
            "price_change_ytd_pct": _float(row.get("ytd") or row.get("YTD")),
        }
    return {}


def percent_of(numerator: float | None, denominator: float | None) -> float | None:
    """`numerator / denominator` as a percentage, or None when it cannot be computed.

    A zero or negative denominator is "we did not learn this", not an infinity: FMP
    reports 0 for float on names it does not cover, and dividing by it would manufacture
    a spectacular percentage out of missing data.
    """
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        return None
    return numerator / denominator * 100.0


class StaticMarketContextProvider:
    """In-memory snapshots for tests and offline demos. `{ticker: MarketSnapshot}`."""

    def __init__(self, snapshots: dict[str, MarketSnapshot] | None = None) -> None:
        self._snapshots = {k.upper(): v for k, v in (snapshots or {}).items()}

    def snapshot(self, ticker: str) -> MarketSnapshot | None:
        if not ticker:
            return None
        return self._snapshots.get(ticker.upper())


class FmpMarketContextProvider:
    """Company scale and price action from Financial Modeling Prep.

    Optional, like every FMP-backed component: the pipeline only builds one when
    `settings.fmp_enabled`. The three endpoints behind a snapshot are fetched together
    and cached per ticker for the life of the object, including the negative result --
    a ticker FMP does not cover is asked about once per run, not once per event.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: dict[str, MarketSnapshot | None] = {}

    def _get(self, path: str, params: dict) -> Any:
        key = self.settings.require("fmp_api_key")
        return request_json(
            f"{self.settings.fmp_base_url.rstrip('/')}{path}",
            params={**params, "apikey": key},
            settings=self.settings,
        )

    def _fetch(self, symbol: str) -> MarketSnapshot | None:
        fields: dict[str, Any] = {}
        # Each endpoint is tried independently so a gap in one (float is missing for
        # many foreign listings) still leaves the other two attached to the event.
        for path, extract in (
            ("/stable/profile", lambda p: parse_fmp_profile(p)),
            ("/stable/shares-float", lambda p: {"shares_float": parse_fmp_shares_float(p)}),
            ("/stable/stock-price-change", lambda p: parse_fmp_price_change(p)),
        ):
            try:
                fields.update(extract(self._get(path, {"symbol": symbol})))
            except (SourceUnavailableError, ValueError, TypeError):
                continue
        if not any(v is not None for v in fields.values()):
            return None
        return MarketSnapshot(ticker=symbol, **fields)

    # -- MarketContextProvider --------------------------------------------------
    def snapshot(self, ticker: str) -> MarketSnapshot | None:
        if not ticker:
            return None
        symbol = ticker.upper()
        if symbol not in self._cache:
            try:
                self._cache[symbol] = self._fetch(symbol)
            except WhaleAgentError:
                # Including a missing API key: enrichment is best-effort by contract,
                # and the caller has no better move than carrying on without it.
                self._cache[symbol] = None
        return self._cache[symbol]


def context_from_snapshot(
    snapshot: MarketSnapshot | None,
    *,
    share_count: float | None = None,
    usd_value: float | None = None,
    into: EventContext | None = None,
) -> EventContext:
    """Write a snapshot plus the two derived percentages into an `EventContext`.

    Pure and event-free so it can be tested without constructing a filing. Mutates and
    returns `into` when given, so an event that already carries filer or cohort context
    keeps it.
    """
    context = into if into is not None else EventContext()
    if snapshot is None:
        return context
    context.market_cap_usd = snapshot.market_cap_usd
    context.shares_float = snapshot.shares_float
    context.sector = snapshot.sector
    context.industry = snapshot.industry
    context.exchange = snapshot.exchange
    context.price_change_1m_pct = snapshot.price_change_1m_pct
    context.price_change_3m_pct = snapshot.price_change_3m_pct
    context.price_change_ytd_pct = snapshot.price_change_ytd_pct
    # Free float excludes the holder's own block, so a large holder divided by it can
    # exceed 100%. A 53.89% holder rendered as "171.2% of float", which is arithmetically
    # right and reads as a bug, because no holding is 171% of anything. Above 100% the
    # denominator is no longer describing the same universe as the numerator, so the
    # figure is dropped. It is not clamped to 100%: that would be inventing a number.
    float_pct = percent_of(share_count, snapshot.shares_float)
    context.percent_of_float = float_pct if float_pct is None or float_pct <= 100.0 else None
    context.percent_of_market_cap = percent_of(usd_value, snapshot.market_cap_usd)
    return context


_CJK_RANGES = (
    (0x3040, 0x30FF),  # hiragana and katakana
    (0x3400, 0x4DBF),  # CJK ideographs, extension A
    (0x4E00, 0x9FFF),  # CJK ideographs
    (0xAC00, 0xD7AF),  # hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF66, 0xFF9F),  # halfwidth katakana
)


def is_cjk(name: str) -> bool:
    """True when the name contains Japanese, Chinese or Korean script.

    Any occurrence counts rather than a majority: "株式会社ASICS" is as unreadable to a
    non-reader as the fully Japanese form, and the vendor's Latin name is better for both.
    Fullwidth Latin ("Ｚｅｒｏ　Ｇａｍｉｎｇ") is deliberately excluded, since it is odd
    to look at but is still readable as English.
    """
    return any(any(low <= ord(char) <= high for low, high in _CJK_RANGES) for char in name)


def enrich_event(event: NormalizedEvent, provider: MarketContextProvider) -> NormalizedEvent:
    """Attach market context to one event in place, and return it for chaining.

    Events with no ticker (many foreign and crypto disclosures) are left untouched
    rather than looked up by name: a fuzzy symbol match would attach one company's
    market cap to another company's filing, which is worse than no context at all.
    """
    if not event.ticker:
        return event
    snapshot = provider.snapshot(event.ticker)
    context_from_snapshot(
        snapshot,
        share_count=event.share_count,
        usd_value=event.effective_usd,
        into=event.context,
    )

    # FMP's insider feed returns companyName as null, so the issuer falls back to the
    # ticker and an article ends up headlined "Several unrelated filers bought FSBC".
    # The profile response we just fetched carries the real name, so use it -- but only
    # when the existing name is nothing more than the symbol, never overwriting a name
    # the filing itself supplied.
    # EDINET is the other case. Its issuer names are Japanese, which is faithful to the
    # filing and unreadable to a reader who does not read Japanese: "野村證券株式会社 bought
    # 太陽誘電株式会社 for $1.4B". FMP already prices these tickers and its profile carries
    # the English name, so the display name is swapped when the filing's name is CJK and
    # the vendor's is not. The filing's own name is kept in raw_payload rather than thrown
    # away, because it is the authoritative string and provenance may need it.
    if snapshot and snapshot.company_name:
        current = (event.issuer_name or "").strip()
        if not current or current.upper() == event.ticker.upper():
            event.issuer_name = snapshot.company_name
        elif is_cjk(current) and not is_cjk(snapshot.company_name):
            event.raw_payload["issuer_name_local"] = current
            event.issuer_name = snapshot.company_name
    return event


def enrich_events(
    events: list[NormalizedEvent], provider: MarketContextProvider
) -> list[NormalizedEvent]:
    """Enrich a batch. Cost is one snapshot per distinct ticker, not per event."""
    for event in events:
        enrich_event(event, provider)
    return events
