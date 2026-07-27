"""Financial Modeling Prep adapters: insider trades, congressional trades, 13F holdings.

Licensing: commercial vendor, paid subscription. Redistribution is not permitted under
FMP's terms; this is personal ingestion only.

Optional by construction. Nothing here runs unless `FMP_API_KEY` is set and
`WHALE_ENABLE_FMP` is not turned off, so the digest is fully functional on free sources
alone and gains FMP coverage the day a plan is bought. `parse()`/`normalize()` are
pure and golden-file tested; only `fetch()` touches the network.

One deliberate choice worth knowing about: congressional disclosures report a dollar
*range* ("$1,001 - $15,000"), never an exact amount. This adapter takes the LOWER bound
and marks the value an estimate. A midpoint would be a number nobody disclosed, and the
provenance gate exists precisely to stop that kind of invention.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.enrichment.roles import tidy_role
from whale_agent.errors import NotConfiguredError
from whale_agent.ingestion._http import request_json
from whale_agent.ingestion.base import Adapter
from whale_agent.ingestion.vendor_types import (
    VendorValue,
    coerce_price_per_share,
    coerce_share_count,
    coerce_usd_amount,
)
from whale_agent.models.enums import (
    FilerType,
    Jurisdiction,
    PriceSource,
    TransactionType,
)
from whale_agent.models.event import NormalizedEvent

_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Last resort: a leading ISO date with anything trailing it.
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def range_lower_bound(text: str | None) -> float | None:
    """Lowest disclosed dollar figure in a range string like "$1,001 - $15,000".

    Returns the lower bound, never a midpoint: the range endpoints are disclosed facts,
    anything between them is not.
    """
    if not text:
        return None
    matches = _MONEY.findall(str(text))
    if not matches:
        return None
    values = [float(m.replace(",", "")) for m in matches]
    return min(values)


class _FmpBase(Adapter):
    """Shared credential handling and request plumbing for the FMP endpoints."""

    jurisdiction = Jurisdiction.US.value
    path: str = ""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _get(self, path: str, params: dict | None = None) -> Any:
        key = self.settings.require("fmp_api_key")
        return request_json(
            f"{self.settings.fmp_base_url.rstrip('/')}{path}",
            params={**(params or {}), "apikey": key},
            settings=self.settings,
        )

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        params = {k: v for k, v in kwargs.items() if v is not None}
        params.setdefault("limit", 100)
        yield self._get(self.path, params)

    def parse(self, raw: Any) -> list[dict]:
        """FMP returns a bare JSON array for every endpoint used here."""
        if isinstance(raw, dict):
            raw = raw.get("data") or raw.get("results") or []
        return [row for row in (raw or []) if isinstance(row, dict)]


class FmpInsiderAdapter(_FmpBase):
    """FMP insider-trading search: SEC Form 4 data, pre-parsed and ticker-keyed.

    Overlaps `us_sec.py` deliberately. FMP resolves the ticker (which EDGAR's ownership
    XML does not carry) and backfills history, so the two sources complement rather than
    duplicate each other -- dedup on the stable key collapses any genuine overlap.
    """

    name = "fmp_insider"
    path = "/stable/insider-trading/search"

    # FMP repeats the SEC transaction codes but sometimes spells them out.
    _TYPE_MAP = {
        "P": TransactionType.OPEN_MARKET_BUY,
        "P-PURCHASE": TransactionType.OPEN_MARKET_BUY,
        "S": TransactionType.OPEN_MARKET_SELL,
        "S-SALE": TransactionType.OPEN_MARKET_SELL,
        "A": TransactionType.GRANT,
        "A-AWARD": TransactionType.GRANT,
        "M": TransactionType.OPTION_EXERCISE,
        "M-EXEMPT": TransactionType.OPTION_EXERCISE,
        "G": TransactionType.OTHER,
        "F": TransactionType.OTHER,
    }

    def normalize(self, parsed: dict) -> NormalizedEvent:
        txn_date = _parse_date(parsed.get("transactionDate")) or date.today()
        filed = _parse_date(parsed.get("filingDate")) or txn_date
        code = str(parsed.get("transactionType") or "").upper().strip()
        txn_type = self._TYPE_MAP.get(code, TransactionType.OTHER)
        # Bounded at the boundary: this is the field that carried a transaction total
        # into a $1.6 quadrillion position. A rejected price is absent, never clamped.
        priced = coerce_price_per_share(
            parsed.get("price"), vendor=self.name, field="price", ctx=parsed
        )
        shares = coerce_share_count(
            parsed.get("securitiesTransacted"),
            vendor=self.name,
            field="securitiesTransacted",
            ctx=parsed,
        )
        price = priced.value
        event = NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            source_url=parsed.get("url") or parsed.get("link"),
            issuer_name=parsed.get("companyName") or parsed.get("symbol") or "UNKNOWN",
            issuer_id=str(parsed.get("companyCik") or "") or None,
            ticker=parsed.get("symbol"),
            filer_name=parsed.get("reportingName") or "UNKNOWN",
            filer_id=str(parsed.get("reportingCik") or "") or None,
            filer_type=FilerType.INSIDER.value,
            # FMP has always sent this and it was only ever parked in raw_payload.
            filer_role=tidy_role(parsed.get("typeOfOwner")),
            transaction_type=txn_type,
            transaction_date=txn_date,
            disclosure_date=filed,
            native_currency="USD",
            share_count=shares.value,
            price_used=price,
            # FMP echoes the Form 4's stated per-share price, so this is filing-stated
            # data even though it arrives through a vendor.
            price_source=PriceSource.FILING_STATED if price else PriceSource.NOT_PRICED,
            raw_payload=parsed,
        )
        return shares.record_on(priced.record_on(event))


class FmpCongressAdapter(_FmpBase):
    """FMP Senate/House STOCK Act disclosures.

    `fetch(chamber="senate"|"house")`. Amounts are ranges (see module docstring), so
    almost none of these clear the $5M gate -- that is the correct outcome, not a bug.
    They still earn their place as context in the weekly report.
    """

    name = "fmp_congress"
    path = "/stable/senate-latest"

    _PATHS = {"senate": "/stable/senate-latest", "house": "/stable/house-latest"}

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        chamber = str(kwargs.pop("chamber", "senate")).lower()
        params = {k: v for k, v in kwargs.items() if v is not None}
        yield self._get(self._PATHS.get(chamber, self.path), params)

    def normalize(self, parsed: dict) -> NormalizedEvent:
        txn_date = _parse_date(parsed.get("transactionDate")) or date.today()
        disclosed = _parse_date(parsed.get("disclosureDate")) or txn_date
        raw_type = str(parsed.get("type") or "").lower()
        if "purchase" in raw_type or raw_type.startswith("buy"):
            txn_type = TransactionType.OPEN_MARKET_BUY
        elif "sale" in raw_type or "sell" in raw_type:
            txn_type = TransactionType.OPEN_MARKET_SELL
        else:
            txn_type = TransactionType.OTHER
        name = (
            " ".join(p for p in [parsed.get("firstName"), parsed.get("lastName")] if p)
            or parsed.get("representative")
            or parsed.get("office")
            or "UNKNOWN"
        )
        return NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            source_url=parsed.get("link") or parsed.get("url"),
            issuer_name=parsed.get("assetDescription") or parsed.get("symbol") or "UNKNOWN",
            ticker=parsed.get("symbol"),
            filer_name=name,
            filer_id=parsed.get("office"),
            filer_type=FilerType.INDIVIDUAL.value,
            transaction_type=txn_type,
            transaction_date=txn_date,
            disclosure_date=disclosed,
            native_currency="USD",
            native_amount=range_lower_bound(parsed.get("amount")),
            # A range's lower bound is disclosed, but the position size is not exact.
            usd_value_is_estimate=True,
            raw_payload=parsed,
        )


class FmpInstitutionalAdapter(_FmpBase):
    """FMP 13F institutional-holdings extract (Ultimate tier).

    13F is quarterly and up to 135 days stale -- these are weekly-report
    context, not instant alerts.

    This endpoint returns a position SNAPSHOT and nothing else: the live rows carry
    `date`, `filingDate`, `cik`, `securityCusip`, `symbol`, `nameOfIssuer`, `shares`,
    `value` and the two links, with no change or delta field anywhere. So a row here
    cannot say whether a holding is new -- that is `FmpInstitutionalHolderAdapter`'s job
    -- and the transaction type is the honest `OTHER`.

    It used to also read `sharesNumber` / `changeInSharesNumber` / `investorName` and
    branch on a quarter-over-quarter delta. Those keys do not exist on this endpoint and
    never did; the branch was unreachable in production and kept alive only by a test
    written against the imagined schema, which is the worst kind of dead code because it
    reads as tested behaviour. The filer is named by CIK because the payload has no name
    field -- the caller already knows which manager it asked for.

    **Keyed by the filer's CIK, not by ticker.** This endpoint answers "what does this
    manager hold", not "who holds this stock" -- passing `symbol` returns
    `HTTP 400: Invalid or missing query parameter - cik`. Call it once per manager you
    care about (Berkshire is `0001067983`), which is also why it is not in the daily
    registry: there is no useful "all filers" call to make.
    """

    name = "fmp_institutional"
    path = "/stable/institutional-ownership/extract"

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        cik = kwargs.pop("cik", None)
        if not cik:
            raise NotConfiguredError(
                "FmpInstitutionalAdapter.fetch requires cik=<filer CIK>; the 13F extract "
                "is per-manager, not per-symbol."
            )
        params = {k: v for k, v in kwargs.items() if v is not None}
        params["cik"] = str(cik)
        yield self._get(self.path, params)

    def normalize(self, parsed: dict) -> NormalizedEvent:
        as_of = _parse_date(parsed.get("date")) or date.today()
        filed = _parse_date(parsed.get("filingDate")) or as_of
        # The whole holding is all this endpoint knows, and a holding is not a purchase,
        # so it is not typed as one.
        shares = coerce_share_count(
            parsed.get("shares"), vendor=self.name, field="shares", ctx=parsed
        )
        amount = coerce_usd_amount(
            parsed.get("value"), vendor=self.name, field="value", ctx=parsed
        )
        cik = str(parsed.get("cik") or "") or None
        event = NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            source_url=parsed.get("finalLink") or parsed.get("link"),
            issuer_name=parsed.get("nameOfIssuer") or parsed.get("symbol") or "UNKNOWN",
            issuer_id=str(parsed.get("securityCusip") or "") or None,
            ticker=parsed.get("symbol"),
            # No name field on this payload; the CIK is the only identity it carries.
            filer_name=f"CIK {cik}" if cik else "UNKNOWN",
            filer_id=cik,
            filer_type=FilerType.FUND.value,
            transaction_type=TransactionType.OTHER,
            transaction_date=as_of,
            disclosure_date=filed,
            native_currency="USD",
            native_amount=amount.value,
            share_count=shares.value,
            usd_value_is_estimate=True,
            raw_payload=parsed,
        )
        return shares.record_on(amount.record_on(event))


class FmpInstitutionalHolderAdapter(_FmpBase):
    """FMP 13F holder analytics: the per-holder DELTAS for one symbol.

    `/stable/institutional-ownership/extract-analytics/holder?symbol=&year=&quarter=`.
    Keyed by symbol, unlike the snapshot extract above, and it is the only endpoint that
    answers the question scoring actually cares about: did this manager just open a
    position, or top up one they have held for years. `isNew` / `isSoldOut` are booleans
    the vendor computes against the prior quarter, so we take them at face value rather
    than re-deriving them from share counts and inheriting their rounding.

    The filer name is occasionally blank in the live feed (a filer whose CIK FMP has not
    mapped yet); those rows still carry usable share and price data, so they normalize
    with an UNKNOWN name rather than being dropped.
    """

    name = "fmp_institutional_holder"
    path = "/stable/institutional-ownership/extract-analytics/holder"

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        symbol = kwargs.pop("symbol", None)
        if not symbol:
            raise NotConfiguredError(
                "FmpInstitutionalHolderAdapter.fetch requires symbol=<ticker>; the "
                "holder analytics endpoint is per-symbol, not per-manager."
            )
        params = {k: v for k, v in kwargs.items() if v is not None}
        params["symbol"] = str(symbol).upper()
        yield self._get(self.path, params)

    def normalize(self, parsed: dict) -> NormalizedEvent:
        as_of = _parse_date(parsed.get("date")) or date.today()
        filed = _parse_date(parsed.get("filingDate")) or as_of
        change = _float(parsed.get("changeInSharesNumber")) or 0.0
        # An unchanged position is a legitimate zero here, so only a non-zero delta is
        # put through the share-count bounds; zero stays None, as it already did.
        moved = (
            coerce_share_count(
                abs(change),
                vendor=self.name,
                field="changeInSharesNumber",
                ctx=parsed,
            )
            if change
            else VendorValue(None)
        )
        amount = coerce_usd_amount(
            parsed.get("changeInMarketValue"),
            vendor=self.name,
            field="changeInMarketValue",
            ctx=parsed,
        )
        is_new = bool(parsed.get("isNew"))
        is_sold_out = bool(parsed.get("isSoldOut"))
        if is_new:
            txn_type = TransactionType.FUND_NEW_POSITION
        elif change > 0:
            txn_type = TransactionType.FUND_ADD_POSITION
        else:
            txn_type = TransactionType.OTHER
        event = NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            issuer_name=parsed.get("securityName") or parsed.get("symbol") or "UNKNOWN",
            issuer_id=str(parsed.get("securityCusip") or "") or None,
            ticker=parsed.get("symbol"),
            filer_name=parsed.get("investorName") or "UNKNOWN",
            filer_id=str(parsed.get("cik") or "") or None,
            filer_type=FilerType.FUND.value,
            transaction_type=txn_type,
            transaction_date=as_of,
            disclosure_date=filed,
            native_currency="USD",
            # Only the quarter's change is news; the rest was reported last quarter.
            native_amount=amount.value,
            share_count=moved.value,
            # `avgPricePaid` is a quarter-average, not the price of any one trade, so
            # anything valued from it is an estimate by construction.
            usd_value_is_estimate=True,
            raw_payload=parsed,
        )
        event.context.is_new_position = is_new
        event.context.is_sold_out = is_sold_out
        event.context.change_in_shares_pct = _float(
            parsed.get("changeInSharesNumberPercentage")
        )
        event.context.avg_price_paid = _float(parsed.get("avgPricePaid"))
        return moved.record_on(amount.record_on(event))
