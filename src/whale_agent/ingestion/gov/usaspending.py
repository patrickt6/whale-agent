"""Federal contract awards from USASpending.gov -- keyless, and the closest thing the
free tier has to a causal link.

Why this source earns its place: every other context feed says what a company spends.
This one says what the government just handed it. Put next to a Form 4, it is the only
free dataset that can support "the CFO bought in March, and in April the Army awarded
them $4.8bn" -- which is a thesis, not a coincidence file.

Licensing: US federal public domain, published by the Treasury Bureau of the Fiscal
Service under the DATA Act. No key, no quota published, no redistribution restriction.

Scope: `ContextAnnotation`, never `NormalizedEvent`. An award is company-level context
with no filer taking a position; routing it into the daily $5M gate would be a category
error and would drown the digest in defence primes.

Two API details that are not obvious and that got verified live:

- `time_period` filters on *transaction* date by default, so a plain window returns
  awards whose `Start Date` is decades old (a 1993 DOE contract came back for a 2026
  window). `date_type: "new_awards_only"` is what actually means "awarded recently",
  and it is the default here, because an annotation dated 1993 sitting beside a 2026
  insider buy is worse than no annotation.
- Matching is by `recipient_search_text` on the recipient's legal name, not by ticker.
  There is no ticker in this dataset at all, so the caller supplies the mapping.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.errors import SourceUnavailableError
from whale_agent.ingestion._http import request_json
from whale_agent.ingestion.base import Adapter
from whale_agent.models.annotation import ContextAnnotation
from whale_agent.models.enums import Jurisdiction
from whale_agent.models.event import NormalizedEvent

# Contract award types only (A/B/C/D = definitive contract, purchase order, delivery
# order, BPA call). Grants and loans use different codes and a different fields list;
# they are deliberately out of scope until there is a reason to want them.
CONTRACT_AWARD_TYPES = ["A", "B", "C", "D"]

_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Start Date",
    "End Date",
    "Description",
    "recipient_id",
]

# Ticker -> the recipient name as it is registered in SAM, which is what
# `recipient_search_text` matches on. Small and hand-checked rather than fuzzy: a wrong
# match here attributes someone else's federal contract to our issuer.
RECIPIENT_NAMES: dict[str, str] = {
    "LMT": "Lockheed Martin",
    "RTX": "RTX Corporation",
    "NOC": "Northrop Grumman",
    "GD": "General Dynamics",
    "BA": "Boeing",
    "LHX": "L3Harris",
    "LDOS": "Leidos",
    "HII": "Huntington Ingalls",
    "TXT": "Textron",
    "CACI": "CACI International",
    "SAIC": "Science Applications International",
    "BAH": "Booz Allen Hamilton",
    "PLTR": "Palantir Technologies",
    "IBM": "International Business Machines",
    "MSFT": "Microsoft Corporation",
    "ORCL": "Oracle America",
    "AMZN": "Amazon Web Services",
    "GOOGL": "Google LLC",
    "PFE": "Pfizer",
    "MRNA": "Moderna",
    "CAT": "Caterpillar",
    "DE": "Deere & Company",
}


def recipient_name_for(ticker: str | None) -> str | None:
    """The SAM-registered recipient name for a ticker, or None if unmapped."""
    if not ticker:
        return None
    return RECIPIENT_NAMES.get(ticker.strip().upper())


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


class UsaSpendingAdapter(Adapter):
    """Recent federal contract awards for a named company.

    `normalize()` raises rather than returning something plausible-but-wrong: this
    adapter has no place in the daily event pipeline, and a caller reaching for it that
    way should find out at once. Use `annotations()`.
    """

    name = "usaspending"
    jurisdiction = Jurisdiction.US.value

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # -- requests ---------------------------------------------------------------
    def search_body(
        self,
        recipient: str,
        *,
        start: date,
        end: date,
        limit: int = 25,
        page: int = 1,
    ) -> dict:
        """The POST body for one page of an award search.

        Split out from `fetch` so the request shape is testable without a socket; the
        award-type codes and the `new_awards_only` date type are the parts most likely
        to be got wrong by a later edit.
        """
        return {
            "filters": {
                "award_type_codes": CONTRACT_AWARD_TYPES,
                "recipient_search_text": [recipient],
                "time_period": [
                    {
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "date_type": "new_awards_only",
                    }
                ],
            },
            "fields": _FIELDS,
            "limit": limit,
            "page": page,
            "sort": "Award Amount",
            "order": "desc",
        }

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        recipient = kwargs.get("recipient") or recipient_name_for(kwargs.get("ticker"))
        if not recipient:
            return
        end = kwargs.get("end") or date.today()
        start = kwargs.get("start") or end - timedelta(
            days=int(kwargs.get("lookback_days") or 180)
        )
        body = self.search_body(
            recipient,
            start=start,
            end=end,
            limit=int(kwargs.get("limit") or 25),
        )
        try:
            yield request_json(
                f"{self.settings.usaspending_base_url.rstrip('/')}/api/v2/search/spending_by_award/",
                method="POST",
                json_body=body,
                headers={"Accept": "application/json"},
                settings=self.settings,
            )
        except SourceUnavailableError:
            # Context is a nice-to-have; the digest ships without it.
            return

    def parse(self, raw: Any) -> list[dict]:
        if not isinstance(raw, dict):
            return []
        return [row for row in (raw.get("results") or []) if isinstance(row, dict)]

    def normalize(self, parsed: dict) -> NormalizedEvent:
        raise NotImplementedError(
            "A federal contract award is company-level context, not a whale event. "
            "Call UsaSpendingAdapter.annotations() instead."
        )

    # -- annotations ------------------------------------------------------------
    def annotations(
        self, rows: list[dict], *, ticker: str | None = None
    ) -> list[ContextAnnotation]:
        """Turn parsed award rows into dated context annotations.

        Rows with no usable start date are dropped rather than dated to today: an award
        stamped with the wrong day is exactly the kind of thing that makes a false
        "insider bought just before the contract" story.
        """
        out: list[ContextAnnotation] = []
        for row in rows:
            as_of = _parse_date(row.get("Start Date"))
            if as_of is None:
                continue
            agency = row.get("Awarding Sub Agency") or row.get("Awarding Agency") or ""
            award_id = row.get("Award ID") or ""
            out.append(
                ContextAnnotation(
                    source=self.name,
                    kind="gov_contract",
                    ticker=(ticker or "").upper() or None,
                    issuer_name=row.get("Recipient Name") or None,
                    as_of=as_of,
                    amount_usd=_float(row.get("Award Amount")),
                    label=(
                        "federal contract award"
                        + (f" from {agency}" if agency else "")
                        + (f" ({award_id})" if award_id else "")
                    ),
                    source_url=self.award_url(row),
                    raw_payload=row,
                )
            )
        return out

    @staticmethod
    def award_url(row: dict) -> str | None:
        """Public USASpending permalink for an award, when the row carries its id."""
        internal = row.get("generated_internal_id")
        if not internal:
            return None
        return f"https://www.usaspending.gov/award/{internal}"

    def annotations_for_ticker(
        self,
        ticker: str,
        *,
        lookback_days: int = 180,
        limit: int = 25,
    ) -> list[ContextAnnotation]:  # pragma: no cover - network path
        """Convenience end-to-end call. Empty list on anything going wrong."""
        recipient = recipient_name_for(ticker)
        if not recipient or not self.settings.usaspending_enabled:
            return []
        out: list[ContextAnnotation] = []
        for raw in self.fetch(ticker=ticker, lookback_days=lookback_days, limit=limit):
            out.extend(self.annotations(self.parse(raw), ticker=ticker))
        return out
