"""Quarterly lobbying disclosures, straight from the Senate Office of Public Records.

This is the primary source that Quiver's lobbying product is itself derived from. It is
keyless, complete (53,822 filings for 2026 alone), and public domain, so the paid feed
buys convenience and a ticker mapping rather than data. See the module-level note at the
bottom of `annotations()` for where the free version is genuinely weaker.

Host matters: the API lives at **lda.senate.gov**. The newer `lda.gov` host answers 403
to programmatic clients. Do not "modernise" the base URL.

Licensing: US government public domain, published under the Lobbying Disclosure Act.
No key and no advertised rate limit -- which is a reason for restraint, not licence: the
adapter sleeps between pages and caps how many it will walk.

The one modelling trap, and the reason this file is longer than it looks like it should
be: **income and expenses are alternative reporting methods, never both**. A lobbying
firm filing on behalf of a client reports `income` (what the client paid it); a company
that lobbies for itself reports `expenses` (what it spent in-house), along with an
`expenses_method` saying which accounting basis it used. Verified live: Lockheed's
outside firm reported income 50,000.00 with expenses null, while Boeing filing for
itself reported expenses 2,450,000.00 with income null. Coalescing a null to zero would
understate every self-filer -- the largest spenders in the dataset -- to nothing.

Registrations (`filing_type` "RR" and friends) carry neither figure at all. They are
real filings and worth surfacing as "started lobbying", but they are not spend, so they
get an annotation with `amount_usd=None` rather than a fabricated zero.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.errors import SourceUnavailableError
from whale_agent.ingestion._http import request_json
from whale_agent.ingestion.base import Adapter
from whale_agent.models.annotation import ContextAnnotation
from whale_agent.models.enums import Jurisdiction
from whale_agent.models.event import NormalizedEvent

# `filing_period` is a slug, not a quarter number, and the annotation needs a real date.
# Anchoring to the first day of the covered quarter (not the posting date) is what makes
# these comparable with an event's transaction date.
_PERIOD_START_MONTH: dict[str, int] = {
    "first_quarter": 1,
    "second_quarter": 4,
    "third_quarter": 7,
    "fourth_quarter": 10,
    # Pre-2008 filings used semiannual periods; still present in the archive.
    "mid_year": 1,
    "year_end": 7,
}

# `expenses_method` codes, spelled out because "C" on its own means nothing to a reader
# of the weekly report.
EXPENSES_METHODS: dict[str, str] = {
    "A": "LDA method",
    "B": "IRC 6033(b)(8) method",
    "C": "IRC 162(e) method",
}


def _decimal(value: Any) -> float | None:
    """Parse an LDA money string. None stays None -- see the module docstring."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _posted_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def period_start(row: dict) -> date | None:
    """First day of the quarter a filing covers, from `filing_year`/`filing_period`.

    Falls back to the posting date, because a filing that is dated only by when it was
    uploaded is still better than a dropped one -- but never to today.
    """
    year = row.get("filing_year")
    month = _PERIOD_START_MONTH.get(str(row.get("filing_period") or "").lower())
    if year and month:
        try:
            return date(int(year), month, 1)
        except (TypeError, ValueError):
            pass
    return _posted_date(row.get("dt_posted"))


def reported_amount(row: dict) -> tuple[float | None, str]:
    """The one figure a filing actually reports, and which method it used.

    Returns `(amount, basis)` where basis is "income", "expenses", or "" when the
    filing reports neither (registrations, terminations, no-activity reports).
    """
    income = _decimal(row.get("income"))
    if income is not None:
        return income, "income"
    expenses = _decimal(row.get("expenses"))
    if expenses is not None:
        return expenses, "expenses"
    return None, ""


class SenateLdaAdapter(Adapter):
    """Lobbying filings for a client company, as `ContextAnnotation`s.

    `normalize()` raises: lobbying spend is quarterly company-level context bound for
    the weekly report, not a whale event.
    """

    name = "senate_lda"
    jurisdiction = Jurisdiction.US.value

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # -- requests ---------------------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> Any:
        return request_json(
            url,
            params=params,
            headers={"Accept": "application/json"},
            settings=self.settings,
        )

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        """Walk the paginated filings list, politely.

        The API returns a DRF-style envelope with an absolute `next` URL, so pagination
        follows that rather than reconstructing page numbers. `max_pages` is a hard stop:
        a broad client-name query can match thousands of filings, and there is no
        published rate limit to tell us when we have become a problem.
        """
        params = {
            "filing_year": kwargs.get("filing_year") or date.today().year,
            # The pagination parameter is `limit`, not DRF's usual `page_size`; the
            # `next` URL the API hands back spells it that way.
            "limit": int(kwargs.get("page_size") or 25),
        }
        client_name = kwargs.get("client_name")
        if client_name:
            params["client_name"] = client_name
        if kwargs.get("filing_type"):
            params["filing_type"] = kwargs["filing_type"]

        url: str | None = f"{self.settings.senate_lda_base_url.rstrip('/')}/filings/"
        max_pages = int(kwargs.get("max_pages") or 5)
        for page in range(max_pages):
            try:
                payload = self._get(url, params if page == 0 else None)
            except SourceUnavailableError:
                return
            yield payload
            url = payload.get("next") if isinstance(payload, dict) else None
            if not url:
                return
            time.sleep(self.settings.senate_lda_page_delay_seconds)

    def parse(self, raw: Any) -> list[dict]:
        if not isinstance(raw, dict):
            return []
        return [row for row in (raw.get("results") or []) if isinstance(row, dict)]

    def normalize(self, parsed: dict) -> NormalizedEvent:
        raise NotImplementedError(
            "Lobbying spend is weekly-report context, not a whale event. "
            "Call SenateLdaAdapter.annotations() instead."
        )

    # -- annotations ------------------------------------------------------------
    def annotations(
        self,
        rows: list[dict],
        *,
        ticker: str | None = None,
        include_unpriced: bool = True,
    ) -> list[ContextAnnotation]:
        """Turn parsed filings into dated annotations.

        `include_unpriced` keeps registrations and no-activity reports, which carry no
        figure but do say a company has just engaged (or dropped) a lobbying firm. Set
        it false when the consumer only wants spend it can total.
        """
        out: list[ContextAnnotation] = []
        for row in rows:
            as_of = period_start(row)
            if as_of is None:
                continue
            amount, basis = reported_amount(row)
            if amount is None and not include_unpriced:
                continue

            client = (row.get("client") or {}).get("name") or ""
            registrant = (row.get("registrant") or {}).get("name") or ""
            self_filed = bool(basis == "expenses")

            if amount is None:
                label = row.get("filing_type_display") or "lobbying filing"
                label = f"lobbying {label.lower()}"
                if registrant:
                    label += f" by {registrant}"
            elif self_filed:
                method = EXPENSES_METHODS.get(str(row.get("expenses_method") or ""), "")
                label = "in-house lobbying spend" + (f" ({method})" if method else "")
            else:
                label = "lobbying fees" + (f" paid to {registrant}" if registrant else "")

            out.append(
                ContextAnnotation(
                    source=self.name,
                    kind="lobbying",
                    ticker=(ticker or "").upper() or None,
                    issuer_name=client or registrant or None,
                    as_of=as_of,
                    amount_usd=amount,
                    label=label,
                    source_url=row.get("filing_document_url") or row.get("url"),
                    raw_payload=row,
                )
            )
        return out

    def annotations_for_client(
        self,
        client_name: str,
        *,
        ticker: str | None = None,
        filing_year: int | None = None,
        max_pages: int = 5,
    ) -> list[ContextAnnotation]:  # pragma: no cover - network path
        """Convenience end-to-end call. Empty list on anything going wrong."""
        if not self.settings.senate_lda_enabled or not client_name:
            return []
        out: list[ContextAnnotation] = []
        for raw in self.fetch(
            client_name=client_name, filing_year=filing_year, max_pages=max_pages
        ):
            out.extend(self.annotations(self.parse(raw), ticker=ticker))
        return out


def total_spend(annotations: Iterable[ContextAnnotation]) -> float:
    """Sum only the annotations that actually reported a figure.

    Deliberately not `sum(a.amount_usd or 0)`: the point is that unreported is not
    zero, and this is the function that keeps a caller from forgetting it.
    """
    return sum(a.amount_usd for a in annotations if a.amount_usd is not None)
