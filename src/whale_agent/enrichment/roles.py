"""What the filer is to the company: director, CFO, ten percent owner.

This is the highest value field the product was not showing. "HALVORSEN ERIK R bought
$131.4M of Meridian Pharmaceuticals" is a fact the reader cannot weigh. "Erik Halvorsen,
Director and CEO" is the same fact with the thing that makes it interesting attached, and
the research the whole product rests on is specifically about insiders, meaning people
with a defined relationship to the issuer.

We already had it. FMP sends `typeOfOwner` on 96 percent of rows and it was being parked
in `raw_payload` and never read. SEC ships the same information as separate booleans plus
a free text officer title, and the Form 4 parser was not reading those at all.

Reading from `raw_payload` as a fallback is deliberate. It means every row already in
storage gains a role without a re-backfill, which matters when the store holds thousands
of filings and re-fetching them would cost a day and a vendor quota.

Roles are tidied, never invented. An empty or placeholder value returns None, and None
renders as absent rather than as "unknown", for the same reason every other unknown in
this codebase does: a label we made up is worse than a gap the reader can see.
"""

from __future__ import annotations

from whale_agent.models.event import NormalizedEvent

__all__ = ["role_for", "tidy_role"]

_PLACEHOLDERS = {"", "n/a", "na", "none", "null", "unknown", "-"}

# Vendor spellings that are relationships rather than job titles, and how to say them.
_RELATIONSHIP_WORDS = {
    "director": "Director",
    "officer": None,  # a bare "officer" adds nothing the title does not already say
    "10 percent owner": "10% owner",
    "ten percent owner": "10% owner",
    "other": None,
}


def _title_case(text: str) -> str:
    """Fix shouted titles without touching ones that are deliberately cased.

    "CHIEF FINANCIAL OFFICER" should read as a title. "Chairman, President & CEO" is
    already right, and CEO must not become Ceo, so anything containing a lowercase letter
    is left exactly as filed.
    """
    if any(ch.islower() for ch in text):
        return text
    return " ".join(word.capitalize() if len(word) > 3 else word for word in text.split())


def tidy_role(raw: str | None) -> str | None:
    """Turn a vendor relationship string into something a person would say.

    Handles the two shapes FMP emits: a bare relationship list ("director, 10 percent
    owner") and a relationship list with a title after a colon ("director, officer: Chief
    Executive Officer"). The trailing-colon-with-no-title case is real and appears on
    hundreds of rows, so it must not leave dangling punctuation.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in _PLACEHOLDERS:
        return None

    relationships, _, title = text.partition(":")
    parts: list[str] = []
    for word in relationships.split(","):
        key = word.strip().lower()
        if not key:
            continue
        mapped = _RELATIONSHIP_WORDS.get(key, word.strip())
        if mapped:
            parts.append(mapped)

    title = title.strip()
    if title and title.lower() not in _PLACEHOLDERS:
        parts.append(_title_case(title))

    return ", ".join(parts) or None


def _from_form4_flags(payload: dict) -> str | None:
    """SEC's Form 4 relationship block, which arrives as booleans plus a title."""
    parts: list[str] = []
    if payload.get("is_director"):
        parts.append("Director")
    title = str(payload.get("officer_title") or "").strip()
    if payload.get("is_officer") and title or title:
        parts.append(_title_case(title))
    if payload.get("is_ten_percent_owner"):
        parts.append("10% owner")
    return ", ".join(parts) or None


def role_for(event: NormalizedEvent) -> str | None:
    """The filer's relationship to the issuer, or None when we never learned it.

    Order: an explicit field first, since an adapter that resolved the role properly
    beats re-deriving it, then the stored vendor payload so existing rows are covered.
    """
    explicit = getattr(event, "filer_role", None)
    if explicit:
        return tidy_role(explicit)

    payload = event.raw_payload or {}
    if not isinstance(payload, dict):
        return None

    vendor = payload.get("typeOfOwner") or payload.get("type_of_owner")
    if vendor:
        return tidy_role(vendor)

    return _from_form4_flags(payload)
