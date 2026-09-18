"""US SEC adapter: Form 4 (insider transactions) and SC 13D/G (large stakes).

parse()/normalize() operate on raw XML strings and are fully deterministic, so they
run against golden-file fixtures with no network. fetch() uses edgartools for live
pulls (Phase 0 smoke test only).

Two things the ownership document does not give us are added here rather than downstream.
The XML names the issuer by CIK only, so a `TickerResolver` is consulted at normalize
time -- without it every EDGAR event reaches the enrichment layer with no ticker and is
skipped. And the XML has no link to itself, so `edgar_filing_url` reconstructs the
EDGAR index URL from the CIK and accession number that the filing metadata already
carries: every row in the product promises a "read the filing" link, and that link is
the only way a reader can check a figure we computed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from whale_agent.enrichment.tickers import TickerResolver, normalize_cik
from whale_agent.ingestion.base import Adapter
from whale_agent.ingestion.vendor_types import (
    coerce_price_per_share,
    coerce_share_count,
)
from whale_agent.models.enums import (
    FORM4_CODE_MAP,
    FilerType,
    Jurisdiction,
    PriceSource,
    TransactionType,
)
from whale_agent.models.event import NormalizedEvent


def _text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    # SEC ownership XML wraps many scalars in a <value> child.
    value = node.find("value")
    if value is not None and value.text is not None:
        return value.text.strip()
    return node.text.strip() if node.text else None


def _to_float(s: str | None) -> float | None:
    """Numeric text as a float, or None. Never raises: one unreadable cell in an
    ownership document must not cost the whole filing."""
    if not s:
        return None
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def edgar_filing_url(cik: object, accession: object) -> str | None:
    """Build the EDGAR filing-index URL from a CIK and an accession number.

    Both are always present in the filing metadata even when the source hands us no
    link, so there is no reason for an EDGAR-sourced row to reach the digest unlinked.
    The index page is chosen over the primary document deliberately: it is stable, it
    lists every exhibit, and it is what a reader wants when the question is "where does
    this number come from".

    The path wants the CIK unpadded and the accession both ways round -- bare digits for
    the directory, dashed for the file name -- which is exactly the kind of detail that
    produces a plausible-looking 404 if it is guessed.
    """
    padded = normalize_cik(cik)
    digits = "".join(ch for ch in str(accession or "") if ch.isdigit())
    if not padded or len(digits) != 18:
        return None
    dashed = f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
    return f"https://www.sec.gov/Archives/edgar/data/{int(padded)}/{digits}/{dashed}-index.htm"


def filing_metadata(filing: Any) -> dict:  # pragma: no cover - network path
    """Pull the identifying fields off an edgartools filing, tolerating its renames."""
    accession = (
        getattr(filing, "accession_no", None)
        or getattr(filing, "accession_number", None)
        or getattr(filing, "accessionNo", None)
    )
    cik = getattr(filing, "cik", None)
    return {
        "accession": str(accession) if accession else None,
        "filing_cik": str(cik) if cik is not None else None,
        "source_url": getattr(filing, "filing_url", None)
        or getattr(filing, "homepage_url", None)
        or edgar_filing_url(cik, accession),
        "filing_date": str(getattr(filing, "filing_date", "") or "") or None,
    }


def take_filings(filings, limit: int | None):
    """The filings to read from an EDGAR result set.

    A falsy `limit` means the whole window. The date range already bounds the work --
    the watermark makes a daily run one day wide -- so an additional row cap only
    discards filings that were correctly selected. Over 2026-07-28..08-04 EDGAR
    published 4,218 Form 4 filings against a default cap of 50.
    """
    if not limit:
        return iter(filings)
    return iter(filings.head(limit))


class UsSecForm4Adapter(Adapter):
    """Parses SEC ownershipDocument (Form 3/4/5) XML.

    `ticker_resolver` is injected rather than constructed so parse/normalize stay pure
    and unit-testable offline; the live `fetch()` path builds the shared SEC-backed
    resolver on demand when nothing was supplied.
    """

    name = "sec_edgar_form4"
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
        form = kwargs.get("form", "4")
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
            # An envelope, not a bare string: the XML alone cannot say where it came
            # from, and a filing with no link is a figure the reader cannot check.
            yield {"xml": f.xml(), **filing_metadata(f)}

    def parse(self, raw: str | Mapping[str, Any]) -> list[dict]:
        """Explode one Form 4 XML into one row per non-derivative transaction.

        Accepts either the bare XML string (what the golden fixtures hold) or the
        `{"xml": ..., "source_url": ...}` envelope that `fetch()` yields.
        """
        meta: dict[str, Any] = {}
        if isinstance(raw, Mapping):
            meta = {k: v for k, v in raw.items() if k != "xml"}
            raw = str(raw.get("xml") or "")
        root = ET.fromstring(raw)
        is_amendment = (root.findtext("documentType") or "").strip().endswith("/A")
        issuer = root.find("issuer")
        issuer_name = _text(issuer.find("issuerName")) if issuer is not None else None
        issuer_cik = _text(issuer.find("issuerCik")) if issuer is not None else None

        owner = root.find("reportingOwner")
        filer_name = None
        filer_cik = None
        is_director = is_officer = is_ten_percent = False
        officer_title = None
        if owner is not None:
            oid = owner.find("reportingOwnerId")
            if oid is not None:
                filer_name = _text(oid.find("rptOwnerName"))
                filer_cik = _text(oid.find("rptOwnerCik"))
            # The relationship block. Never read until now, which is why the digest could
            # say a person bought $131M without saying they run the company.
            rel = owner.find("reportingOwnerRelationship")
            if rel is not None:
                is_director = _text(rel.find("isDirector")) in ("1", "true", "TRUE")
                is_officer = _text(rel.find("isOfficer")) in ("1", "true", "TRUE")
                is_ten_percent = _text(rel.find("isTenPercentOwner")) in ("1", "true", "TRUE")
                officer_title = _text(rel.find("officerTitle"))

        rows: list[dict] = []

        def _row_common(txn: ET.Element, instrument_type: str) -> dict:
            coding = txn.find("transactionCoding")
            code = _text(coding.find("transactionCode")) if coding is not None else None
            amounts = txn.find("transactionAmounts")
            shares = price = None
            acq_disp = None
            if amounts is not None:
                shares = _text(amounts.find("transactionShares"))
                price = _text(amounts.find("transactionPricePerShare"))
                acq_disp = _text(amounts.find("transactionAcquiredDisposedCode"))
            txn_date = _text(txn.find("transactionDate"))
            is_10b51 = False
            if coding is not None:
                is_10b51 = (coding.findtext("transactionTimeliness") or "") == "L"
            return {
                "issuer_name": issuer_name,
                "issuer_cik": issuer_cik,
                "filer_name": filer_name,
                "filer_cik": filer_cik,
                "is_director": is_director,
                "is_officer": is_officer,
                "is_ten_percent_owner": is_ten_percent,
                "officer_title": officer_title,
                "code": code,
                "acquired_disposed": acq_disp,
                # Non-raising: a malformed number in one transaction line must cost
                # that field, not the whole filing. The bounds are applied in
                # normalize(), where the row is still in scope for the log.
                "shares": _to_float(shares),
                "price": _to_float(price),
                "transaction_date": txn_date,
                "is_amendment": is_amendment,
                "is_10b51": is_10b51,
                # Clearly typed so a derivative row (option exercise, RSU vesting) is
                # never confused with a plain open-market transaction downstream.
                "instrument_type": instrument_type,
                "source_url": meta.get("source_url")
                or edgar_filing_url(
                    meta.get("filing_cik") or issuer_cik, meta.get("accession")
                ),
            }

        non_derivative_table = root.find("nonDerivativeTable")
        if non_derivative_table is not None:
            for txn in non_derivative_table.findall("nonDerivativeTransaction"):
                rows.append(_row_common(txn, "non_derivative"))

        # Option exercises and RSU vesting are reported here, not in
        # nonDerivativeTable. Skipping this table silently discards them (defect #4).
        derivative_table = root.find("derivativeTable")
        if derivative_table is not None:
            for txn in derivative_table.findall("derivativeTransaction"):
                row = _row_common(txn, "derivative")
                title_el = txn.find("underlyingSecurity/underlyingSecurityTitle")
                row["underlying_security_title"] = _text(title_el)
                row["conversion_or_exercise_price"] = _to_float(
                    _text(txn.find("conversionOrExercisePrice"))
                )
                rows.append(row)

        return rows

    def normalize(self, parsed: dict) -> NormalizedEvent:
        txn_type = self._classify(parsed)
        txn_date = _parse_date(parsed.get("transaction_date")) or date.today()
        # Same bounded coercion as the vendor path. EDGAR's own XML is the more
        # trustworthy of the two, which is exactly why the guard belongs here too: the
        # bug class is a field holding the wrong quantity, not a vendor being careless.
        priced = coerce_price_per_share(
            parsed.get("price"), vendor=self.name, field="price", ctx=parsed
        )
        shares = coerce_share_count(
            parsed.get("shares"), vendor=self.name, field="shares", ctx=parsed
        )
        price = priced.value
        event = NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source=self.name,
            source_url=parsed.get("source_url"),
            issuer_name=parsed.get("issuer_name") or "UNKNOWN",
            issuer_id=parsed.get("issuer_cik"),
            ticker=self._resolve_ticker(parsed.get("issuer_cik")),
            filer_name=parsed.get("filer_name") or "UNKNOWN",
            filer_id=parsed.get("filer_cik"),
            filer_type=FilerType.INSIDER.value,
            transaction_type=txn_type,
            transaction_date=txn_date,
            disclosure_date=txn_date,  # Form 4 lag <= 2 business days; refined by fetch()
            native_currency="USD",
            share_count=shares.value,
            price_used=price,
            price_source=PriceSource.FILING_STATED if price else PriceSource.NOT_PRICED,
            is_amendment=bool(parsed.get("is_amendment")),
            raw_payload=parsed,
        )
        return shares.record_on(priced.record_on(event))

    def _resolve_ticker(self, cik: object) -> str | None:
        """Ticker for the issuer CIK, or None. Never raises: a resolver that is down
        must cost this event its enrichment, not the run its digest."""
        if self.ticker_resolver is None or not cik:
            return None
        try:
            return self.ticker_resolver.resolve_cik(cik)
        except Exception:  # noqa: BLE001 - deliberately total
            return None

    @staticmethod
    def _classify(parsed: dict) -> TransactionType:
        code = (parsed.get("code") or "").upper()
        base = FORM4_CODE_MAP.get(code, TransactionType.OTHER)
        # An "S" sale under a 10b5-1 plan is a scheduled sale, not opportunistic.
        if base == TransactionType.OPEN_MARKET_SELL and parsed.get("is_10b51"):
            return TransactionType.SCHEDULED_SALE
        return base
