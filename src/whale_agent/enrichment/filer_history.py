"""What we know about a filer from having watched them, not from having heard of them.

"Unfamiliar" and "unusual" are the two properties this product is actually looking for,
and neither can be looked up. A vendor can tell us Vanguard is large; nothing external
can tell us that this particular fund has filed nineteen times in our window and is now
doing something four times its own median size. That is a fact about our own dataset, so
it is computed here from our own dataset and from nothing else -- no external call.

Two measures come out of this module and both matter:

  * **Unfamiliarity** -- how many times we have seen this filer at all. A name we have
    never recorded is more interesting than one we log weekly, because the weekly one is
    a subscription and the new one is a decision.
  * **Unusual-for-this-filer** -- their first appearance in eighteen months, or a
    position well above their own median. This is the opportunistic/routine distinction
    from Cohen-Malloy-Pomorski (2012) measured per filer rather than assumed from form
    type: signal concentrated in the trades that broke the filer's own pattern.

The cold-start problem is the reason for `history_is_warm`. On a fresh database every
filer is unseen, so every event is "a debut", and the first digest confidently announces
that all thirty names are new. That claim is not false because the data is wrong; it is
false because the question was unanswerable. Until the store holds enough history for
absence to be evidence, these terms return None and scoring falls back to neutral.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median

from whale_agent.models.event import NormalizedEvent
from whale_agent.storage.db import Store, filer_key_for

__all__ = [
    "FilerHistory",
    "MIN_OBSERVATIONS_FOR_HISTORY",
    "MIN_SPAN_DAYS_FOR_HISTORY",
    "UNUSUAL_GAP_DAYS",
    "attach_filer_history",
    "filer_key_for",
    "history_is_warm",
    "load_filer_history",
    "mark_first_time",
]

# Warm-up thresholds. Both must hold before we will claim a filer is new.
#
# 200 observations: with a typical day producing tens of qualifying filings, this is a
# few days of ingestion -- enough that the frequent filers (the index managers, the
# serial 13F reporters) are all present, which is exactly the population an "unseen"
# claim is implicitly contrasted against.
#
# 45 days: the count alone can be met by one heavy backfill afternoon, and a filer who
# files quarterly is genuinely absent from any window shorter than a quarter. 45 days is
# the shortest window in which a quarterly filer's absence is even slightly informative;
# below it, "we have not seen them" mostly means "we have not been watching".
MIN_OBSERVATIONS_FOR_HISTORY = 200
MIN_SPAN_DAYS_FOR_HISTORY = 45

# 18 months, per the brief: a filer resurfacing after this long is making a decision, not
# maintaining a position.
UNUSUAL_GAP_DAYS = 548


@dataclass(frozen=True)
class FilerHistory:
    """Everything our own records say about one filer, as of a reference date."""

    filer_key: str
    prior_filings: int
    last_seen: date | None
    days_since_last: int | None
    typical_usd: float | None  # median of their past disclosed positions

    @property
    def is_unseen(self) -> bool:
        """True if we hold no prior record of this filer."""
        return self.prior_filings == 0

    def is_returning_after_gap(self, gap_days: int = UNUSUAL_GAP_DAYS) -> bool:
        """True if they were absent long enough that reappearing is itself the news."""
        return self.days_since_last is not None and self.days_since_last >= gap_days

    def size_vs_typical(self, usd_value: float | None) -> float | None:
        """This position as a multiple of their own median. None when unmeasurable."""
        if usd_value is None or not self.typical_usd or self.typical_usd <= 0:
            return None
        return usd_value / self.typical_usd


def history_is_warm(store: Store) -> bool:
    """True once absence of a filer from the store is evidence rather than ignorance."""
    return (
        store.filer_observation_count() >= MIN_OBSERVATIONS_FOR_HISTORY
        and store.filer_history_span_days() >= MIN_SPAN_DAYS_FOR_HISTORY
    )


def load_filer_history(
    store: Store,
    event: NormalizedEvent,
    as_of: date | None = None,
) -> FilerHistory:
    """Summarise one filer's record, counting only what preceded this event.

    The `before` cutoff is load-bearing: an event re-scored after it has been stored
    would otherwise find itself in its own history and stop being a debut.
    """
    key = filer_key_for(event)
    ref = as_of or event.disclosure_date
    rows = store.filer_observations(key, before=ref)
    if not rows:
        return FilerHistory(
            filer_key=key,
            prior_filings=0,
            last_seen=None,
            days_since_last=None,
            typical_usd=None,
        )
    last_seen = rows[-1]["disclosure_date"]
    values = [r["usd_value"] for r in rows if r["usd_value"] is not None]
    return FilerHistory(
        filer_key=key,
        prior_filings=len(rows),
        last_seen=last_seen,
        days_since_last=max(0, (ref - last_seen).days),
        typical_usd=median(values) if values else None,
    )


def mark_first_time(
    event: NormalizedEvent,
    seen_filer_ids: set[str],
    *,
    history_is_warm: bool = True,
) -> NormalizedEvent:
    """Set `is_first_time_filer`, but only when the store can support the claim.

    This is the cold-start fix. Set-membership against an empty store says every filer is
    a debut, which inflated every score by the first-time bonus and made the opening
    digest read as though the entire market had just been founded. With `history_is_warm`
    False the flag stays off: an unverifiable claim is worse than a missing one, and the
    measured terms still rank the events perfectly well without it.
    """
    key = filer_key_for(event)
    event.is_first_time_filer = history_is_warm and key not in seen_filer_ids
    return event


def attach_filer_history(
    store: Store,
    events: list[NormalizedEvent],
    as_of: date | None = None,
) -> list[NormalizedEvent]:
    """Populate the `filer_*` context fields on each event, in place.

    Leaves them None on a cold store rather than writing zeros: scoring reads None as
    "not learned" and stays neutral, whereas a zero would read as "never seen before" and
    promote everything.
    """
    warm = history_is_warm(store)
    for event in events:
        if not warm:
            continue
        hist = load_filer_history(store, event, as_of)
        event.context.filer_prior_filings = hist.prior_filings
        event.context.filer_last_seen = hist.last_seen
        event.context.filer_days_since_last = hist.days_since_last
        event.context.filer_typical_usd = hist.typical_usd
    return events
