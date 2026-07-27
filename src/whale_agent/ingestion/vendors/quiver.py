"""Quiver Quantitative adapters: congressional trading, lobbying, government contracts.

Licensing: commercial vendor, paid subscription (~$30-75/mo). Personal ingestion only,
no redistribution.

Optional by construction: nothing runs without `QUIVER_QUANT_API_KEY`, so the digest is
complete on free sources and gains this coverage the day someone subscribes.

Scope split, per the roadmap: congressional TRADES become `NormalizedEvent`s and flow
through the normal gate; lobbying spend and government contracts become
`ContextAnnotation`s bound for the weekly report. They are quarterly company-level
facts with no filer taking a position, so routing them into the daily $5M pipeline
would be a category error.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.ingestion._http import request_json
from whale_agent.ingestion.base import Adapter
from whale_agent.ingestion.vendor_types import coerce_usd_amount
from whale_agent.ingestion.vendors.fmp import range_lower_bound
from whale_agent.models.annotation import ContextAnnotation
from whale_agent.models.enums import FilerType, Jurisdiction, TransactionType
from whale_agent.models.event import NormalizedEvent


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:19]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
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


class _QuiverBase(Adapter):
    jurisdiction = Jurisdiction.US.value
    path: str = ""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _get(self, path: str, params: dict | None = None) -> Any:
        key = self.settings.require("quiver_api_key")
        return request_json(
            f"{self.settings.quiver_base_url.rstrip('/')}{path}",
            params=params,
            headers={
                "Authorization": f"Token {key}",
                "Accept": "application/json",
            },
            settings=self.settings,
        )

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        yield self._get(self.path, {k: v for k, v in kwargs.items() if v is not None})

    def parse(self, raw: Any) -> list[dict]:
        if isinstance(raw, dict):
            raw = raw.get("data") or raw.get("results") or []
        return [row for row in (raw or []) if isinstance(row, dict)]


class QuiverCongressAdapter(_QuiverBase):
    """STOCK Act congressional trades from Quiver's live congress-trading feed.

    Like FMP's version, amounts are disclosed as ranges and the lower bound is used --
    never a midpoint. Overlap with `FmpCongressAdapter` is intentional and harmless:
    identical trades collapse on the stable dedup key.
    """

    name = "quiver_congress"
    path = "/beta/live/congresstrading"

    def normalize(self, parsed: dict) -> NormalizedEvent:
        txn_date = _parse_date(parsed.get("TransactionDate")) or date.today()
        reported = _parse_date(parsed.get("ReportDate")) or txn_date
        raw_type = str(parsed.get("Transaction") or "").lower()
        if "purchase" in raw_type or "buy" in raw_type:
            txn_type = TransactionType.OPEN_MARKET_BUY
        elif "sale" in raw_type or "sell" in raw_type:
            txn_type = TransactionType.OPEN_MARKET_SELL
        else:
            txn_type = TransactionType.OTHER
        # `Amount` is sometimes a plain number and sometimes a range string.
        amount = _float(parsed.get("Amount"))
        if amount is None:
            amount = range_lower_bound(parsed.get("Amount") or parsed.get("Range"))
        # Bounded at the boundary, like the FMP path: a disclosure bracket is a small
        # number by construction, so anything past the absolute ceiling is a field
        # holding the wrong quantity rather than a very rich legislator.
        valued = coerce_usd_amount(amount, vendor=self.name, field="Amount", ctx=parsed)
        chamber = parsed.get("House") or parsed.get("Chamber") or ""
        member = parsed.get("Representative") or parsed.get("Senator") or "UNKNOWN"
        event = NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            issuer_name=parsed.get("Company") or parsed.get("Ticker") or "UNKNOWN",
            ticker=parsed.get("Ticker"),
            filer_name=member,
            filer_id=f"{chamber}:{member}".strip(":") or None,
            filer_type=FilerType.INDIVIDUAL.value,
            transaction_type=txn_type,
            transaction_date=txn_date,
            disclosure_date=reported,
            native_currency="USD",
            native_amount=valued.value,
            usd_value_is_estimate=True,  # disclosed as a bracket, not an exact figure
            raw_payload=parsed,
        )
        return valued.record_on(event)


class QuiverLobbyingAdapter(_QuiverBase):
    """Corporate lobbying spend. Produces `ContextAnnotation`, not `NormalizedEvent`.

    `normalize()` raises rather than silently returning something wrong: this adapter
    does not belong in the daily event pipeline, and a caller reaching for it that way
    should find out immediately. Use `annotations()`.
    """

    name = "quiver_lobbying"
    path = "/beta/live/lobbying"

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        ticker = kwargs.pop("ticker", None)
        if ticker:
            yield self._get(f"/beta/historical/lobbying/{ticker.upper()}")
        else:
            yield self._get(self.path, kwargs or None)

    def normalize(self, parsed: dict) -> NormalizedEvent:
        raise NotImplementedError(
            "Lobbying data is weekly-report context, not a whale event. "
            "Call QuiverLobbyingAdapter.annotations() instead."
        )

    def annotations(self, rows: list[dict]) -> list[ContextAnnotation]:
        out: list[ContextAnnotation] = []
        for row in rows:
            as_of = _parse_date(row.get("Date") or row.get("date"))
            if as_of is None:
                continue
            amount = _float(row.get("Amount") or row.get("amount"))
            client = row.get("Client") or row.get("Registrant") or ""
            out.append(
                ContextAnnotation(
                    source=self.name,
                    kind="lobbying",
                    ticker=row.get("Ticker") or row.get("ticker"),
                    issuer_name=row.get("Company") or client or None,
                    as_of=as_of,
                    amount_usd=amount,
                    label=f"lobbying spend{f' via {client}' if client else ''}",
                    raw_payload=row,
                )
            )
        return out


class QuiverGovContractsAdapter(_QuiverBase):
    """Federal government contract awards. Also context, also weekly-report bound."""

    name = "quiver_gov_contracts"
    path = "/beta/live/govcontractsall"

    def normalize(self, parsed: dict) -> NormalizedEvent:
        raise NotImplementedError(
            "Government-contract data is weekly-report context, not a whale event. "
            "Call QuiverGovContractsAdapter.annotations() instead."
        )

    def annotations(self, rows: list[dict]) -> list[ContextAnnotation]:
        out: list[ContextAnnotation] = []
        for row in rows:
            as_of = _parse_date(row.get("Date") or row.get("date"))
            if as_of is None:
                # Quarterly rows sometimes carry Year/Qtr instead of a date; anchor to
                # the first day of the quarter rather than guessing a day.
                year, qtr = row.get("Year"), row.get("Qtr") or row.get("Quarter")
                if year and qtr:
                    as_of = date(int(year), 1 + 3 * (int(qtr) - 1), 1)
                else:
                    continue
            agency = row.get("Agency")
            label = "federal contract award" + (f" ({agency})" if agency else "")
            out.append(
                ContextAnnotation(
                    source=self.name,
                    kind="gov_contract",
                    ticker=row.get("Ticker") or row.get("ticker"),
                    issuer_name=row.get("Company") or agency or None,
                    as_of=as_of,
                    amount_usd=_float(row.get("Amount") or row.get("amount")),
                    label=label,
                    raw_payload=row,
                )
            )
        return out
