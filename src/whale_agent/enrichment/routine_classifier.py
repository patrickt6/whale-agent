"""Routine vs opportunistic classification (Cohen-Malloy-Pomorski 2012 heuristic).

If an insider trades the same calendar month every year across their history, the
trade is "routine" and heavily discounted; otherwise "opportunistic". Only
opportunistic trades historically carried signal (~82 bps/month), so scoring applies
a routine penalty.

`routine_label` and the attach helpers below produce the tri-state, display-facing
version of this split: "opportunistic" / "routine" / "unknown". They exist because the
weekly report's evidence block claims every row is classified on this basis, and until
these were wired in, nothing in the codebase actually called `classify_routine` --
the claim was false. Note what these helpers deliberately do *not* touch: `is_routine`
on `NormalizedEvent`, the field `scoring/score.py` reads for the routine penalty. That
stays whatever it already was. Wiring the classifier in for display must not change a
single ranking, so the label lives in its own field (`routine_label`) instead.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Literal

from whale_agent.models.event import NormalizedEvent
from whale_agent.storage.db import Store, filer_key_for

RoutineLabel = Literal["opportunistic", "routine", "unknown"]


def classify_routine(history: Iterable[date], *, min_years: int = 3) -> bool:
    """Return True if the filer's trade history looks calendar-routine.

    Heuristic: at least `min_years` distinct years, all trades in the same calendar
    month. This mirrors the CMP definition of a routine trader.
    """
    dates = list(history)
    years = {d.year for d in dates}
    if len(years) < min_years:
        return False
    months = {d.month for d in dates}
    return len(months) == 1


def build_filer_history(trade_dates_by_filer: dict[str, list[date]]) -> dict[str, bool]:
    """Convenience: classify every filer given a map of filer_id -> trade dates."""
    out: dict[str, bool] = {}
    grouped: dict[str, list[date]] = defaultdict(list)
    for filer_id, dates in trade_dates_by_filer.items():
        grouped[filer_id].extend(dates)
    for filer_id, dates in grouped.items():
        out[filer_id] = classify_routine(dates)
    return out


def routine_label(history: Iterable[date], *, min_years: int = 3) -> RoutineLabel:
    """Tri-state read of the same heuristic, for a displayed annotation rather than a
    scoring discount.

    `classify_routine` defaults to False whenever there is too little history to judge,
    which is the right behaviour for a discount that should never fire on a guess. It is
    the wrong behaviour for a label that claims to have decided something: a reader
    cannot tell "we checked and this filer trades opportunistically" apart from "we do
    not know yet". This returns "unknown" explicitly in that case instead, so a row
    never silently reads as routine or opportunistic on no evidence.
    """
    dates = list(history)
    if len({d.year for d in dates}) < min_years:
        return "unknown"
    return "routine" if len({d.month for d in dates}) == 1 else "opportunistic"


def label_for_event(
    store: Store,
    event: NormalizedEvent,
    as_of: date | None = None,
    *,
    min_years: int = 3,
) -> RoutineLabel:
    """Classify one event's filer from the store's own disclosure history.

    Mirrors `enrichment/filer_history.load_filer_history`'s use of
    `Store.filer_observations`, keeping only the dates: the CMP split is about calendar
    pattern, not position size, so the USD figures that module also carries are not
    needed here. `before=ref` matches the same rule the rest of enrichment follows --
    an event is judged against the history that preceded it, never against itself.
    """
    ref = as_of or event.disclosure_date
    key = filer_key_for(event)
    dates = [row["disclosure_date"] for row in store.filer_observations(key, before=ref)]
    return routine_label(dates, min_years=min_years)


def attach_routine_labels(
    store: Store,
    events: list[NormalizedEvent],
    as_of: date | None = None,
) -> list[NormalizedEvent]:
    """Populate `routine_label` on each event, in place, for display in the report.

    Callers wiring this in: do not also set `is_routine` from this result. That field
    feeds `scoring/score.py`'s ranking penalty, and changing what drives it is a product
    decision about ranking, not a side effect of making a display claim true.
    """
    for event in events:
        event.routine_label = label_for_event(store, event, as_of=as_of)
    return events
