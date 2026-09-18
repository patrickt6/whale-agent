"""The subject line, in the register of a show rundown rather than a report title.

House style, taken from the TBPN example in `docs/example-tbpn-headline.md`:

    Leopold Stays in the Game, Big Tech Earnings, OpenAI Slashes GPT-5.6 Prices
    Stripe's $53B PayPal Offer, OpenAI's First Device, TBPN's New Business Ideas
    AMD Advancing AI, Google Q2 Earnings, OpenAI Plans $750B Cloud Spend

The rules that produces: two to four clauses, comma-separated, each a handful of words,
active voice, a named subject in every one, figures kept in the clause rather than
explained, and no connective tissue between clauses. No SEC form codes: a reader does
not know what a 13D or 13G is, so the subject line says what happened in plain words
(discloses a stake, reports a trade), never the form name. "Whale weekly, 2026-08-03"
tells the reader nothing; "Duquesne Discloses a Stake, Six Insiders Back Five Star
Bancorp" tells them whether to open it.

**Composed, not written.** Every clause is built from a fact the report already computed
and can re-derive on the page below. No model writes this. That is not a stylistic
preference: a subject line is the one string a reader sees before deciding to trust the
email, and it is the worst possible place for a fluent guess. The vocabulary here is
fixed, so the range of sentences is small and every one of them is checkable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MAX_CLAUSES = 4
MIN_CLAUSES = 1

# Spelled out to nine, the way a headline writes them.
_WORDS = {
    2: "Two",
    3: "Three",
    4: "Four",
    5: "Five",
    6: "Six",
    7: "Seven",
    8: "Eight",
    9: "Nine",
}


def _count_word(n: int) -> str:
    return _WORDS.get(n, str(n))


def _short_money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B".replace(".0B", "B")
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def _trim_issuer(name: str) -> str:
    """Drop the corporate suffix. A headline says Five Star Bancorp, not Bancorp Inc."""
    out = name.strip().rstrip(".,")
    for suffix in (
        " Corporation",
        " Corp.",
        " Corp",
        " Incorporated",
        " Inc.",
        " Inc",
        " Limited",
        " Ltd.",
        " Ltd",
        " PLC",
        " plc",
        " L.P.",
        " LP",
        " Company",
        " Co.",
        " Co",
        " Holdings",
        " Group",
        " N.V.",
        " S.A.",
    ):
        if out.endswith(suffix):
            out = out[: -len(suffix)].strip().rstrip(",")
    return out or name.strip()


def _short_manager(name: str) -> str:
    """First distinctive word or two of a manager's name."""
    out = _trim_issuer(name)
    for suffix in (" Capital", " Management", " Asset", " Partners", " Family Office"):
        if out.endswith(suffix):
            out = out[: -len(suffix)].strip()
    return out or name


def _form_verb_object(form: str) -> tuple[str, str]:
    """A plain verb and object for the filing type. No SEC form codes in the subject
    line: a reader does not know what a 13D is, and STE100 bars unexplained jargon.
    """
    f = form.upper().strip()
    if f.startswith("SCHEDULE 13D") or f.startswith("SCHEDULE 13G"):
        return "Discloses", "a Stake"
    if f in {"3", "4", "5"}:
        return "Reports", "a Trade"
    return "Files", "a Report"


@dataclass(frozen=True)
class HeadlineFacts:
    """Everything the subject line is allowed to draw on."""

    cluster_issuer: str = ""
    cluster_filers: int = 0
    cluster_usd: float = 0.0
    largest_issuer: str = ""
    largest_usd: float = 0.0
    fund_name: str = ""
    fund_form: str = ""
    fund_count: int = 0
    cleared: int = 0


def clauses_from(facts: HeadlineFacts) -> list[str]:
    """Ordered candidate clauses, most newsworthy first."""
    out: list[str] = []

    # A fund filing a stake is the freshest thing the report knows: days old, not months.
    if facts.fund_name and facts.fund_form:
        subject = _short_manager(facts.fund_name)
        verb, obj = _form_verb_object(facts.fund_form)
        if facts.fund_count > 1:
            out.append(f"{subject} {verb} {obj} and More")
        else:
            out.append(f"{subject} {verb} {obj}")

    # Several unrelated buyers into one name is the pattern the product exists to find.
    if facts.cluster_issuer and facts.cluster_filers >= 2:
        who = f"{_count_word(facts.cluster_filers)} Insiders"
        issuer = _trim_issuer(facts.cluster_issuer)
        if facts.cluster_usd >= 1_000_000:
            out.append(f"{who} Put {_short_money(facts.cluster_usd)} Into {issuer}")
        else:
            out.append(f"{who} Back {issuer}")

    if facts.largest_issuer and facts.largest_usd > 0:
        out.append(
            f"{_short_money(facts.largest_usd)} Into {_trim_issuer(facts.largest_issuer)}"
        )

    if facts.cleared:
        out.append(f"{facts.cleared} Moves Clear the Bar")

    return out


def build_headline(facts: HeadlineFacts, on: date) -> str:
    """The subject line. Falls back to the dated form when the week is empty."""
    clauses = clauses_from(facts)
    if not clauses:
        return f"Whale Weekly, {on.isoformat()}"
    seen: list[str] = []
    for clause in clauses:
        if clause not in seen:
            seen.append(clause)
    return ", ".join(seen[:MAX_CLAUSES])
