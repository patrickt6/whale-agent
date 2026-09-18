"""Currency conversion to USD.

**The shipped provider is date-independent, and that is a real limitation, not a
detail.** `FxProvider.rate_to_usd` takes an `on` date because a correct implementation
needs one, but `StaticFxProvider` ignores it and returns a fixed approximate rate from
the table below. A filing from eighteen months ago is therefore converted at today's
rough rate, not at the rate that prevailed when it was filed.

What that costs, stated plainly so nobody has to discover it from the code: for a
non-USD filing near the threshold, a rate that has moved 10% since the filing date can
move the USD figure across the $5M gate in either direction. Every USD figure on a
non-USD filing is approximate to roughly the size of the currency move since filing.
USD filings -- the large majority of the corpus -- are unaffected, since the conversion
is the identity.

This is deliberate for Phase 0: it keeps the deterministic core free of a network
dependency, which is what lets the whole suite run offline. The interface is the seam a
live provider (ECB reference rates, exchangerate.host) drops into, and the day one does,
the `on` argument starts being honoured with no change at any call site.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol


class FxProvider(Protocol):
    def rate_to_usd(self, currency: str, on: date) -> float:
        """Units of USD per 1 unit of `currency` on `on`. USD->USD is 1.0."""
        ...


# Static fallback rates (USD per 1 unit of currency). Deliberately approximate; a
# live provider replaces this. Covers the currencies the supported jurisdictions file in.
_STATIC_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.73,
    "AUD": 0.66,
    "JPY": 0.0064,
    "TWD": 0.031,
    "KRW": 0.00073,
    "HKD": 0.128,
    "INR": 0.012,
    "CNY": 0.138,
    "BRL": 0.18,
    "SGD": 0.74,
}


class StaticFxProvider:
    """Date-independent static rates for Phase 0."""

    def __init__(self, rates: dict[str, float] | None = None) -> None:
        self._rates = rates or dict(_STATIC_RATES)

    def rate_to_usd(self, currency: str, on: date) -> float:
        cur = currency.upper()
        if cur not in self._rates:
            raise KeyError(f"No FX rate for currency {cur!r}")
        return self._rates[cur]


_DEFAULT_PROVIDER = StaticFxProvider()


def rate_to_usd(currency: str, on: date, provider: FxProvider | None = None) -> float:
    return (provider or _DEFAULT_PROVIDER).rate_to_usd(currency, on)
