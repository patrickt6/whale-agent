"""The weekly issue: one publication covering everything, assembled in code.

This replaces the previous shape, which was one thin page per detected pattern. That shape
produced an article about a single company with three sentences in it, and the owner's
verdict on it was correct: a reader who opens a weekly expects the week, not one filing
cluster with a chart.

So an issue has a spine that does not depend on anything having happened:

    1. What surfaced        the week in a paragraph, always present
    2. Insider filings      who bought, who sold, and who they are
    3. Congressional        disclosed bands, ranked, never gated
    4. Institutional        stakes and fund positions
    5. International        non-US filings
    6. Rates and credit     the macro backdrop, dated
    7. Method and evidence  the permanent block

Every section renders even when it has nothing, carrying a sentence about why. That is the
same rule the overview tables already follow and it exists because an omitted section reads
as "nothing happened" when the truth is usually "we cannot see it yet".

**All prose here is deterministic.** It is built from the events by counting, ranking and
comparing, so every sentence is reproducible from the same rows and every figure in it is
one the reader can re-add from the table below it. That is not a stopgap for the LLM being
unavailable. It is the arrangement the product's claim requires: the model may improve how
a sentence reads, and it never decides what the sentence says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from whale_agent.jobs.overview import Section
from whale_agent.summarization.render import format_usd

__all__ = ["IssueSection", "Issue", "build_issue"]


@dataclass
class IssueSection:
    """One numbered part of the issue: a heading, some prose, and its evidence."""

    key: str
    number: int
    heading: str
    paragraphs: list[str] = field(default_factory=list)
    section: Section | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        return f"s{self.number}-{self.key}"


@dataclass
class Issue:
    published_on: date
    title: str
    deck: str
    standfirst: list[str]
    sections: list[IssueSection]

    @property
    def contents(self) -> list[tuple[str, str]]:
        """Anchor and heading for the directory at the top."""
        return [(s.anchor, s.heading) for s in self.sections]


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _named(rows, limit: int = 3) -> str:
    """A readable list of filers, with their roles when we know them.

    Roles are the point. "Six filers bought" is a count; "the chief executive and two
    directors bought" is the finding, and it is the distinction the research rests on.
    """
    parts = []
    for row in rows[:limit]:
        who = row.filer
        if row.filer_role:
            who = f"{who} ({row.filer_role.lower()})"
        parts.append(who)
    if len(rows) > limit:
        parts.append(f"and {len(rows) - limit} more")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _direction_split(section: Section) -> tuple[list, list]:
    buys = [r for r in section.rows if "buy" in r.what]
    sells = [r for r in section.rows if "sell" in r.what]
    return buys, sells


def _insider_prose(section: Section) -> list[str]:
    if not section.rows:
        return [
            "No insider filing cleared the threshold in this window. That is a normal "
            "outcome for a quiet fortnight and not a gap in coverage."
        ]
    buys, sells = _direction_split(section)
    bought = sum(r.amount_usd or 0 for r in buys)
    sold = sum(r.amount_usd or 0 for r in sells)
    out = []

    lead = (
        f"{len(section.rows)} insider {_plural(len(section.rows), 'filing', 'filings')} "
        f"cleared the threshold this fortnight."
    )
    if buys and sells:
        lead += (
            f" {format_usd(bought)} of it was buying across "
            f"{len(buys)} {_plural(len(buys), 'filing', 'filings')}, and {format_usd(sold)} "
            f"was selling across {len(sells)}."
        )
    elif buys:
        lead += f" All of it was buying, {format_usd(bought)} in total."
    elif sells:
        lead += f" All of it was selling, {format_usd(sold)} in total."
    out.append(lead)

    # The asymmetry is the one thing the literature is confident about, so it is stated
    # every week rather than only when it is convenient.
    if sells and bought < sold:
        out.append(
            "Selling outweighed buying. That is the ordinary state of the world and "
            "carries far less information than the reverse: people sell for a hundred "
            "reasons and buy for one."
        )
    elif buys and bought > sold:
        out.append(
            "Buying outweighed selling, which is the less common direction and the one "
            "the research says is worth reading."
        )

    if buys:
        out.append(f"The largest disclosed purchases came from {_named(buys)}.")
    biggest = section.rows[0]
    who = biggest.filer + (f", {biggest.filer_role}," if biggest.filer_role else "")
    out.append(
        f"The single largest filing in the section is {who} at {biggest.amount_label} "
        f"in {biggest.issuer}, disclosed {biggest.when.isoformat()}."
    )
    return out


def _congress_prose(section: Section) -> list[str]:
    if not section.rows:
        return [
            "No congressional disclosure landed in this window. These are reported in "
            "bands rather than exact amounts, and we take the lower bound of the band, so "
            "the figures in this section understate by construction."
        ]
    total = section.total_usd
    return [
        f"{len(section.rows)} congressional {_plural(len(section.rows), 'disclosure', 'disclosures')} "
        f"landed this fortnight, totalling at least {format_usd(total)}.",
        "Members disclose a band rather than an amount. Every figure here is the bottom of "
        "the band the filer reported, never a midpoint, because a midpoint is a number "
        "nobody disclosed. Read these as a floor.",
        f"The largest was {_named(section.rows, 1)} at {section.rows[0].amount_label} "
        f"in {section.rows[0].issuer}.",
    ]


def _institutional_prose(section: Section) -> list[str]:
    if not section.rows:
        return [
            "Nothing in this section this fortnight. Worth being precise about why: 13F "
            "institutional holdings are not ingested at all, so only activist and passive "
            "stake filings can reach it. This section is thin because of what we collect, "
            "not because institutions were idle."
        ]
    return [
        f"{len(section.rows)} stake {_plural(len(section.rows), 'filing', 'filings')} "
        f"this fortnight, totalling {format_usd(section.total_usd)} where a value was disclosed.",
        "A stake filing states a position rather than a trade, and the largest holders are "
        "often index funds whose filings follow inflows rather than a view. Those are kept "
        "in the record and ranked below filers who chose the position.",
    ]


def _international_prose(section: Section) -> list[str]:
    if not section.rows:
        return [
            "No non-US filing appeared this fortnight. Coverage here is genuinely partial: "
            "Taiwan and Japan are switched on, and the remaining jurisdictions in the plan "
            "have no adapter yet. A Taiwanese or Japanese filing also reports share counts "
            "rather than dollars, so it needs a resolved price before it can clear the "
            "threshold at all.",
        ]
    return [
        f"{len(section.rows)} non-US {_plural(len(section.rows), 'filing', 'filings')} "
        f"cleared the threshold, totalling {format_usd(section.total_usd)}.",
        "Non-US disclosure regimes report holdings rather than transactions, so these are "
        "positions as at a date rather than something bought on a day.",
    ]


_PROSE = {
    "insider": _insider_prose,
    "congressional": _congress_prose,
    "institutional": _institutional_prose,
    "international": _international_prose,
}

_HEADINGS = {
    "insider": "Insider filings",
    "congressional": "Congressional trading",
    "institutional": "Institutional and stake filings",
    "international": "International",
}


def _standfirst(sections: list[Section], cleared: int, recorded: int) -> list[str]:
    """The opening. States the size of the week and what is worth turning to."""
    by_key = {s.key: s for s in sections}
    insider = by_key.get("insider")
    lines = [
        f"{cleared} disclosed {_plural(cleared, 'move', 'moves')} cleared the $5M "
        f"threshold this week, out of {recorded} filings recorded."
    ]
    if insider and insider.rows:
        buys, sells = _direction_split(insider)
        if buys:
            top = buys[0]
            lines.append(
                f"The buying worth looking at first is {top.filer} in {top.issuer}, "
                f"{top.amount_label} disclosed {top.when.isoformat()}."
            )
    lines.append(
        "Everything below is copied from a filing or computed from filing fields. Where a "
        "figure was not disclosed it says so rather than showing a zero, and every row "
        "links to the document it came from."
    )
    return lines


def build_issue(
    on: date,
    cleared: int,
    recorded: int,
    sections: list[Section],
    macro_lines: list[str],
    macro_sourcing: str,
    how_to_read: str,
) -> Issue:
    """Assemble the whole issue. Pure: same inputs give the same issue."""
    by_key = {s.key: s for s in sections}
    parts: list[IssueSection] = []

    parts.append(
        IssueSection(
            key="surfaced",
            number=1,
            heading="What surfaced this week",
            paragraphs=_standfirst(sections, cleared, recorded),
        )
    )

    for index, key in enumerate(
        ("insider", "congressional", "institutional", "international"), start=2
    ):
        section = by_key.get(key)
        parts.append(
            IssueSection(
                key=key,
                number=index,
                heading=_HEADINGS[key],
                paragraphs=_PROSE[key](section) if section else [],
                section=section,
            )
        )

    macro_paras = (
        [
            "Background for the filings above, as context rather than cause. Nothing here "
            "establishes that a rate or a credit figure is why anybody bought anything.",
        ]
        if macro_lines
        else [
            "No macro series were readable this run. Rates come from the Treasury's keyless "
            "endpoint, and bank credit and payrolls need a FRED key that is not set."
        ]
    )
    parts.append(
        IssueSection(
            key="macro",
            number=6,
            heading="Rates, credit and macro",
            paragraphs=macro_paras + ([macro_sourcing] if macro_sourcing else []),
            lines=macro_lines,
        )
    )

    # The long method block is gone at the owner's instruction. What replaces it is one
    # line, kept because it is the only sentence in the issue that says what this is:
    # an awareness tool rather than advice. It is a sentence, not a spiel.
    parts.append(
        IssueSection(
            key="about",
            number=7,
            heading="About these figures",
            paragraphs=[
                "Every figure above is copied from a public filing or computed from filing "
                "fields, and each row links to the document it came from. This is an "
                "awareness tool, not investment advice."
            ],
        )
    )

    insider = by_key.get("insider")
    # The largest BUY, not the largest filing. The biggest row in a week is routinely a
    # compensation grant, and "led by" a grant overstates it: nobody chose to buy anything.
    top = None
    if insider and insider.rows:
        buys, _ = _direction_split(insider)
        top = buys[0] if buys else None
    title = (
        f"{cleared} disclosed moves cleared $5M this week"
        if cleared
        else "A quiet week in disclosed positions"
    )
    deck = (
        f"Led by {top.filer} buying {top.amount_label} of {top.issuer}. "
        "Insider, congressional, institutional and international filings, with the macro backdrop."
        if top
        else "Insider, congressional, institutional and international filings, with the macro backdrop."
    )
    return Issue(
        published_on=on,
        title=title,
        deck=deck,
        standfirst=[],
        sections=parts,
    )
