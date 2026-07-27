"""Arkham Intelligence adapter: labelled on-chain transfers.

Licensing: commercial API with a free tier; personal ingestion only.

Arkham's value over Whale Alert is the entity layer -- it names the fund, exchange, or
individual behind an address rather than just flagging a big number. That is exactly the
"notable person moved money" signal this product is about, so transfers whose entity
label is missing on both sides are dropped: an unattributed transfer is a number without
a subject.

Same caveat as every crypto source: this is not a disclosed position and it scores below
any real filing.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.ingestion._http import request_json
from whale_agent.ingestion.base import Adapter
from whale_agent.models.enums import FilerType, Jurisdiction, TransactionType
from whale_agent.models.event import NormalizedEvent

_ENTITY_TYPE_TO_FILER = {
    "fund": FilerType.FUND.value,
    "individual": FilerType.INDIVIDUAL.value,
    "cex": FilerType.FUND.value,
    "exchange": FilerType.FUND.value,
    "government": FilerType.SWF.value,
}


def _entity(side: Any) -> tuple[str | None, str | None]:
    """Extract (entity name, entity type) from one side of an Arkham transfer."""
    if not isinstance(side, dict):
        return None, None
    entity = side.get("arkhamEntity") or side.get("entity") or {}
    if not isinstance(entity, dict):
        return None, None
    name = entity.get("name") or None
    kind = (entity.get("type") or "").lower() or None
    return name, kind


def _timestamp(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC).date()
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


class ArkhamAdapter(Adapter):
    name = "arkham"
    jurisdiction = Jurisdiction.CRYPTO.value

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        key = self.settings.require("arkham_api_key")
        params: dict[str, Any] = {
            "usdGte": int(kwargs.get("min_usd") or self.settings.threshold_usd),
            "limit": int(kwargs.get("limit") or 100),
        }
        if kwargs.get("chains"):
            params["chains"] = kwargs["chains"]
        yield request_json(
            f"{self.settings.arkham_base_url.rstrip('/')}/transfers",
            params=params,
            headers={"API-Key": key, "Accept": "application/json"},
            settings=self.settings,
        )

    def parse(self, raw: Any) -> list[dict]:
        rows = raw.get("transfers") if isinstance(raw, dict) else raw
        out: list[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            from_name, from_type = _entity(row.get("fromAddress"))
            to_name, to_type = _entity(row.get("toAddress"))
            if not from_name and not to_name:
                continue  # unattributed on both sides: no subject, no story
            out.append(
                {
                    **row,
                    "from_entity": from_name,
                    "from_entity_type": from_type,
                    "to_entity": to_name,
                    "to_entity_type": to_type,
                }
            )
        return out

    def normalize(self, parsed: dict) -> NormalizedEvent:
        when = (
            _timestamp(parsed.get("blockTimestamp") or parsed.get("timestamp")) or date.today()
        )
        symbol = str(parsed.get("tokenSymbol") or parsed.get("symbol") or "").upper()
        chain = parsed.get("chain") or "unknown chain"
        from_name = parsed.get("from_entity") or "unlabelled wallet"
        to_name = parsed.get("to_entity") or "unlabelled wallet"
        usd = parsed.get("historicalUSD")
        if usd is None:
            usd = parsed.get("unitValueUSD") or parsed.get("usd")
        return NormalizedEvent(
            jurisdiction=Jurisdiction.CRYPTO,
            source=self.name,
            source_url=parsed.get("txURL") or parsed.get("url"),
            issuer_name=f"{symbol} on {chain}",
            issuer_id=symbol or None,
            filer_name=f"{from_name} -> {to_name}",
            filer_id=str(parsed.get("transactionHash") or parsed.get("hash") or "") or None,
            filer_type=_ENTITY_TYPE_TO_FILER.get(
                parsed.get("from_entity_type") or "", FilerType.UNKNOWN.value
            ),
            transaction_type=TransactionType.CRYPTO_TRANSFER,
            transaction_date=when,
            disclosure_date=when,
            native_currency="USD",
            # Arkham's historical USD is its own valuation at transfer time, not a
            # disclosed figure, so it is always an estimate.
            native_amount=float(usd) if usd not in (None, "") else None,
            usd_value_is_estimate=True,
            raw_payload=parsed,
        )
