"""One fund is one whale, however many registrants it files under.

Situational Awareness files as at least four: LP (0002045724) carries the 13G and the
Form 4, Partners LP (0002038540) carries the 13F and is the name that appears in
SharonAI's S-1 selling-securityholder table, and two more are quiet. Treating those as
separate entities is why the report showed fragments of one fund as strangers.

Names matter as much as CIKs here. A resale registration names a holder in prose; there
is no CIK in that table, so matching has to work from the string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Whale:
    """A fund, its registrants, and the names it is written under."""

    name: str
    ciks: tuple[str, ...]
    aliases: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        """True if `text` names this whale.

        Word-boundary matching, because "Situational Awareness" is also an ordinary
        phrase in defence and AI writing and a bare substring test produces constant
        false positives. An alias must appear as a whole phrase followed by an entity
        suffix or end of name.
        """
        cleaned = re.sub(r"\s+", " ", text or "").strip().lower()
        if not cleaned:
            return False
        for alias in (self.name, *self.aliases):
            pattern = (
                rf"\b{re.escape(alias.lower())}\b"
                r"(?=\s+(?:lp|llc|inc|ltd|l\.p\.|l\.l\.c\.|partners|capital|management"
                r"|holdings|corp|company|co\b)|\s*$)"
            )
            if re.search(pattern, cleaned):
                return True
        return False


# CIKs resolved against EDGAR rather than typed from memory: a wrong one silently
# reports a different fund's filings.
WHALES: tuple[Whale, ...] = (
    Whale(
        name="Situational Awareness",
        ciks=("0002045724", "0002038540", "0002048430", "0002047424"),
        aliases=("Situational Awareness LP", "Situational Awareness Partners"),
    ),
    Whale("Berkshire Hathaway", ("0001067983",)),
    Whale("Pershing Square", ("0001336528",), ("Pershing Square Capital",)),
    Whale("Scion Asset Management", ("0001649339",), ("Scion Asset",)),
    Whale("Bridgewater Associates", ("0001350694",), ("Bridgewater",)),
    Whale("Third Point", ("0001040273",)),
    Whale("Duquesne Family Office", ("0001536411",), ("Duquesne",)),
    Whale("Coatue Management", ("0001135730",), ("Coatue",)),
    Whale("Tiger Global Management", ("0001167483",), ("Tiger Global",)),
    Whale("Lone Pine Capital", ("0001061165",), ("Lone Pine",)),
)

_BY_CIK = {cik: whale for whale in WHALES for cik in whale.ciks}


def whale_for_cik(cik: str) -> Whale | None:
    return _BY_CIK.get((cik or "").strip().zfill(10))


def whale_for_name(text: str) -> Whale | None:
    for whale in WHALES:
        if whale.matches(text):
            return whale
    return None


def all_ciks() -> tuple[str, ...]:
    return tuple(_BY_CIK)
