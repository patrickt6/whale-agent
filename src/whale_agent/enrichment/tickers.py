"""CIK to ticker resolution: the missing key that switches enrichment on for EDGAR.

SEC ownership documents identify the issuer by CIK and nothing else -- there is no
ticker anywhere in a Form 4, and a 13D cover page carries a CUSIP at best. Every
enrichment we have (`price.py`, `market_context.py`) is keyed by ticker, so without a
translation step every EDGAR-sourced event silently skips enrichment while the
vendor-sourced ones get it. That is worse than having no enrichment at all: the
scoring model's small-cap tilt and percent-of-float term then apply to half the corpus
and the ranking still looks complete.

The mapping comes from SEC's own `company_tickers.json`, which is keyless, covers every
registrant with a listed ticker, and is one file rather than one lookup per issuer. It
only requires a real contact string in the User-Agent, which `settings.sec_user_agent`
already supplies for the rest of the EDGAR path. FMP's `/stable/profile?cik=` was tried
first and returns an empty body for a valid CIK, so it is not an option.

The mapping changes only when a registrant lists, delists, or changes symbol, so it is
fetched at most once per process and cached on disk between runs. Resolution itself is a
dict lookup and never touches the network; a failed fetch degrades to "no tickers"
rather than raising, because a missing ticker must cost an event its enrichment, not the
digest its run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from whale_agent.config import Settings, get_settings
from whale_agent.enrichment.plausibility import is_placeholder_issuer
from whale_agent.errors import SourceUnavailableError
from whale_agent.ingestion._http import polite_headers, request_text

if TYPE_CHECKING:  # pragma: no cover
    from whale_agent.models.event import NormalizedEvent

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# The file is a few hundred kilobytes and changes on the order of days. A week keeps a
# daily run offline almost always while still picking up new listings on its own.
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 3600


def normalize_cik(value: object) -> str | None:
    """Reduce any CIK spelling to the canonical zero-padded 10-digit string.

    CIKs arrive three ways in this codebase: zero-padded strings from ownership XML
    (`0000320193`), bare integers from SEC's own JSON (`320193`), and unpadded strings
    from vendor payloads. Comparing those directly produces silent misses, which look
    exactly like an unknown issuer, so every path through this module normalises first.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Tolerate the "CIK0000320193" prefix some EDGAR URLs and vendors use.
    if text.upper().startswith("CIK"):
        text = text[3:].lstrip("-").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(10)


def parse_company_tickers(payload: object) -> dict[str, str]:
    """Parse SEC's `company_tickers.json` into `{padded cik: ticker}`.

    The published shape is an index-keyed object of
    `{"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}` rows; a plain list of
    the same rows is accepted too, since that is the shape a caller would most naturally
    hand-write in a fixture. Rows missing either field are skipped rather than defaulted:
    a wrong ticker would attach another company's market cap to this event.

    A CIK with several share classes appears more than once. The first row wins, which is
    SEC's own ordering by market capitalisation, so `GOOGL` beats `GOOG`.
    """
    if isinstance(payload, Mapping):
        rows: list[object] = list(payload.values())
    elif isinstance(payload, list):
        rows = list(payload)
    else:
        return {}

    mapping: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cik = normalize_cik(row.get("cik_str") or row.get("cik"))
        ticker = row.get("ticker") or row.get("symbol")
        if not cik or not ticker:
            continue
        symbol = str(ticker).strip().upper()
        if symbol and cik not in mapping:
            mapping[cik] = symbol
    return mapping


# Tokens that stay uppercase when a shouted name is cased down. Legal suffixes and the
# handful of forms that are genuinely initialisms rather than words.
_KEEP_UPPER = {
    "LLC",
    "L.L.C.",
    "LP",
    "L.P.",
    "LLP",
    "PLC",
    "NV",
    "N.V.",
    "SA",
    "S.A.",
    "AG",
    "AB",
    "ETF",
    "REIT",
    "USA",
    "US",
    "UK",
    "III",
    "II",
    "IV",
    "&",
}
_KEEP_LOWER = {"of", "and", "the", "for", "de", "van", "der"}


def _readable_company_name(title: str) -> str:
    """Case a shouted SEC name for prose, and leave every other name alone.

    SEC stores a large share of names in capitals ("FIVE STAR BANCORP"). In a filing
    field that is invisible; in a headline it shouts. Only names with no lowercase letter
    at all are touched, so a deliberate capitalisation someone actually uses -- "AMG BBH
    Asset-Backed Credit Fund, LLC", "51Talk" -- passes through untouched.
    """
    if any(ch.islower() for ch in title):
        return title
    words = title.split()
    out: list[str] = []
    for i, word in enumerate(words):
        bare = word.strip(".,")
        if word.upper() in _KEEP_UPPER or bare.upper() in _KEEP_UPPER:
            out.append(word)
        elif i and word.lower() in _KEEP_LOWER:
            out.append(word.lower())
        else:
            out.append(word.capitalize())
    return " ".join(out)


def parse_company_names(payload: object) -> dict[str, str]:
    """Parse the same `company_tickers.json` into `{ticker: legal name}`.

    A separate pass rather than a second return value from `parse_company_tickers`, so
    that the CIK-to-ticker map keeps its narrow contract and callers that only need the
    symbol are not handed a structure they have to unpack.

    This exists because the alternative source of company names is a vendor field that is
    frequently null: FMP's insider feed returns `companyName` as None often enough that
    issuers routinely arrive named after their own ticker, and a research note headlined
    "FSBC" reads as a bug to the one person it is written for. SEC publishes the name in a
    file already downloaded for the ticker map, for free, and is the authority on it.

    Same skip-do-not-default rule as the ticker map: a name attached to the wrong symbol
    is worse than no name, because it is wrong confidently.
    """
    if isinstance(payload, Mapping):
        rows: list[object] = list(payload.values())
    elif isinstance(payload, list):
        rows = list(payload)
    else:
        return {}

    names: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ticker = row.get("ticker") or row.get("symbol")
        title = row.get("title") or row.get("name")
        if not ticker or not title:
            continue
        symbol = str(ticker).strip().upper()
        label = _readable_company_name(str(title).strip())
        # First row wins, matching the ticker map: SEC orders by market cap, so the
        # primary share class supplies the name.
        if symbol and label and symbol not in names:
            names[symbol] = label
    return names


def fill_issuer_names(
    events: list[NormalizedEvent], names: Mapping[str, str]
) -> list[NormalizedEvent]:
    """Replace ticker-shaped issuer names with the company's actual name.

    The rule is the one `market_context` already enforces and it is the important part: a
    name the filing itself supplied is authoritative and is never overwritten, however
    much better the SEC's version might look. Only an issuer whose name is nothing more
    than its own symbol, or a vendor placeholder, is filled in.

    Returns new events rather than mutating: the stored payload is the record of what the
    source said, and display-time resolution is not a correction to it.
    """
    if not names:
        return events
    out: list[NormalizedEvent] = []
    for ev in events:
        ticker = (ev.ticker or "").strip().upper()
        current = (ev.issuer_name or "").strip()
        looks_unresolved = is_placeholder_issuer(current) or (
            bool(ticker) and current.upper() == ticker
        )
        resolved = names.get(ticker) if looks_unresolved else None
        out.append(ev.model_copy(update={"issuer_name": resolved}) if resolved else ev)
    return out


class TickerResolver(Protocol):
    def resolve_cik(self, cik: object) -> str | None:
        """Ticker for `cik`, or None when the registrant has no listed symbol.

        Must never raise and must never make a network call per event; callers treat
        None as "leave this event unenriched" rather than as an error.
        """
        ...


class StaticTickerResolver:
    """In-memory mapping for tests and offline demos. Mirrors `StaticPriceProvider`."""

    def __init__(self, mapping: Mapping[object, str] | None = None) -> None:
        self._map: dict[str, str] = {}
        for cik, ticker in (mapping or {}).items():
            key = normalize_cik(cik)
            if key and ticker:
                self._map[key] = str(ticker).strip().upper()

    def resolve_cik(self, cik: object) -> str | None:
        key = normalize_cik(cik)
        return self._map.get(key) if key else None


class NullTickerResolver:
    """Resolves nothing. The explicit stand-in for "ticker lookup is switched off"."""

    def resolve_cik(self, cik: object) -> str | None:  # noqa: ARG002 - protocol shape
        return None


class SecTickerResolver:
    """CIK to ticker from SEC's published `company_tickers.json`.

    The whole file is loaded once and held in memory: the mapping is small enough that
    per-issuer lookups are free, and one request covers a digest run of any size. The
    load is attempted at most once per instance even when it fails, so a SEC outage costs
    one timeout rather than one per event.

    Disk cache is best-effort in both directions -- an unreadable or unwritable cache
    file degrades to a fetch or to no cache at all, never to an exception, because this
    is an enrichment nicety and not something the digest may die on.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache_path: Path | str | None = None,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        user_agent: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.user_agent = user_agent or self.settings.sec_user_agent
        self.ttl_seconds = ttl_seconds
        self.cache_path = (
            Path(cache_path)
            if cache_path is not None
            else Path.home() / ".cache" / "whale-agent" / "sec_company_tickers.json"
        )
        self._map: dict[str, str] | None = None
        self.fetch_count = 0  # exposed so tests can assert "at most one network call"

    # -- cache ------------------------------------------------------------------
    def _read_cache(self) -> dict[str, str] | None:
        try:
            if not self.cache_path.is_file():
                return None
            if time.time() - self.cache_path.stat().st_mtime > self.ttl_seconds:
                return None
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        mapping = parse_company_tickers(payload)
        return mapping or None

    def _write_cache(self, payload: object) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass  # a cache we cannot write is a slower next run, not a failure

    # -- loading ----------------------------------------------------------------
    def _fetch(self) -> dict[str, str]:
        self.fetch_count += 1
        body = request_text(
            SEC_COMPANY_TICKERS_URL,
            headers=polite_headers(self.user_agent),
            settings=self.settings,
        )
        payload = json.loads(body)
        mapping = parse_company_tickers(payload)
        if mapping:
            self._write_cache(payload)
        return mapping

    def load(self) -> dict[str, str]:
        """Return the mapping, fetching it at most once for the life of this object."""
        if self._map is not None:
            return self._map
        cached = self._read_cache()
        if cached is not None:
            self._map = cached
            return self._map
        try:
            self._map = self._fetch()
        except (SourceUnavailableError, ValueError, OSError):
            # Remember the failure as an empty mapping: retrying per event would turn
            # one SEC outage into hundreds of timed-out requests.
            self._map = {}
        return self._map

    def company_names(self) -> dict[str, str]:
        """`{ticker: legal name}` from the same file, for display-time name resolution.

        Calls `load()` first so the cache is populated by the path that already handles
        fetch failure; this method then only ever reads the file. A missing or unreadable
        cache yields an empty map, which `fill_issuer_names` treats as "leave every name
        alone" -- the same degrade-do-not-raise contract as ticker resolution.
        """
        self.load()
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return parse_company_names(payload)

    # -- TickerResolver ---------------------------------------------------------
    def resolve_cik(self, cik: object) -> str | None:
        key = normalize_cik(cik)
        if not key:
            return None
        return self.load().get(key)


class CachingTickerResolver:
    """Wraps another resolver with a per-CIK memo, including negative results.

    Useful when the delegate is expensive or unreliable; `SecTickerResolver` is already
    a dict lookup, so this exists for composing a fallback chain without re-asking a
    slow source for a CIK it has already failed to answer.
    """

    def __init__(self, delegate: TickerResolver) -> None:
        self._delegate = delegate
        self._memo: dict[str, str | None] = {}

    def resolve_cik(self, cik: object) -> str | None:
        key = normalize_cik(cik)
        if not key:
            return None
        if key not in self._memo:
            try:
                self._memo[key] = self._delegate.resolve_cik(key)
            except Exception:  # noqa: BLE001 - a resolver must never sink a run
                self._memo[key] = None
        return self._memo[key]


_DEFAULT: SecTickerResolver | None = None


def default_ticker_resolver(
    settings: Settings | None = None, *, user_agent: str | None = None
) -> SecTickerResolver:
    """Process-wide resolver, so several adapters in one run share the single fetch."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SecTickerResolver(settings, user_agent=user_agent)
    return _DEFAULT
