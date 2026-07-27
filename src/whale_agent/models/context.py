"""`EventContext`: everything we know about an event beyond the filing itself.

The filing says who bought what and when. It does not say whether the company is worth
$200M or $200B, whether the buyer has ever filed before, or whether anyone else bought
the same name that week -- and those are the facts that decide whether an event is
interesting. This is where that context lives.

Two rules govern what may go in here:

1. **Every field is fetched or computed, never recalled.** A value here came from a
   vendor endpoint, from our own database, or from arithmetic over the two. That is what
   lets the thesis layer use these fields freely: they are as checkable as the filing.
2. **Every field is optional.** Enrichment is best-effort. FMP may not cover a Taiwanese
   small cap; a first run has no filer history. A `None` means "we did not learn this",
   never "this is zero", and scoring must treat the two differently.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class EventContext(BaseModel):
    """Market, filer, and cohort context attached to a `NormalizedEvent`."""

    # -- company scale (FMP profile / shares-float) -----------------------------
    # The evidence for insider signal was concentrated in small caps, so company size
    # is the single most useful enrichment we can attach.
    market_cap_usd: float | None = None
    shares_float: float | None = None
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None

    # -- how big is this relative to the company -------------------------------
    # A $6M buy is a real bet in a $200M company and rounding error in Apple. Percent of
    # float is the honest denominator: shares outstanding includes stock nobody can buy.
    percent_of_float: float | None = None
    percent_of_market_cap: float | None = None

    # -- price action around the filing (FMP stock-price-change) ---------------
    # Answers "has the move already happened", which decides whether a disclosure is
    # news or an explanation of last month's chart.
    price_change_1m_pct: float | None = None
    price_change_3m_pct: float | None = None
    price_change_ytd_pct: float | None = None

    # -- what we know about this filer from our own history --------------------
    # `filer_prior_filings` counts how often we have seen them before, which is how
    # "unfamiliar" gets measured rather than looked up. Zero on a first run for
    # everyone, so scoring must not read zero as "obscure" until history exists.
    filer_prior_filings: int | None = None
    filer_last_seen: date | None = None
    filer_days_since_last: int | None = None
    filer_typical_usd: float | None = None  # median of their past disclosed positions

    # -- institutional position change (FMP extract-analytics/holder) ----------
    # The 13F snapshot endpoint cannot distinguish a brand-new position from an
    # existing one; these come from the analytics endpoint, which reports the delta.
    is_new_position: bool | None = None
    is_sold_out: bool | None = None
    change_in_shares_pct: float | None = None
    avg_price_paid: float | None = None

    # -- cohort: what else happened around this issuer this period -------------
    # Computed over the event set, not fetched. `distinct_buyers_this_period` is the
    # cluster signal generalised beyond insiders.
    distinct_buyers_this_period: int | None = None
    sector_buyers_this_period: int | None = None

    # -- federal contract activity (USASpending) -------------------------------
    # Free, keyless, and the one source that can link a position to a subsequent award.
    recent_contract_usd: float | None = None
    recent_contract_agency: str | None = None

    def is_small_cap(self, ceiling_usd: float = 2_000_000_000.0) -> bool | None:
        """True/False when market cap is known, None when it is not.

        Returning None rather than False on unknown matters: an unenriched event should
        not be scored as though we checked and found it large.
        """
        if self.market_cap_usd is None:
            return None
        return self.market_cap_usd < ceiling_usd
