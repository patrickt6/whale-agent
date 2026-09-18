"""The macro backdrop, rendered for a reader, and keyed to a note only when it fits.

`enrichment/macro.py` has been able to fetch this since it was written and nothing ever
called it. This module is the missing half: it turns observations into lines a person can
read, and decides when a macro figure has any business appearing next to a cluster of
filings.

Three rules, and the third is the one that matters.

**Every figure carries its own date, and a stale one says how stale.** The providers
publish on different lags, so a snapshot is never uniformly current. Printing a June
figure in a late July report without its date is a wrong number in a document whose whole
claim is that its numbers are checkable.

**A proxy says it is a proxy.** Treasury's keyless endpoint reports average interest on
bills and notes. That is a reasonable stand in for the policy rate and the long yield, and
it is not the fed funds target or the 10 year constant maturity. Labelling it as those
would be the same category of error as inventing a figure, just harder to catch.

**A macro line is attached to a note only when the link is defensible.** Bank credit next
to a cluster of bank insiders is context. The same figure next to a biotech cluster is
decoration pretending to be analysis. Sector is populated on a small minority of stored
rows, so most notes get the rates backdrop and no sector claim at all. Silence is the
correct output when we do not know the sector, and it is the common case.

Nothing here asserts causation. The backdrop says which quarter it is. It never says the
quarter is why anybody bought anything.
"""

from __future__ import annotations

from datetime import date

from whale_agent.enrichment.macro import (
    BANK_CREDIT,
    POLICY_RATE,
    SECTOR_EMPLOYMENT,
    TEN_YEAR_YIELD,
    MacroObservation,
)
from whale_agent.models.event import NormalizedEvent

__all__ = ["backdrop_lines", "macro_note_for", "sectors_in", "MACRO_LABELS"]

# Honest names. The proxy caveat is added at render time from the observation itself, so
# these stay true whichever provider supplied the figure.
MACRO_LABELS: dict[str, str] = {
    POLICY_RATE: "Policy rate",
    TEN_YEAR_YIELD: "Ten year yield",
    BANK_CREDIT: "Bank credit outstanding",
    SECTOR_EMPLOYMENT: "Manufacturing payrolls",
}

# Order the reader thinks in: the price of money, then the long end, then the credit
# cycle, then the real economy.
MACRO_ORDER = (POLICY_RATE, TEN_YEAR_YIELD, BANK_CREDIT, SECTOR_EMPLOYMENT)

# Past this, a figure is old enough that presenting it without comment would mislead.
STALE_AFTER_DAYS = 21

# Which sectors a series can honestly be placed beside. Deliberately narrow: an empty
# entry means the series is never keyed to a narrative, only shown in the backdrop.
SECTOR_RELEVANCE: dict[str, frozenset[str]] = {
    BANK_CREDIT: frozenset({"Financial Services", "Financials", "Real Estate"}),
    SECTOR_EMPLOYMENT: frozenset({"Industrials", "Basic Materials"}),
    POLICY_RATE: frozenset(),
    TEN_YEAR_YIELD: frozenset(),
}


def _format_value(obs: MacroObservation) -> str:
    if obs.units == "percent":
        return f"{obs.value:.2f}%"
    if obs.units == "USD billions":
        return f"${obs.value:,.0f}bn"
    return f"{obs.value:,.0f} {obs.units}".strip()


def _line(obs: MacroObservation, on: date) -> str:
    age = (on - obs.as_of).days
    parts = [f"{MACRO_LABELS.get(obs.series, obs.series)}: {_format_value(obs)}"]
    parts.append(f"as of {obs.as_of.isoformat()}")
    if age > STALE_AFTER_DAYS:
        parts.append(f"{age} days old")
    if obs.is_proxy:
        parts.append("proxy, see sourcing")
    return ", ".join(parts) + "."


def backdrop_lines(snapshot: dict[str, MacroObservation], on: date) -> list[str]:
    """One line per series we actually have. No placeholders for the ones we do not."""
    return [_line(snapshot[s], on) for s in MACRO_ORDER if s in snapshot]


def sectors_in(events: list[NormalizedEvent]) -> set[str]:
    """Sectors present on these events. Unknown is not a sector and is not returned."""
    found: set[str] = set()
    for event in events:
        sector = getattr(event.context, "sector", None) if event.context else None
        if sector:
            found.add(sector)
    return found


def macro_note_for(
    events: list[NormalizedEvent], snapshot: dict[str, MacroObservation], on: date
) -> str | None:
    """A backdrop sentence for this note, or None when no link is defensible.

    Returns None far more often than not, which is intended. A sentence that appears on
    every article regardless of what the article is about stops being read by the second
    week, and would be decoration rather than context.
    """
    sectors = sectors_in(events)
    if not sectors:
        return None

    relevant = [
        snapshot[series]
        for series in MACRO_ORDER
        if series in snapshot and SECTOR_RELEVANCE.get(series, frozenset()) & sectors
    ]
    if not relevant:
        return None

    named = ", ".join(sorted(sectors))
    figures = " ".join(_line(obs, on) for obs in relevant)
    return (
        f"Backdrop for the {named} names above, as context rather than cause. {figures} "
        "These filings are dated disclosures, and nothing here establishes that the two "
        "are connected."
    )
