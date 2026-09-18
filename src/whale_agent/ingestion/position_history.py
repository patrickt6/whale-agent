"""Answering "what did this manager hold before, and what changed" -- with provenance.

This is the data layer only. It does not write prose. It exists so that a prose layer
can ask one question -- `prior_position(store, cik, name, issuer, as_of)` -- and get
back every fact and every citation it needs to answer a reader-facing "was there a
prior position" question without doing arithmetic, without guessing, and without
silently treating "we have no record" as "they held none".

Persistence (`persist_snapshot_holdings`) is what makes the second run's answer better
than the first: a 13F information table read once is written to `thirteenf_holdings`
(see `storage/db.py`) and is there for every later run to diff against, so a manager's
history accrues instead of being re-derived from a live fetch every week.

Three states this module is careful never to collapse into one another:
  * "no 13F filing on file at all for this manager" (`no_history_on_file`) -- the very
    first run, before any history has accrued.
  * "one 13F on file, nothing earlier to diff against" (`no_prior_quarter_on_file`) --
    a real filing exists, comparison is simply not possible yet.
  * "two or more 13Fs on file, and this specific issuer appears in neither"
    (`no_record`) -- distinct from "held none", because 13F covers only 13F-reportable
    securities and absence never proves non-ownership.

A Form 4 (an individual insider's trade) and a 13F (an institution's quarterly
position) are different filings about different legal persons. This module answers
only the 13F question for the CIK it is given; it does not attempt to resolve a Form 4
filer to an affiliated institution's 13F. A caller holding a Form 4 filer's own CIK
will correctly get back `no_history_on_file` if that CIK never files a 13F -- which
*is* the honest "no comparable prior 13F position on file" answer, not a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from whale_agent.ingestion.fund_watchlist import Holding
from whale_agent.storage.db import Store


@dataclass(frozen=True)
class PriorPositionFact:
    """Everything a prose layer needs to state whether there was a prior position,
    with nowhere left for it to do arithmetic or invent a number.

    `status` is one of:
      "no_history_on_file"      -- no 13F for this manager has ever been recorded.
      "no_prior_quarter_on_file"-- one 13F on file; nothing earlier to compare to.
      "no_record"               -- two+ 13Fs on file; this issuer is in neither.
      "new"                     -- absent from the prior quarter, present now.
      "closed"                  -- present in the prior quarter, absent now.
      "increased" / "decreased" -- present in both, value moved.
      "unchanged"               -- present in both, value identical.

    `*_value_usd` on an option row (`is_option` True) is the 13F's own NOTIONAL value
    of the underlying, not premium paid or capital at risk -- copied as the filing
    reports it, per `Holding.value_usd`'s own convention in fund_watchlist.py.
    """

    manager_cik: str
    manager_name: str
    issuer: str
    status: str
    cusip: str = ""

    as_of_report_period: date | None = None
    prior_report_period: date | None = None

    current_shares: int | None = None
    current_value_usd: int | None = None
    prior_shares: int | None = None
    prior_value_usd: int | None = None

    delta_shares: int | None = None
    delta_value_usd: int | None = None
    delta_pct: float | None = None

    is_option: bool = False
    option_type: str = ""

    # Provenance for the current-quarter figure.
    source_accession: str = ""
    source_url: str = ""
    # Provenance for the prior-quarter figure, when there is one.
    prior_source_accession: str = ""
    prior_source_url: str = ""

    note: str = ""


def _row_to_holding(row: dict) -> Holding:
    return Holding(
        issuer=row["issuer"],
        value_usd=row["value_usd"],
        shares=row["shares"],
        cusip=row["cusip"] or "",
        option_type=row["option_type"] or "",
    )


def _find(rows: list[dict], issuer_or_cusip: str) -> dict | None:
    """Match by CUSIP first -- the stable identity, see `Holding.key` -- then fall
    back to a case-insensitive exact match on the issuer name a filer reported."""
    needle = issuer_or_cusip.strip().upper()
    for row in rows:
        if row["cusip"] and row["cusip"].upper() == needle:
            return row
    for row in rows:
        if row["issuer"].strip().upper() == needle:
            return row
    return None


def prior_position(
    store: Store,
    manager_cik: str,
    manager_name: str,
    issuer_or_cusip: str,
    as_of: date,
) -> PriorPositionFact:
    """Did `manager_cik` hold `issuer_or_cusip` before, and what changed.

    `as_of` bounds which report periods are considered -- only periods filed/reported
    on file strictly before it are used, so a caller asking "as of last week" never
    gets handed a period from after that date.
    """
    periods = store.report_periods_on_file(manager_cik, before=None)
    periods = [p for p in periods if p < as_of] if as_of else periods
    # report_periods_on_file already returns newest-first; re-filtering preserves order.

    base = dict(manager_cik=manager_cik, manager_name=manager_name)

    if not periods:
        return PriorPositionFact(
            **base,
            issuer=issuer_or_cusip,
            status="no_history_on_file",
            note=(
                "No 13F filing on file for this manager. This is the honest answer on "
                "a first run; history accrues from here."
            ),
        )

    current_period = periods[0]
    current_rows = store.holdings_for_period(manager_cik, current_period)
    current_row = _find(current_rows, issuer_or_cusip)

    if len(periods) < 2:
        if current_row is None:
            return PriorPositionFact(
                **base,
                issuer=issuer_or_cusip,
                status="no_record",
                as_of_report_period=current_period,
                note=(
                    "One 13F is on file for this manager and this issuer does not "
                    "appear on it. That is not the same as holding none: 13F covers "
                    "only 13F-reportable securities, and there is no earlier filing "
                    "to compare against yet."
                ),
            )
        h = _row_to_holding(current_row)
        return PriorPositionFact(
            **base,
            issuer=h.issuer,
            cusip=h.cusip,
            status="no_prior_quarter_on_file",
            as_of_report_period=current_period,
            current_shares=h.shares,
            current_value_usd=h.value_usd,
            is_option=h.is_option,
            option_type=h.option_type,
            source_accession=current_row["accession"],
            source_url=current_row["source_url"],
            note=(
                "This is the only 13F on file for this manager, so there is nothing "
                "earlier to compare it to yet. That will change once a second quarter "
                "has been recorded."
            ),
        )

    prior_period = periods[1]
    prior_rows = store.holdings_for_period(manager_cik, prior_period)
    prior_row = _find(prior_rows, issuer_or_cusip)

    if current_row is None and prior_row is None:
        return PriorPositionFact(
            **base,
            issuer=issuer_or_cusip,
            status="no_record",
            as_of_report_period=current_period,
            prior_report_period=prior_period,
            note=(
                "No record of this issuer in either 13F on file for this manager. "
                "13F covers only 13F-reportable securities, so this does not prove "
                "the position was never held -- only that neither filing reports it."
            ),
        )

    if current_row is None:
        h = _row_to_holding(prior_row)
        return PriorPositionFact(
            **base,
            issuer=h.issuer,
            cusip=h.cusip,
            status="closed",
            as_of_report_period=current_period,
            prior_report_period=prior_period,
            prior_shares=h.shares,
            prior_value_usd=h.value_usd,
            is_option=h.is_option,
            option_type=h.option_type,
            prior_source_accession=prior_row["accession"],
            prior_source_url=prior_row["source_url"],
            note="Held last quarter, absent from the current 13F: reported as closed.",
        )

    if prior_row is None:
        h = _row_to_holding(current_row)
        return PriorPositionFact(
            **base,
            issuer=h.issuer,
            cusip=h.cusip,
            status="new",
            as_of_report_period=current_period,
            prior_report_period=prior_period,
            current_shares=h.shares,
            current_value_usd=h.value_usd,
            is_option=h.is_option,
            option_type=h.option_type,
            source_accession=current_row["accession"],
            source_url=current_row["source_url"],
            note="No position last quarter, present now: reported as new.",
        )

    cur = _row_to_holding(current_row)
    pri = _row_to_holding(prior_row)
    delta_value = cur.value_usd - pri.value_usd
    delta_shares = (
        None if cur.shares is None or pri.shares is None else cur.shares - pri.shares
    )
    delta_pct = (100.0 * delta_value / pri.value_usd) if pri.value_usd else None
    status = (
        "increased" if delta_value > 0 else "decreased" if delta_value < 0 else "unchanged"
    )
    note = {
        "increased": "Position grew quarter over quarter, by reported market value.",
        "decreased": "Position shrank quarter over quarter, by reported market value.",
        "unchanged": "Reported market value is unchanged from the prior quarter.",
    }[status]
    if cur.is_option:
        note += (
            " Figures are the 13F's notional value of the underlying, not premium "
            "paid or capital at risk."
        )
    return PriorPositionFact(
        **base,
        issuer=cur.issuer,
        cusip=cur.cusip,
        status=status,
        as_of_report_period=current_period,
        prior_report_period=prior_period,
        current_shares=cur.shares,
        current_value_usd=cur.value_usd,
        prior_shares=pri.shares,
        prior_value_usd=pri.value_usd,
        delta_shares=delta_shares,
        delta_value_usd=delta_value,
        delta_pct=delta_pct,
        is_option=cur.is_option,
        option_type=cur.option_type,
        source_accession=current_row["accession"],
        source_url=current_row["source_url"],
        prior_source_accession=prior_row["accession"],
        prior_source_url=prior_row["source_url"],
        note=note,
    )


def persist_snapshot_holdings(
    store: Store,
    *,
    manager_cik: str,
    current: tuple[date, str, str, date | None, list[Holding]],
    prior: tuple[date, str, str, date | None, list[Holding]] | None = None,
) -> None:
    """Write one manager's current (and, when available, prior) information table.

    `current` / `prior` are `(report_period, accession, source_url, filed_date,
    holdings)`. `prior` is optional: a manager fetched for the first time, or one
    whose prior quarter is already recorded from an earlier run, need not supply it.
    Idempotent -- see `Store.record_13f_holdings`.
    """
    period, accession, url, filed, holdings = current
    store.record_13f_holdings(manager_cik, period, accession, url, holdings, filed_date=filed)
    if prior is not None:
        p_period, p_accession, p_url, p_filed, p_holdings = prior
        store.record_13f_holdings(
            manager_cik, p_period, p_accession, p_url, p_holdings, filed_date=p_filed
        )
