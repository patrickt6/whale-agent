"""Filer / issuer registry entities and the filer-reputation registry.

The registry used to be a fame ranking: recognisable names were multiplied by up to
2.0x, which made the digest a list of people the reader could have named without us. Two
things were wrong with that. Fame is not information -- Berkshire trimming a position is
the same trade whoever does it -- and the largest fame boosts went to Vanguard and
BlackRock, whose filings are the *least* informative events in the dataset. An index
manager's 13G is a mechanical consequence of fund flows into the index; nobody decided
anything, so it should rank below an unremarkable filing by someone who did.

So this module now answers a narrower question: does this filer's involvement change
what the filing means?

  * Passive/index managers: yes, downwards. Their presence is evidence the position was
    not chosen. They sit below baseline.
  * Activists and concentrated operators: yes, mildly upwards. An Elliott 13D changes
    the range of outcomes for the issuer in a way an anonymous 13D does not.
  * Everyone else, famous or not: no. Baseline.

The whole registry now spans roughly [0.9, 1.25] instead of [1.0, 2.0], which makes it a
tiebreaker rather than a ranking. The work of finding interesting events is done by the
measured terms in `scoring.score` -- company size, position size relative to the company,
and what we have observed about this filer before.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from whale_agent.models.enums import FilerType

# The most a name alone may move a score, in either direction. Deliberately small: any
# term that can reorder the digest should be measured, not recalled.
MAX_FAME_MULTIPLIER = 1.25
PASSIVE_MULTIPLIER = 0.9  # below baseline even before the 13G-specific discount


class Entity(BaseModel):
    """A filer or issuer. `entity_id` is a stable cross-source id (ISIN/LEI/CIK/CUSIP)."""

    entity_id: str
    name: str
    filer_type: FilerType = FilerType.UNKNOWN
    aliases: list[str] = Field(default_factory=list)
    # AUM or net-worth proxy in USD, used by the conviction multiplier. None if unknown.
    size_usd: float | None = None
    notability: float = 1.0  # 0.9 (passive/index) .. 1.25 (activist)


# Filers whose involvement genuinely changes the situation for the issuer. Keyed by a
# normalized (lowercased, stripped) name, matched by exact key then substring.
_ACTIVE_INVOLVEMENT: dict[str, float] = {
    # Activists: a 13D from these names is a credible threat of a board fight, which is
    # a different event from the same stake accumulated quietly.
    "elliott management": 1.25,
    "elliott investment": 1.25,
    "starboard value": 1.25,
    "pershing square": 1.25,
    "bill ackman": 1.25,
    "icahn": 1.25,
    "carl icahn": 1.25,
    "third point": 1.2,
    "trian": 1.2,
    "jana partners": 1.2,
    "engine capital": 1.15,
    "value act": 1.2,
    "valueact": 1.2,
    # Concentrated operators: run few positions, so any position is a real decision.
    # Modest, because a concentrated book is a weaker claim than an activist mandate.
    "berkshire hathaway": 1.1,
    "warren buffett": 1.1,
    "pabrai": 1.1,
    "greenlight capital": 1.1,
    "baupost": 1.1,
}

# Passive and index managers. A 13G from any of these is the lowest-information event we
# ingest: they hold essentially everything, and the filing tracks flows into the fund
# rather than a view on the issuer. Includes sovereign funds that index (Norges runs a
# benchmark-tracking book) and the index arms of otherwise-active houses.
_PASSIVE_MANAGERS: frozenset[str] = frozenset(
    {
        "vanguard",
        "blackrock",
        "ishares",
        "state street",
        "ssga",
        "geode capital",
        "northern trust",
        "dimensional fund",
        "fidelity index",
        "fmr index",
        "charles schwab investment",
        "invesco",
        "norges bank",
        "nbim",
        "index fund",
        "index trust",
        "index advisors",
    }
)


def normalize_name(name: str) -> str:
    return " ".join(name.lower().split())


def is_passive_manager(name: str | None) -> bool:
    """True if the filer is an index/passive vehicle whose filings are mechanical."""
    if not name:
        return False
    key = normalize_name(name)
    return any(token in key for token in _PASSIVE_MANAGERS)


def lookup_notability(name: str | None) -> float:
    """Return the filer-reputation multiplier for a name.

    Range is roughly [0.9, 1.25]: 0.9 for passive/index managers, 1.0 for a name we hold
    no view on (the overwhelming majority, including most famous people), and up to 1.25
    for filers whose involvement changes the issuer's situation.

    Passive detection wins over the active registry so that, e.g., "BlackRock Index
    Trust" cannot be talked into a boost by a substring match.
    """
    if not name:
        return 1.0
    if is_passive_manager(name):
        return PASSIVE_MULTIPLIER
    key = normalize_name(name)
    if key in _ACTIVE_INVOLVEMENT:
        return _ACTIVE_INVOLVEMENT[key]
    for seed, mult in _ACTIVE_INVOLVEMENT.items():
        if seed in key:
            return mult
    return 1.0
