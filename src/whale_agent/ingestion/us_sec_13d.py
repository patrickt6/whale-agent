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
from datetime import date, timedelta
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


def _primary_document(cik: str, accession: str) -> str:
    """The filing's own structured document. Empty string when it cannot be had."""
    from whale_agent.ingestion.fund_watchlist import _get

    if not cik or not accession:
        return ""
    bare = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{bare}/primary_doc.xml"
    try:
        return _get(url).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the fallback is best-effort
        return ""


def fill_missing_stake(cover: dict) -> dict:
    """Read percent and shares from the filing when the library reported zero.

    edgartools falls back to a header-only parse for filings it cannot read
    structurally and reports 0.0. These filings exist because somebody crossed 5%, so a
    zero is a failed parse rather than a fact, and the figures are in the document.
    """
    from whale_agent.ingestion.fund_watchlist import parse_stake_filing

    if cover.get("percent_of_class") or cover.get("shares_owned"):
        return cover
    xml = _primary_document(
        str(cover.get("filer_cik") or ""), str(cover.get("accession") or "")
    )
    if not xml:
        return cover
    detail = parse_stake_filing(xml)
    if detail.percent is None and detail.shares is None:
        return cover
    return {
        **cover,
        "percent_of_class": detail.percent
        if detail.percent is not None
        else cover.get("percent_of_class"),
        "shares_owned": detail.shares
        if detail.shares is not None
        else cover.get("shares_owned"),
    }


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
        # 13G outnumbers 13D roughly 12:1 and is how most funds actually cross 5%
        # (it is the passive-investor form), so both must be fetched or ~90% of
        # addressable volume never reaches the pipeline.
        form = kwargs.get("form") or ["SC 13D", "SC 13G"]
        limit = kwargs.get("limit", 20)
        since = kwargs.get("since")  # date | None: last successful watermark
        filing_date = None
        if since is not None:
            until = kwargs.get("until") or date.today() + timedelta(days=1)
            filing_date = f"{since.isoformat()}:{until.isoformat()}"
        filings = edgar.get_filings(form=form, filing_date=filing_date)
        if filings is None:
            return
        for f in filings.head(limit):
            for cover in self._filing_to_covers(f):
                yield json.dumps(cover)

    @staticmethod
    def _filing_to_covers(filing: Any) -> list[dict]:  # pragma: no cover - network path
        """One dict per reporting person on the filing's cover page.

        A 13D/G names its filer(s) via `reporting_persons` (`ReportingPerson`, which
        carries name/CIK/aggregate_amount/percent_of_class); the `Schedule13D`/
        `Schedule13G` object itself never exposes a `filer_name` or stake size. There is
        deliberately no issuer-name fallback here: a filing whose reporting persons
        cannot be read is skipped entirely rather than silently attributed to the
        issuer, which is exactly the bug that produced 23 rows of an issuer disclosing
        a stake in itself.
        """
        obj = filing.obj()
        meta = filing_metadata(filing)
        issuer_info = getattr(obj, "issuer_info", None)
        subject_cik = (
            getattr(issuer_info, "cik", None)
            or getattr(obj, "subject_company_cik", None)
            or filing.cik
        )
        cusip = getattr(issuer_info, "cusip", None) or getattr(obj, "cusip", None)
        issuer_name = (
            getattr(issuer_info, "name", None)
            or getattr(obj, "subject_company", None)
            or filing.company
        )
        reporting_persons = getattr(obj, "reporting_persons", None) or []

        base = {
            "form": filing.form,
            "issuer_name": issuer_name,
            "issuer_cusip": cusip or None,
            "issuer_cik": str(subject_cik) if subject_cik else None,
            "filing_date": meta.get("filing_date") or str(filing.filing_date),
            "accession": meta.get("accession"),
            "source_url": meta.get("source_url"),
        }

        covers: list[dict] = []
        for person in reporting_persons:
            filer_name = getattr(person, "name", None)
            if not filer_name:
                # No fallback to the issuer. An unidentifiable filer is quarantined
                # by never being emitted, not silently mislabeled.
                continue
            filer_cik = getattr(person, "cik", None) or filing.cik
            covers.append(
                {
                    **base,
                    "filer_name": filer_name,
                    "filer_cik": filer_cik,
                    "percent_of_class": getattr(person, "percent_of_class", None),
                    "shares_owned": getattr(person, "aggregate_amount", None),
                }
            )
        return [fill_missing_stake(c) for c in covers]

    def parse(self, raw: str) -> list[dict]:
        data = json.loads(raw)
        filer = (data.get("filer_name") or "").strip()
        issuer = (data.get("issuer_name") or "").strip()
        # Defense in depth: even if a raw row somehow carries no filer, or the same
        # name as the issuer (the exact shape of the "23 rows disclosing a stake in
        # itself" bug), it is dropped here rather than reaching a NormalizedEvent.
        if not filer or (issuer and filer.casefold() == issuer.casefold()):
            return []
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
