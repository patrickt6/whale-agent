"""Valuation: turn native amounts, share counts, and percentages into USD figures.

Implements :
- Dollars stated in filing -> convert native->USD via the configured `FxProvider`. Note
  that the shipped provider is date-independent, so this is not actually the filing-date
  rate; `enrichment/fx.py` documents what that approximation costs.
- Share counts -> price by preference order, flag as estimate when not filing-stated.
- Percentage thresholds -> implied USD = percent * shares_outstanding * price
  (equivalently market_cap * percent).

The optional `price` provider is what makes branches 2 and 3 work on real filings:
without it `price_used` is only ever whatever the adapter found in the document, so
share-count and percent-only filings stay unvalued. With it, the provider fills the gap
and stamps the resulting `price_source`, so a looked-up estimate is never mistaken for
a filing-stated figure.

**This is where the numbers are born, so this is where a bad one has to be refused.**
`value_event` returns `Valuation | Quarantined` rather than a bare float precisely
because a float can be ignored: every caller that wants a figure now has to look at
which of the two it got. Quarantine is not "clamp it to something believable and carry
on" -- a clamped figure is still a false claim, only harder to notice. It means the
event keeps `usd_value = None`, which the rest of the codebase already reads as "not
learned" rather than as zero, and the reason travels with the row.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from whale_agent.enrichment.fx import FxProvider, rate_to_usd
from whale_agent.enrichment.plausibility import (
    ABSOLUTE_CEILING_USD,
    MAX_PLAUSIBLE_SHARE_PRICE,
    MIN_PLAUSIBLE_SHARE_PRICE,
)
from whale_agent.enrichment.price import PriceProvider
from whale_agent.models.enums import PriceSource
from whale_agent.models.event import NormalizedEvent

# Where a valuation refusal is recorded on the row, alongside the vendor-parse
# rejections written by `ingestion/vendor_types.py`.
QUARANTINE_KEY = "_valuation_quarantine"


@dataclass(frozen=True)
class Valuation:
    """A figure this event can be published with, and what produced it.

    `usd` is `None` when the filing simply carried nothing to value -- a percentage-only
    disclosure with no share count, say. That is a successful valuation of an event with
    no dollar figure, which is a different thing from a refused one, and it renders as
    "not disclosed" exactly as it always has.
    """

    usd: float | None
    inputs: dict = field(default_factory=dict)
    checks_passed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class Quarantined:
    """A figure that was computed, disbelieved, and therefore not kept."""

    reason: str
    inputs: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return False


def _inputs_of(event: NormalizedEvent) -> dict:
    """The fields any figure on this event was derived from. Provenance for the log."""
    return {
        "source": event.source,
        "filer_name": event.filer_name,
        "issuer_name": event.issuer_name,
        "native_amount": event.native_amount,
        "native_currency": event.native_currency,
        "share_count": event.share_count,
        "price_used": event.price_used,
        "price_source": getattr(event.price_source, "value", event.price_source),
        "percent_of_company": event.percent_of_company,
        "shares_outstanding": event.shares_outstanding,
        "fx_rate_used": event.fx_rate_used,
    }


def _quarantine(event: NormalizedEvent, why: str) -> Quarantined:
    """Refuse the figure, leave the event unvalued, and record why on the row."""
    event.usd_value = None
    event.implied_usd_value = None
    result = Quarantined(why, _inputs_of(event))
    event.raw_payload.setdefault(QUARANTINE_KEY, []).append(why)
    return result


def _implausible_price(price: float | None) -> str | None:
    """Why this per-share price cannot be believed, or None if it can."""
    if price is None:
        return None
    if not math.isfinite(price):
        return "per-share price is not a finite number"
    if price <= 0:
        return "per-share price is zero or negative"
    if price < MIN_PLAUSIBLE_SHARE_PRICE:
        return f"per-share price of {price} is below any real listing"
    if price > MAX_PLAUSIBLE_SHARE_PRICE:
        return (
            f"per-share price of {price:,.0f} is implausible; the field most likely "
            "holds a transaction total rather than a unit price"
        )
    return None


def _implausible_total(label: str, value: float) -> str | None:
    """Why this computed dollar figure cannot be believed, or None if it can."""
    if not math.isfinite(value):
        return f"{label} is not a finite number"
    if value > ABSOLUTE_CEILING_USD:
        return (
            f"{label} of {value:,.0f} exceeds the {ABSOLUTE_CEILING_USD:,.0f} ceiling "
            "for any real disclosure"
        )
    return None


def _fill_price(event: NormalizedEvent, price: PriceProvider | None) -> None:
    """Populate `price_used`/`price_source` from the provider when the filing had none."""
    if event.price_used is not None or price is None or not event.ticker:
        return
    quote = price.close_on(event.ticker, event.transaction_date)
    if quote is None:
        return
    event.price_used = quote.price
    event.price_source = quote.source


def _fill_shares_outstanding(event: NormalizedEvent, price: PriceProvider | None) -> None:
    """Populate `shares_outstanding` for percent-of-company filings that omit it."""
    if (
        event.shares_outstanding is not None
        or event.percent_of_company is None
        or price is None
        or not event.ticker
    ):
        return
    event.shares_outstanding = price.shares_outstanding(event.ticker)


def value_event(
    event: NormalizedEvent,
    fx: FxProvider | None = None,
    price: PriceProvider | None = None,
) -> Valuation | Quarantined:
    """Populate usd_value / implied_usd_value / estimate flags in place.

    Returns `Valuation` when the figures can be published and `Quarantined` when they
    cannot. On quarantine the event is left with no dollar figure at all, so a caller
    that ignores the return value still cannot ship a false number -- the discipline the
    return type asks for is about *reporting* the refusal, not about enforcing it.
    """
    checks: list[str] = []

    # 0. Fill in a market price and share count when the filing itself carried neither.
    _fill_price(event, price)
    _fill_shares_outstanding(event, price)

    # The price is an input to both multiplications below, so it is tested once, first.
    bad_price = _implausible_price(event.price_used)
    if bad_price is not None:
        event.price_used = None
        event.price_source = PriceSource.NOT_PRICED
        return _quarantine(event, bad_price)
    if event.price_used is not None:
        checks.append("price_within_band")

    # 1. Direct native dollar amount stated in the filing.
    if event.native_amount is not None:
        rate = rate_to_usd(event.native_currency, event.disclosure_date, fx)
        event.fx_rate_used = rate
        computed = event.native_amount * rate
        bad = _implausible_total("stated amount", computed)
        if bad is not None:
            return _quarantine(event, bad)
        event.usd_value = computed
        checks.append("stated_amount_within_ceiling")
        # Estimate only if the money itself was priced by us, not merely FX-converted.
        event.usd_value_is_estimate = event.price_source not in (
            PriceSource.NOT_PRICED,
            PriceSource.FILING_STATED,
        )

    # 2. Share count * price when no dollar amount was stated.
    elif event.share_count is not None and event.price_used is not None:
        rate = rate_to_usd(event.native_currency, event.disclosure_date, fx)
        event.fx_rate_used = rate
        computed = event.share_count * event.price_used * rate
        bad = _implausible_total("shares x price", computed)
        if bad is not None:
            # The observed bug, refused at the multiplication that creates it.
            return _quarantine(event, bad)
        event.usd_value = computed
        checks.append("shares_x_price_within_ceiling")
        # A filing-stated transaction price is exact; anything else is an estimate.
        event.usd_value_is_estimate = event.price_source != PriceSource.FILING_STATED

    # 3. Percentage-of-company threshold -> implied position value.
    if (
        event.percent_of_company is not None
        and event.shares_outstanding is not None
        and event.price_used is not None
    ):
        rate = rate_to_usd(event.native_currency, event.disclosure_date, fx)
        event.fx_rate_used = rate
        market_cap_usd = event.shares_outstanding * event.price_used * rate
        implied = (event.percent_of_company / 100.0) * market_cap_usd
        bad = _implausible_total("implied position value", implied)
        if bad is not None:
            return _quarantine(event, bad)
        event.implied_usd_value = implied
        checks.append("implied_value_within_ceiling")

    return Valuation(event.effective_usd, _inputs_of(event), tuple(checks))


def value_events(
    events: list[NormalizedEvent],
    fx: FxProvider | None = None,
    price: PriceProvider | None = None,
) -> tuple[list[NormalizedEvent], list[tuple[NormalizedEvent, Quarantined]]]:
    """Value a batch, keeping the refusals rather than discarding them.

    Returns `(events, quarantined)`. The first list is every event, still valued in
    place, because a quarantined row is excluded from the *figures* rather than deleted
    from the record. The second is what the caller turns into a coverage note: a drop
    the reader is never told about is indistinguishable from a quiet week.
    """
    quarantined: list[tuple[NormalizedEvent, Quarantined]] = []
    for event in events:
        result = value_event(event, fx, price)
        if isinstance(result, Quarantined):
            quarantined.append((event, result))
    return events, quarantined
