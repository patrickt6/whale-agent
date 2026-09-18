"""Dataroma adapter: significant insider buys at superinvestor-owned companies.

Licensing -- read this before turning it on. Dataroma is free to read but has no
official API and is owned by Morningstar; this adapter scrapes HTML, which its terms of
use may not permit. Treat it as personal ingestion only, never redistribute its output,
and keep the request rate to a handful per day. It is the one source
here whose legal footing is weaker than "open public data", which is why it is easy to
switch off: `WHALE_ENABLE_DATAROMA=0`.

What it adds over EDGAR: Dataroma cross-references insider buys against the 84
superinvestor portfolios it tracks, so a purchase at a company a famous fund already
owns surfaces here as a single line. The underlying facts are the same Form 4s, so
overlap with `us_sec.py` collapses on the dedup key.

The page is a plain HTML table whose column order has changed before, so `parse()` reads
the header row and keys cells by name. A renamed column drops the affected field rather
than silently shifting every value one place to the left.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.ingestion._http import polite_headers, request_text
from whale_agent.ingestion.base import Adapter
from whale_agent.models.enums import (
    FilerType,
    Jurisdiction,
    PriceSource,
    TransactionType,
)
from whale_agent.models.event import NormalizedEvent

INSIDER_BUYS_PATH = "/m/ins/ins.php"

_TICKER = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\b")


def _norm_header(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class DataromaInsiderBuysAdapter(Adapter):
    """Scrapes Dataroma's significant-insider-buys table."""

    name = "dataroma_insider_buys"
    jurisdiction = Jurisdiction.US.value

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        yield request_text(
            f"{self.settings.dataroma_base_url.rstrip('/')}{INSIDER_BUYS_PATH}",
            headers=polite_headers(self.settings.dataroma_user_agent, {"Accept": "text/html"}),
            settings=self.settings,
        )

    def parse(self, raw: str) -> list[dict]:
        """Read the first data table, keying every cell by its column header."""
        from bs4 import BeautifulSoup  # imported lazily: only this adapter needs it

        soup = BeautifulSoup(raw, "html.parser")
        rows: list[dict] = []
        for table in soup.find_all("table"):
            header_cells = table.find_all("th")
            if not header_cells:
                continue
            headers = [_norm_header(c.get_text()) for c in header_cells]
            if "stock" not in headers and "ticker" not in headers:
                continue
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) < 3:
                    continue
                row = {
                    headers[i]: cells[i].get_text(" ", strip=True)
                    for i in range(min(len(headers), len(cells)))
                }
                link = tr.find("a", href=True)
                if link:
                    row["_href"] = link["href"]
                rows.append(row)
            break  # the first matching table is the insider-buys table
        return rows

    def normalize(self, parsed: dict) -> NormalizedEvent:
        stock = parsed.get("stock") or parsed.get("ticker") or ""
        ticker_match = _TICKER.search(stock)
        ticker = ticker_match.group(1) if ticker_match else None
        # "AAPL - Apple Inc." -> issuer name is whatever follows the separator.
        issuer = stock
        for sep in (" - ", " – ", "-"):
            if sep in stock:
                issuer = stock.split(sep, 1)[1].strip()
                break
        when = _parse_date(parsed.get("date")) or date.today()
        price = _num(parsed.get("price"))
        value = _num(parsed.get("value") or parsed.get("amount"))
        insider = parsed.get("insider") or parsed.get("reporter") or "UNKNOWN"
        title = parsed.get("title") or ""
        href = parsed.get("_href")
        return NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            source_url=(
                f"{self.settings.dataroma_base_url.rstrip('/')}{href}"
                if href and href.startswith("/")
                else href
            ),
            issuer_name=issuer or ticker or "UNKNOWN",
            ticker=ticker,
            filer_name=f"{insider} ({title})" if title else insider,
            filer_id=f"dataroma:{insider}",
            filer_type=FilerType.INSIDER.value,
            # This page lists purchases only; it is not a general transaction feed.
            transaction_type=TransactionType.OPEN_MARKET_BUY,
            transaction_date=when,
            disclosure_date=when,
            native_currency="USD",
            native_amount=value,
            share_count=_num(parsed.get("shares")),
            price_used=price,
            # The price shown is the Form 4's stated transaction price, restated.
            price_source=PriceSource.FILING_STATED if price else PriceSource.NOT_PRICED,
            raw_payload=parsed,
        )
