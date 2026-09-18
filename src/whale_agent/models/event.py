"""The NormalizedEvent schema: the single canonical representation of a disclosed move.

Every adapter normalizes its jurisdiction-specific filing into this shape. Downstream
enrichment, scoring, dedup, and render all operate on NormalizedEvent only.
"""

from __future__ import annotations

import hashlib
from datetime import date

from pydantic import BaseModel, Field

from whale_agent.models.context import EventContext
from whale_agent.models.enums import (
    Jurisdiction,
    PriceSource,
    Tier,
    TransactionType,
)


class NormalizedEvent(BaseModel):
    """One disclosed transaction/holding, normalized across jurisdictions."""

    # Identity / provenance
    jurisdiction: Jurisdiction
    source: str  # e.g. "sec_edgar"
    source_url: str | None = None

    # Parties
    issuer_name: str
    issuer_id: str | None = None  # ISIN/LEI/CUSIP/CIK
    ticker: str | None = None  # exchange symbol, when known; drives price lookup
    filer_name: str
    filer_id: str | None = None
    filer_type: str = "unknown"
    # What the filer is to this issuer: "Director", "Chief Financial Officer",
    # "10% owner". None means we never learned it, which is the correct state for a
    # 13D filer or a congressional disclosure, where there is no relationship to report.
    filer_role: str | None = None

    # The event
    transaction_type: TransactionType
    transaction_date: date
    disclosure_date: date

    # Money (native + normalized). See  for valuation rules.
    native_currency: str = "USD"
    native_amount: float | None = None  # native-currency dollar value if stated
    share_count: float | None = None
    price_used: float | None = None
    price_source: PriceSource = PriceSource.NOT_PRICED
    usd_value: float | None = None
    usd_value_is_estimate: bool = False
    fx_rate_used: float | None = None

    # Percentage-threshold regimes (Taiwan, Japan, HK, EU, AU, India)
    percent_of_company: float | None = None
    percentage_threshold_crossed: bool = False
    shares_outstanding: float | None = None
    implied_usd_value: float | None = None

    # Dedup / amendments
    is_amendment: bool = False
    amended: bool = False  # this original was superseded by a later amendment
    supersedes_id: str | None = None

    # Scoring output (filled by the scoring stage)
    score: float | None = None
    tier: Tier | None = None

    # Cluster / novelty flags (filled by enrichment)
    is_first_time_filer: bool = False
    cluster_size: int = 1  # number of insiders at this issuer in the window
    is_routine: bool = False

    # Cohen-Malloy-Pomorski opportunistic/routine label from
    # `enrichment/routine_classifier`: "opportunistic", "routine", or "unknown" when
    # there is not enough filer history to say. Deliberately separate from `is_routine`
    # above, which `scoring/score.py` reads for the routine penalty -- this field is
    # display-only and never feeds ranking. None means the classifier has not run over
    # this event yet; rendered text must show it as "unknown" rather than omit it, since
    # a missing label read as "routine" would be exactly the fabricated claim the
    # provenance gate exists to catch.
    routine_label: str | None = None

    # Market, filer, and cohort context. Populated by the enrichment stage; always
    # present so callers never branch on None, but every field inside is optional.
    context: EventContext = Field(default_factory=EventContext)

    raw_payload: dict = Field(default_factory=dict)

    @property
    def effective_usd(self) -> float | None:
        """The USD figure the $5M gate and scoring use.

        Prefers a directly computed usd_value; falls back to implied_usd_value for
        percentage-threshold filings that report no dollar amount.
        """
        if self.usd_value is not None:
            return self.usd_value
        return self.implied_usd_value

    def stable_key(self) -> str:
        """Deterministic dedup key.

        Hash of (jurisdiction, issuer_id, filer_id, transaction_date,
        transaction_type, share_count). Falls back to names when ids are absent so
        two runs over the same filing still collide.
        """
        parts = [
            self.jurisdiction.value,
            (self.issuer_id or self.issuer_name).strip().lower(),
            (self.filer_id or self.filer_name).strip().lower(),
            self.transaction_date.isoformat(),
            self.transaction_type.value,
            "" if self.share_count is None else f"{self.share_count:.4f}",
        ]
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @property
    def event_id(self) -> str:
        return self.stable_key()
