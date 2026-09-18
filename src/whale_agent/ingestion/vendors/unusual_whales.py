"""Unusual Whales congressional trades adapter.

Licensing: commercial vendor, paid API subscription. Personal ingestion only, no
redistribution. Off by default: runs only when `WHALE_ENABLE_UNUSUAL_WHALES=1` AND
`UNUSUAL_WHALES_API_KEY` is set.

Source of every endpoint and field name below (opened 2026-09-17):
- OpenAPI spec: https://api.unusualwhales.com/api/openapi (YAML)
  server `https://api.unusualwhales.com`, security scheme `authorization`:
  HTTP bearer (`Authorization: Bearer <API_TOKEN>`).
  Path `GET /api/congress/recent-trades` (operationId
  `PublicApi.CongressController.congress_recent_trades`), query `limit` (default 100,
  max 200), `date`, `ticker`. 200 response schema `Senate Stock`; its example is
  {"data": [...]} with row fields `amounts` ("The reported amount range of the
  transaction", e.g. "$15,001 - $50,000"), `filed_at_date`, `transaction_date`,
  `txn_type` (enum Buy, Sell (partial), Purchase, Sale (Partial), Receive, Sale (Full),
  Sell (PARTIAL), Sell, Exchange), `name`, `reporter`, `member_type`, `politician_id`,
  `ticker`, `issuer` ("The person who executed the transaction", e.g. spouse, joint),
  `notes`, `is_active`.
  Note: the spec's declared types for `member_type` (boolean) and `is_active` (string)
  conflict with its own example values ("house", true). This adapter reads both as
  opaque values and does not depend on either type.

Valuation rule: the amount is a disclosed range, so the LOWER bound is used and the value
is marked an estimate, the same as the FMP and Quiver congress adapters.
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
from whale_agent.models.enums import FilerType, Jurisdiction, TransactionType
from whale_agent.models.event import NormalizedEvent


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class UnusualWhalesCongressAdapter(Adapter):
    """`GET /api/congress/recent-trades` mapped to `NormalizedEvent`."""

    name = "unusual_whales_congress"
    jurisdiction = Jurisdiction.US.value
    path = "/api/congress/recent-trades"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _get(self, params: dict) -> Any:
        key = self.settings.require("unusual_whales_api_key")
        return request_json(
            f"{self.settings.unusual_whales_base_url.rstrip('/')}{self.path}",
            params=params,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            settings=self.settings,
        )

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        params = {k: v for k, v in kwargs.items() if k in ("limit", "date", "ticker") and v}
        params.setdefault("limit", 200)  # documented maximum
        yield self._get(params)

    def parse(self, raw: Any) -> list[dict]:
        if isinstance(raw, dict):
            raw = raw.get("data") or []
        return [row for row in (raw or []) if isinstance(row, dict)]

    def normalize(self, parsed: dict) -> NormalizedEvent:
        txn_date = _parse_date(parsed.get("transaction_date")) or date.today()
        filed = _parse_date(parsed.get("filed_at_date")) or txn_date
        raw_type = str(parsed.get("txn_type") or "").lower()
        if raw_type in ("buy", "purchase"):
            txn_type = TransactionType.OPEN_MARKET_BUY
        elif raw_type.startswith("sell") or raw_type.startswith("sale"):
            txn_type = TransactionType.OPEN_MARKET_SELL
        else:  # Receive, Exchange, or anything not in the documented enum
            txn_type = TransactionType.OTHER
        valued = coerce_usd_amount(
            range_lower_bound(parsed.get("amounts")),
            vendor=self.name,
            field="amounts",
            ctx=parsed,
        )
        member = parsed.get("name") or parsed.get("reporter") or "UNKNOWN"
        event = NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            issuer_name=parsed.get("ticker") or "UNKNOWN",
            ticker=parsed.get("ticker"),
            filer_name=member,
            filer_id=parsed.get("politician_id") or None,
            filer_type=FilerType.INDIVIDUAL.value,
            transaction_type=txn_type,
            transaction_date=txn_date,
            disclosure_date=filed,
            native_currency="USD",
            native_amount=valued.value,
            usd_value_is_estimate=True,  # a range lower bound, not an exact figure
            raw_payload=parsed,
        )
        return valued.record_on(event)
