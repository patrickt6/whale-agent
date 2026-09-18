"""History context per ranked row: what we already hold on record for this filer and issuer.

The reader asked, for each filing: was there a prior position, when, and how large. This
module answers that from the store only. It makes no claim about performance or edge.
Every figure it returns is a stored field (an earlier event's share count or USD value,
or a 13F row's shares or value), so the provenance gate can admit it by name.

Storage note: `Store` is SQLite only (storage/db.py). `Settings.database_url` exists in
config.py but no Postgres code path reads it, so the queries here are SQLite SQL against
`store.conn`.

Call this AFTER `build_ranked` persists the run. The queries exclude the row itself by
event_id and only read rows disclosed strictly before it, so persisting first does not
make an event its own history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from whale_agent.models.enums import TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.storage.db import Store, filer_key_for

REPEAT_WINDOW_DAYS = 90

_ACQUIRE = {
    TransactionType.OPEN_MARKET_BUY,
    TransactionType.ACTIVIST_13D,
    TransactionType.PASSIVE_13G,
    TransactionType.FUND_NEW_POSITION,
    TransactionType.FUND_ADD_POSITION,
}
_DISPOSE = {TransactionType.OPEN_MARKET_SELL, TransactionType.SCHEDULED_SALE}


def _direction(tt: TransactionType) -> set[str]:
    """Transaction type values that count as the same direction as `tt`."""
    for group in (_ACQUIRE, _DISPOSE):
        if tt in group:
            return {t.value for t in group}
    return {tt.value}


@dataclass(frozen=True)
class HistoryContext:
    event_id: str
    prior_date: date | None = None
    prior_shares: float | None = None
    prior_usd: float | None = None
    prior_source: str | None = None  # "13F" or "filing"
    repeat_count: int = 0
    first_seen: date | None = None  # set only when first seen inside the run window

    @property
    def has_prior(self) -> bool:
        return self.prior_date is not None


def _issuer_key(ev: NormalizedEvent) -> str:
    return (ev.issuer_id or ev.issuer_name).strip().lower()


def _prior_event(
    store: Store, ev: NormalizedEvent
) -> tuple[date, float | None, float | None] | None:
    fkey = filer_key_for(ev)
    ikey = _issuer_key(ev)
    rows = store.conn.execute(
        """
        SELECT disclosure_date,
               json_extract(payload, '$.share_count') AS shares,
               COALESCE(json_extract(payload, '$.usd_value'),
                        json_extract(payload, '$.implied_usd_value')) AS usd
        FROM events
        WHERE amended = 0 AND event_id != ? AND disclosure_date < ?
          AND lower(trim(COALESCE(json_extract(payload, '$.filer_id'),
                                  json_extract(payload, '$.filer_name')))) = ?
          AND lower(trim(COALESCE(json_extract(payload, '$.issuer_id'),
                                  json_extract(payload, '$.issuer_name')))) = ?
        ORDER BY disclosure_date DESC LIMIT 1
        """,
        (ev.event_id, ev.disclosure_date.isoformat(), fkey, ikey),
    ).fetchone()
    if rows is None:
        return None
    return date.fromisoformat(rows["disclosure_date"]), rows["shares"], rows["usd"]


def _prior_13f(
    store: Store, ev: NormalizedEvent
) -> tuple[date, float | None, float | None] | None:
    """Latest 13F row for this manager and issuer, reported before the event.

    Matched on manager_cik == filer_id and on CUSIP == issuer_id or the issuer name
    (case-insensitive). SEC events carry the issuer CIK, not the CUSIP, so in practice
    the name match does the work; a name spelled differently in the 13F is missed.
    """
    if not ev.filer_id:
        return None
    row = store.conn.execute(
        """
        SELECT report_period, shares, value_usd FROM thirteenf_holdings
        WHERE manager_cik = ? AND report_period < ? AND option_type = ''
          AND (upper(issuer) = upper(?) OR (cusip != '' AND cusip = ?))
        ORDER BY report_period DESC LIMIT 1
        """,
        (
            ev.filer_id,
            ev.disclosure_date.isoformat(),
            ev.issuer_name.strip(),
            ev.issuer_id or "",
        ),
    ).fetchone()
    if row is None:
        return None
    return date.fromisoformat(row["report_period"]), row["shares"], row["value_usd"]


def _repeat_count(store: Store, ev: NormalizedEvent) -> int:
    """Same-direction filings on the same issuer in the 90 days BEFORE the event date.

    Strictly earlier dates only: one Form 4 with several transaction lines, or the same
    filing from two sources, lands on the same date and must not read as a streak.
    """
    types = sorted(_direction(ev.transaction_type))
    start = ev.disclosure_date - timedelta(days=REPEAT_WINDOW_DAYS)
    marks = ",".join("?" for _ in types)
    row = store.conn.execute(
        f"""
        SELECT COUNT(*) FROM events
        WHERE amended = 0 AND event_id != ?
          AND disclosure_date >= ? AND disclosure_date < ?
          AND json_extract(payload, '$.transaction_type') IN ({marks})
          AND lower(trim(COALESCE(json_extract(payload, '$.filer_id'),
                                  json_extract(payload, '$.filer_name')))) = ?
          AND lower(trim(COALESCE(json_extract(payload, '$.issuer_id'),
                                  json_extract(payload, '$.issuer_name')))) = ?
        """,
        (
            ev.event_id,
            start.isoformat(),
            ev.disclosure_date.isoformat(),
            *types,
            filer_key_for(ev),
            _issuer_key(ev),
        ),
    ).fetchone()
    return int(row[0])


def build_history_context(
    store: Store,
    ev: NormalizedEvent,
    on: date,
    window_days: int = 1,
) -> HistoryContext:
    tagged = [
        (c, label)
        for c, label in ((_prior_event(store, ev), "filing"), (_prior_13f(store, ev), "13F"))
        if c is not None
    ]
    prior, source = max(tagged, key=lambda t: t[0][0]) if tagged else (None, None)

    first_seen = None
    row = store.conn.execute(
        "SELECT first_seen FROM seen_filers WHERE filer_key = ?", (filer_key_for(ev),)
    ).fetchone()
    if row is not None:
        seen = date.fromisoformat(row["first_seen"])
        if on - timedelta(days=window_days) <= seen <= on:
            first_seen = seen

    return HistoryContext(
        event_id=ev.event_id,
        prior_date=prior[0] if prior else None,
        prior_shares=prior[1] if prior else None,
        prior_usd=prior[2] if prior else None,
        prior_source=source,
        repeat_count=_repeat_count(store, ev),
        first_seen=first_seen,
    )


def build_history_contexts(
    store: Store, events: Iterable[NormalizedEvent], on: date, window_days: int = 1
) -> dict[str, HistoryContext]:
    return {ev.event_id: build_history_context(store, ev, on, window_days) for ev in events}


# -- rendering and provenance ---------------------------------------------------------


def _shares(value: float) -> str:
    return f"{value:,.0f}"


def history_line(ctx: HistoryContext) -> str:
    """One short line, STE100 style. No performance or edge claims."""
    from whale_agent.summarization.render import format_usd

    parts: list[str] = []
    if ctx.has_prior:
        # A 13F row is a holding. An earlier event may be a sale, so it is not "held".
        lead = "Held before" if ctx.prior_source == "13F" else "Earlier filing"
        tail = " (13F)" if ctx.prior_source == "13F" else ""
        when = ctx.prior_date.isoformat() if ctx.prior_date is not None else "an earlier date"
        if ctx.prior_shares is not None:
            parts.append(f"{lead}: {_shares(ctx.prior_shares)} shares on {when}{tail}.")
        elif ctx.prior_usd is not None:
            parts.append(f"{lead}: {format_usd(ctx.prior_usd)} on {when}{tail}.")
        else:
            parts.append(f"{lead}: {when}{tail}.")
    else:
        parts.append("First time this filer appears for this issuer in our records.")
    if ctx.repeat_count:
        parts.append(
            f"Same-direction filings on this issuer in the last {REPEAT_WINDOW_DAYS} days: "
            f"{ctx.repeat_count}."
        )
    if ctx.first_seen is not None:
        parts.append(f"New filer, first seen {ctx.first_seen.isoformat()}.")
    return " ".join(parts)


def history_figures(history: Mapping[str, HistoryContext] | None) -> list[str]:
    """The strings a history line can print, for the provenance allowed set."""
    from whale_agent.summarization.render import format_usd

    out: list[str] = []
    for ctx in (history or {}).values():
        if ctx.prior_shares is not None:
            out.append(_shares(ctx.prior_shares))
        if ctx.prior_usd is not None:
            out.append(format_usd(ctx.prior_usd))
        if ctx.prior_date is not None:
            out.append(ctx.prior_date.isoformat())
        if ctx.first_seen is not None:
            out.append(ctx.first_seen.isoformat())
        out.append(str(ctx.repeat_count))
    return out


__all__ = [
    "HistoryContext",
    "build_history_context",
    "build_history_contexts",
    "history_figures",
    "history_line",
]
