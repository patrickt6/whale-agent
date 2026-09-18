"""Finnhub insider transactions adapter (Form 3/4/5, SEDI and other insider filings).

Licensing: commercial vendor; the insider endpoint needs a paid key. Personal ingestion
only, no redistribution. Off by default: runs only when `WHALE_ENABLE_FINNHUB=1` AND
`FINNHUB_API_KEY` is set.

Source of every endpoint and field name below (opened 2026-09-17):
- Docs page: https://finnhub.io/docs/api/insider-transactions
- Machine-readable spec: https://finnhub.io/static/swagger.json
  host `finnhub.io`, basePath `/api/v1`, security `api_key` = query parameter `token`.
  Path `GET /stock/insider-transactions`, query `symbol` (spec: "Leave this param blank
  to get the latest transactions", though it is also marked required), `from`, `to`.
  Response `InsiderTransactions` = {`symbol`, `data`: [`Transactions`]}.
  `Transactions` fields: `symbol`, `name`, `share` (held AFTER the transaction),
  `change` (shares changed; positive suggests BUY, negative SELL), `filingDate`,
  `transactionDate`, `transactionPrice` ("Average transaction price"),
  `transactionCode` (SEC Form 4 code).

Valuation rule: every figure comes from a field. Shares = |`change`|, price =
`transactionPrice`. The response has no dollar-value field and no filing URL, so no
dollar amount and no source_url is set here. The price is an average, so a value derived
from it is marked an estimate.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.ingestion._http import request_json
from whale_agent.ingestion.base import Adapter
from whale_agent.ingestion.vendor_types import (
    VendorValue,
    coerce_price_per_share,
    coerce_share_count,
)
from whale_agent.models.enums import (
    FilerType,
    Jurisdiction,
    PriceSource,
    TransactionType,
)
from whale_agent.models.event import NormalizedEvent


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class FinnhubInsiderAdapter(Adapter):
    """`GET /stock/insider-transactions` mapped to `NormalizedEvent`."""

    name = "finnhub_insider"
    jurisdiction = Jurisdiction.US.value
    path = "/stock/insider-transactions"

    # SEC Form 4 transaction codes, the same subset FMP maps.
    _CODE_MAP = {
        "P": TransactionType.OPEN_MARKET_BUY,
        "S": TransactionType.OPEN_MARKET_SELL,
        "A": TransactionType.GRANT,
        "M": TransactionType.OPTION_EXERCISE,
    }

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _get(self, params: dict) -> Any:
        key = self.settings.require("finnhub_api_key")
        return request_json(
            f"{self.settings.finnhub_base_url.rstrip('/')}{self.path}",
            params={**params, "token": key},
            settings=self.settings,
        )

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        symbols = kwargs.pop("symbols", None) or [kwargs.pop("symbol", "")]
        extra = {k: v for k, v in kwargs.items() if k in ("from", "to") and v}
        for symbol in symbols:
            yield self._get({"symbol": symbol or "", **extra})

    def parse(self, raw: Any) -> list[dict]:
        if not isinstance(raw, dict):
            return []
        top_symbol = raw.get("symbol")
        rows = []
        for row in raw.get("data") or []:
            if isinstance(row, dict):
                # `symbol` is on each row in the schema; fall back to the envelope's.
                rows.append({"symbol": top_symbol, **row} if top_symbol else row)
        return rows

    def normalize(self, parsed: dict) -> NormalizedEvent:
        txn_date = _parse_date(parsed.get("transactionDate")) or date.today()
        filed = _parse_date(parsed.get("filingDate")) or txn_date
        code = str(parsed.get("transactionCode") or "").upper().strip()
        change = _int(parsed.get("change"))
        txn_type = self._CODE_MAP.get(code, TransactionType.OTHER)
        shares = (
            coerce_share_count(abs(change), vendor=self.name, field="change", ctx=parsed)
            if change
            else VendorValue(None)
        )
        priced = coerce_price_per_share(
            parsed.get("transactionPrice"),
            vendor=self.name,
            field="transactionPrice",
            ctx=parsed,
        )
        price = priced.value
        symbol = parsed.get("symbol") or None
        event = NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            issuer_name=symbol or "UNKNOWN",
            ticker=symbol,
            filer_name=parsed.get("name") or "UNKNOWN",
            filer_type=FilerType.INSIDER.value,
            transaction_type=txn_type,
            transaction_date=txn_date,
            disclosure_date=filed,
            native_currency="USD",
            share_count=shares.value,
            price_used=price,
            # The documented field is an average price stated by the filing data.
            price_source=PriceSource.FILING_STATED if price else PriceSource.NOT_PRICED,
            usd_value_is_estimate=True,
            raw_payload=parsed,
        )
        return shares.record_on(priced.record_on(event))
