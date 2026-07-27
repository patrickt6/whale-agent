"""Whale Alert adapter: large on-chain transfers across ~30 chains.

Licensing: commercial API with a free tier. Personal ingestion only.

The honest caveat, carried into every event this produces: an on-chain transfer is not a
disclosed position. Nobody filed anything, the counterparties are heuristic wallet
labels, and an exchange-to-exchange move of $50M usually means nothing at all. These
events are deliberately given the lowest-signal transaction type so they cannot
out-rank a real filing, and transfers between two unlabelled wallets are dropped
entirely -- an anonymous address moving money is not information.
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

# Wallet owner types that identify a real counterparty rather than "unknown".
_LABELLED_TYPES = {"exchange", "fund", "wallet", "treasury", "custodian", "government"}


def _owner(side: dict | None) -> tuple[str | None, str | None]:
    """Return (owner label, owner type) for one side of a transfer."""
    if not isinstance(side, dict):
        return None, None
    owner = side.get("owner") or None
    owner_type = (side.get("owner_type") or "").lower() or None
    return owner, owner_type


class WhaleAlertAdapter(Adapter):
    name = "whale_alert"
    jurisdiction = Jurisdiction.CRYPTO.value

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        key = self.settings.require("whale_alert_api_key")
        start = kwargs.get("start")
        params: dict[str, Any] = {
            "api_key": key,
            "min_value": int(kwargs.get("min_value") or self.settings.whale_alert_min_usd),
        }
        if start is not None:
            params["start"] = int(start)
        if kwargs.get("end") is not None:
            params["end"] = int(kwargs["end"])
        yield request_json(
            f"{self.settings.whale_alert_base_url.rstrip('/')}/transactions",
            params=params,
            settings=self.settings,
        )

    def parse(self, raw: Any) -> list[dict]:
        rows = raw.get("transactions") or [] if isinstance(raw, dict) else raw or []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            from_owner, from_type = _owner(row.get("from"))
            to_owner, to_type = _owner(row.get("to"))
            # Both ends anonymous: no counterparty to name, so no story to tell.
            if not from_owner and not to_owner:
                continue
            out.append(
                {
                    **row,
                    "from_owner": from_owner,
                    "from_owner_type": from_type,
                    "to_owner": to_owner,
                    "to_owner_type": to_type,
                }
            )
        return out

    def normalize(self, parsed: dict) -> NormalizedEvent:
        ts = parsed.get("timestamp")
        when = datetime.fromtimestamp(int(ts), tz=UTC).date() if ts else date.today()
        from_owner = parsed.get("from_owner") or "unknown wallet"
        to_owner = parsed.get("to_owner") or "unknown wallet"
        symbol = str(parsed.get("symbol") or "").upper()
        from_type = parsed.get("from_owner_type")
        return NormalizedEvent(
            jurisdiction=Jurisdiction.CRYPTO,
            source=self.name,
            source_url=parsed.get("transaction_url"),
            issuer_name=f"{symbol} on {parsed.get('blockchain') or 'unknown chain'}",
            issuer_id=symbol or None,
            filer_name=f"{from_owner} -> {to_owner}",
            filer_id=str(parsed.get("hash") or "") or None,
            filer_type=(
                FilerType.FUND.value
                if from_type in _LABELLED_TYPES
                else FilerType.UNKNOWN.value
            ),
            transaction_type=TransactionType.CRYPTO_TRANSFER,
            transaction_date=when,
            disclosure_date=when,
            native_currency="USD",
            # Whale Alert prices the transfer at the time it happened; that is the
            # figure it publishes, so it is used as-is and flagged as an estimate.
            native_amount=(float(parsed["amount_usd"]) if parsed.get("amount_usd") else None),
            usd_value_is_estimate=True,
            raw_payload=parsed,
        )
