"""US SEC SC 13D / 13G adapter (large beneficial-ownership stakes).

13D signals activist intent; 13G is passive. Both cross the ~5% threshold. parse()
consumes the structured cover-page fields (a JSON object mirroring the SEC 13D cover)
and normalize() maps to the percentage-threshold valuation path.

The cover page identifies the subject company by CUSIP, which nothing downstream is
keyed on, so the subject's CIK is captured in `fetch()` and translated to a ticker at
normalize time -- otherwise these events reach the enrichment layer unkeyed and are
skipped, exactly as Form 4 events were. The filing URL is reconstructed from CIK plus
accession for the same reason it is on Form 4: an unlinked row cannot be checked.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from typing import Any

from whale_agent.enrichment.tickers import TickerResolver
from whale_agent.ingestion.base import Adapter
from whale_agent.ingestion.us_sec import _parse_date, edgar_filing_url, filing_metadata
from whale_agent.models.enums import (
    FilerType,
    Jurisdiction,
    PriceSource,
    TransactionType,
)
from whale_agent.models.event import NormalizedEvent


class UsSec13DGAdapter(Adapter):
    name = "sec_edgar_13dg"
    jurisdiction = Jurisdiction.US.value

    def __init__(self, ticker_resolver: TickerResolver | None = None) -> None:
        self.ticker_resolver = ticker_resolver

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        import edgar

        user_agent = kwargs.get("user_agent")
        if user_agent:
            edgar.set_identity(user_agent)
        if self.ticker_resolver is None:
            from whale_agent.enrichment.tickers import default_ticker_resolver

            self.ticker_resolver = default_ticker_resolver(user_agent=user_agent)
        form = kwargs.get("form", "SC 13D")
        limit = kwargs.get("limit", 20)
        for f in edgar.get_filings(form=form).head(limit):
            yield json.dumps(self._filing_to_cover(f))

    @staticmethod
    def _filing_to_cover(filing: Any) -> dict:  # pragma: no cover - network path
        obj = filing.obj()
        meta = filing_metadata(filing)
        # The subject company's CIK, which is what enrichment needs -- not the filer's.
        # edgartools exposes it on `issuer_info`; EDGAR also indexes a 13D under the
        # subject company, so `filing.cik` is the same registrant and is the fallback
        # for versions or filings where the cover object has no structured issuer.
        issuer_info = getattr(obj, "issuer_info", None)
        subject_cik = (
            getattr(issuer_info, "cik", None)
            or getattr(obj, "subject_company_cik", None)
            or filing.cik
        )
        cusip = getattr(obj, "cusip", None) or getattr(issuer_info, "cusip", None)
        return {
            "form": filing.form,
            "issuer_name": getattr(obj, "subject_company", None)
            or getattr(issuer_info, "name", None)
            or filing.company,
            "issuer_cusip": cusip or None,
            "issuer_cik": str(subject_cik) if subject_cik else None,
            "filer_name": getattr(obj, "filer_name", None) or filing.company,
            "filer_cik": filing.cik,
            "percent_of_class": getattr(obj, "percent_of_class", None),
            "shares_owned": getattr(obj, "aggregate_amount", None),
            "filing_date": meta.get("filing_date") or str(filing.filing_date),
            "accession": meta.get("accession"),
            "source_url": meta.get("source_url"),
        }

    def parse(self, raw: str) -> list[dict]:
        data = json.loads(raw)
        return [data]

    def normalize(self, parsed: dict) -> NormalizedEvent:
        form = (parsed.get("form") or "").upper()
        is_activist = "13D" in form
        is_amendment = form.endswith("/A")
        txn_type = TransactionType.ACTIVIST_13D if is_activist else TransactionType.PASSIVE_13G
        filing_date = _parse_date(parsed.get("filing_date")) or date.today()
        percent = parsed.get("percent_of_class")
        price = parsed.get("price")  # optional; supplied by valuation stage if absent
        source_url = parsed.get("source_url") or edgar_filing_url(
            parsed.get("filer_cik"), parsed.get("accession")
        )
        return NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            source_url=source_url,
            issuer_name=parsed.get("issuer_name") or "UNKNOWN",
            issuer_id=parsed.get("issuer_cusip"),
            ticker=parsed.get("ticker") or self._resolve_ticker(parsed.get("issuer_cik")),
            filer_name=parsed.get("filer_name") or "UNKNOWN",
            filer_id=str(parsed.get("filer_cik")) if parsed.get("filer_cik") else None,
            filer_type=FilerType.FUND.value,
            transaction_type=txn_type,
            transaction_date=filing_date,
            disclosure_date=filing_date,
            native_currency="USD",
            share_count=parsed.get("shares_owned"),
            price_used=price,
            price_source=PriceSource.CLOSE_ON_DATE if price else PriceSource.NOT_PRICED,
            percent_of_company=float(percent) if percent is not None else None,
            percentage_threshold_crossed=percent is not None,
            shares_outstanding=parsed.get("shares_outstanding"),
            is_amendment=is_amendment,
            raw_payload=parsed,
        )

    def _resolve_ticker(self, cik: object) -> str | None:
        """Subject-company ticker, or None. Total by design, like the Form 4 path."""
        if self.ticker_resolver is None or not cik:
            return None
        try:
            return self.ticker_resolver.resolve_cik(cik)
        except Exception:  # noqa: BLE001 - deliberately total
            return None
