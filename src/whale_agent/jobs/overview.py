"""The two-week overview: every category the product covers, in one table.

The weekly report leads with research notes, which are deep and narrow -- one or two
issuers, argued properly. This is the opposite view and it exists because the reader asked
for it: a shallow, wide glance at everything disclosed across a fortnight, sorted into the
four categories he thinks in.

Two decisions shape the whole module.

**Every section renders even when it is empty.** Omitting a quiet section would read as
"nothing happened there", which is a claim we cannot support and, right now, would be
false three times out of four: institutional and international coverage is thin because
the adapters that would fill them are unwired or unkeyed, not because the world was quiet.
An empty section with its own explanation tells the reader we looked and came back with
nothing, which is the honest report and also the thing that gets the gaps fixed.

**Congressional rows are ranked by size, never gated on it.** STOCK Act disclosures are
bands, and the code takes the lower bound of the band rather than inventing a midpoint, so
almost nothing in that category will ever clear $5M. Applying the gate here would empty
the section permanently and tell the reader Congress had stopped trading. The gate belongs
to the whale question; this table answers a different one.

A fortnight rather than a week because a single week of one category is often two rows,
and two rows is not an overview. It is the same reason the thesis layer runs weekly rather
than daily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from whale_agent.enrichment.roles import role_for
from whale_agent.models.enums import FilerType, Jurisdiction, TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.summarization.render import format_usd

__all__ = ["OVERVIEW_SECTIONS", "OverviewRow", "Section", "build_overview", "OVERVIEW_DAYS"]

OVERVIEW_DAYS = 14

# Order is the reader's, not the data's: he named these four, in this order.
OVERVIEW_SECTIONS: list[tuple[str, str]] = [
    ("insider", "Insider filings"),
    ("congressional", "Congressional trading"),
    ("institutional", "Institutional buying"),
    ("international", "International plays"),
]

# What each section says when it has nothing, so an empty table is still informative.
# These are statements about our coverage, not about the market, and they are written to
# be embarrassing enough to get fixed.
EMPTY_NOTES: dict[str, str] = {
    "insider": "No insider filing in this window cleared the threshold.",
    "congressional": (
        "No congressional disclosure landed in this window. Note that these are reported "
        "in bands and we take the lower bound, so the figures here understate by design."
    ),
    "institutional": (
        "Nothing here yet. 13F institutional holdings are not ingested. Only activist "
        "and passive stake filings reach this section, and none landed in this window."
    ),
    "international": (
        "Nothing here yet. Taiwan is the only non-US source currently switched on, and a "
        "Taiwanese filing reports share counts rather than dollars, so it needs a resolved "
        "price before it can appear at all. Japan requires an API key that is not set."
    ),
}

# Filings that represent somebody taking or changing a declared stake, as opposed to an
# individual's compensation or an ordinary open-market trade.
_STAKE_TYPES = {
    TransactionType.ACTIVIST_13D,
    TransactionType.PASSIVE_13G,
    TransactionType.FUND_NEW_POSITION,
    TransactionType.FUND_ADD_POSITION,
}

_INSTITUTIONAL_FILERS = {FilerType.FUND, FilerType.SWF, FilerType.FAMILY_OFFICE}


@dataclass(frozen=True)
class OverviewRow:
    """One line in the table. Everything already formatted for display."""

    filer: str
    filer_role: str | None
    issuer: str
    ticker: str
    what: str
    when: date
    amount_usd: float | None
    amount_label: str
    is_estimate: bool
    filing_url: str
    jurisdiction: str


@dataclass
class Section:
    key: str
    label: str
    rows: list[OverviewRow] = field(default_factory=list)
    empty_note: str = ""

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def priced_count(self) -> int:
        return sum(1 for r in self.rows if r.amount_usd is not None)

    @property
    def total_usd(self) -> float:
        """Sum of the rows that have a figure. Unpriced rows contribute nothing.

        Not zero -- nothing. A row we could not value is not a row worth zero dollars,
        and adding it as one would understate the total while looking complete.
        """
        return sum(r.amount_usd for r in self.rows if r.amount_usd is not None)


def _is_congressional(event: NormalizedEvent) -> bool:
    return "congress" in (event.source or "").lower()


def section_for(event: NormalizedEvent) -> str:
    """Exactly one section per event, decided in a fixed order.

    Order matters and is not alphabetical. Congressional is tested before anything else
    because a member of Congress is also an individual and would otherwise fall into the
    insider bucket. International is tested before the domestic categories because "which
    market" is the coarser question, and a Taiwanese fund buying a Taiwanese company is an
    international play first.
    """
    if _is_congressional(event):
        return "congressional"
    if event.jurisdiction not in (Jurisdiction.US, None):
        return "international"
    if event.filer_type in _INSTITUTIONAL_FILERS or event.transaction_type in _STAKE_TYPES:
        return "institutional"
    return "insider"


def _describe(event: NormalizedEvent) -> str:
    return (event.transaction_type.value if event.transaction_type else "disclosure").replace(
        "_", " "
    )


def _row(event: NormalizedEvent) -> OverviewRow:
    value = event.effective_usd
    return OverviewRow(
        filer=event.filer_name or "unnamed filer",
        filer_role=role_for(event),
        issuer=event.issuer_name or (event.ticker or "unnamed issuer"),
        ticker=(event.ticker or "").upper(),
        what=_describe(event),
        when=event.disclosure_date,
        amount_usd=value,
        # "not disclosed" rather than "$0": the distinction is the whole product.
        amount_label=format_usd(value) if value is not None else "not disclosed",
        is_estimate=bool(event.usd_value_is_estimate),
        filing_url=event.source_url or "",
        jurisdiction=event.jurisdiction.value if event.jurisdiction else "",
    )


def build_overview(
    events: list[NormalizedEvent], on: date, days: int = OVERVIEW_DAYS, per_section: int = 12
) -> list[Section]:
    """Sort a fortnight of events into the reader's four categories.

    Rows are ranked by disclosed size within a section, with unpriced rows last: they are
    the least certain thing in the table and should not lead it, but dropping them would
    hide filings that happened.
    """
    cutoff = on - timedelta(days=days)
    sections = {
        key: Section(key=key, label=label, empty_note=EMPTY_NOTES.get(key, ""))
        for key, label in OVERVIEW_SECTIONS
    }

    for event in events:
        if event.disclosure_date and event.disclosure_date < cutoff:
            continue
        sections[section_for(event)].rows.append(_row(event))

    for section in sections.values():
        # `amount_usd is None` sorts last: False < True, so priced rows come first.
        section.rows.sort(key=lambda r: (r.amount_usd is None, -(r.amount_usd or 0.0)))
        del section.rows[per_section:]

    return [sections[key] for key, _ in OVERVIEW_SECTIONS]
