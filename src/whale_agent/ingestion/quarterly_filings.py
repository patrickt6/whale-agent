"""The 13F-filed-this-week headline: which tracked managers filed a quarterly
information table inside the report window, and what changed since their prior
quarter on file.

`thirteenf_holdings` already accrues one row per (manager, report period, position)
every time a 13F is parsed -- see `storage/db.py` and `position_history.py`. This
module is the missing piece between that data and a weekly headline: it asks
`Store.filings_between` which managers filed inside the window (by `filed_date`, the
day EDGAR received it -- not `report_period`, which lags by six to eight weeks), then
reuses `fund_watchlist.diff_holdings` to compute what moved against the immediately
prior period on file, if there is one.

The one rule this module exists to enforce: a manager with only one 13F on file is
NEVER described as having filed a "new" portfolio. Our stored history can simply be
one population pass old -- that says nothing about whether the manager filed a
quarter earlier. `FilingChange.has_prior` is False and `.changes` is None in that
case, and every caller (prose, ranking) must branch on `has_prior` rather than assume
"no prior row" means "no prior position".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from whale_agent.ingestion.fund_watchlist import Holding, PositionChanges, diff_holdings
from whale_agent.storage.db import Store

__all__ = [
    "FilingChange",
    "recent_quarterly_filings",
    "significance_pct",
    "MATERIAL_OPTION_SHARE_PCT",
    "OPTION_SHARE_SHIFT_THRESHOLD_PTS",
]

# A 13F reports a long option position at the NOTIONAL value of the underlying, not
# premium paid or capital at risk (see commit 4c4a4af). Comparing a stock-only total
# across two quarters where the options share of the disclosed book moved materially
# is the same defect one level up: it silently treats a mix change as a size change.
# `MATERIAL_OPTION_SHARE_PCT` is the floor below which an options share in BOTH
# quarters is small enough that a shift, even a large relative one, is noise (a
# manager whose book is 0.1% -> 2% options did not meaningfully change composition).
# `OPTION_SHARE_SHIFT_THRESHOLD_PTS` is the minimum absolute swing in percentage
# points, once that floor is cleared, that counts as "shifted" rather than normal
# quarter-to-quarter drift. Both were picked to catch the real case this module was
# built to fix (Situational Awareness LP: ~72% option share -> ~0.3%, a 71-point
# swing) while not firing on an ordinary book that carries a small, stable options
# sleeve every quarter.
MATERIAL_OPTION_SHARE_PCT = 5.0
OPTION_SHARE_SHIFT_THRESHOLD_PTS = 15.0


@dataclass(frozen=True)
class FilingChange:
    """One tracked manager's 13F filed inside the report window, with (when
    available) what changed against its prior period on file."""

    manager_cik: str
    manager_name: str

    report_period: date
    accession: str
    source_url: str
    filed_date: date

    current_holdings: list[Holding]
    current_total_value_usd: int
    current_position_count: int

    has_prior: bool
    prior_report_period: date | None = None
    prior_accession: str = ""
    prior_source_url: str = ""
    prior_total_value_usd: int | None = None
    prior_position_count: int | None = None

    changes: PositionChanges | None = None

    total_value_delta_usd: int | None = None
    total_value_delta_pct: float | None = None
    position_count_delta: int | None = None

    # Option notional held alongside the stock, per quarter. Carried separately
    # because the stock-only totals above are the honest basis for "what does this
    # manager own", while a book that moves between options and stock makes a
    # stock-to-stock comparison say something it does not mean. Situational
    # Awareness LP filed 2026-08-14 holding $3.9B of stock against $9.8B of option
    # notional a quarter earlier, then $20.2B of stock against almost none: a
    # straight read of the stock line calls that 423% growth when the disclosed
    # book went from $13.7B to $20.2B.
    current_option_notional_usd: int = 0
    prior_option_notional_usd: int | None = None

    @property
    def current_disclosed_total_usd(self) -> int:
        return self.current_total_value_usd + self.current_option_notional_usd

    @property
    def prior_disclosed_total_usd(self) -> int | None:
        if self.prior_total_value_usd is None:
            return None
        return self.prior_total_value_usd + (self.prior_option_notional_usd or 0)

    @property
    def prior_option_share_pct(self) -> float:
        prior_disclosed = self.prior_disclosed_total_usd
        if not prior_disclosed:
            return 0.0
        return 100.0 * (self.prior_option_notional_usd or 0) / prior_disclosed

    @property
    def current_option_share_pct(self) -> float:
        if not self.current_disclosed_total_usd:
            return 0.0
        return 100.0 * self.current_option_notional_usd / self.current_disclosed_total_usd

    @property
    def composition_shifted(self) -> bool:
        """True when the option share of the disclosed book moved enough between the
        two quarters that a stock-to-stock percentage misdescribes what happened.

        Two conditions, both required: the options sleeve has to matter in at least one
        of the quarters (`MATERIAL_OPTION_SHARE_PCT`), and the swing between them has to
        be larger than ordinary drift (`OPTION_SHARE_SHIFT_THRESHOLD_PTS`). Judgement,
        not a standard; see the note on those constants for why each was set where it is.
        """
        prior_disclosed = self.prior_disclosed_total_usd
        if not prior_disclosed or not self.current_disclosed_total_usd:
            return False
        prior_share = self.prior_option_share_pct
        current_share = self.current_option_share_pct
        if max(prior_share, current_share) < MATERIAL_OPTION_SHARE_PCT:
            return False
        return abs(current_share - prior_share) >= OPTION_SHARE_SHIFT_THRESHOLD_PTS


def _holding_from_row(row: dict) -> Holding:
    return Holding(
        issuer=row["issuer"],
        value_usd=row["value_usd"],
        shares=row["shares"],
        cusip=row["cusip"] or "",
        option_type=row["option_type"] or "",
    )


def significance_pct(fc: FilingChange) -> float:
    """The ranking basis: the largest single position move (opened, closed,
    increased, or decreased -- by absolute dollar value) as a percentage of this
    manager's own current 13F portfolio value.

    Deliberately not raw dollar size and not AUM: a $2B move by a $3B book is the
    story; the same $2B move buried in a $500B book is noise, and pool_top_trades
    elsewhere in this codebase already ranks single trades the same way (size against
    the manager's own disclosed portfolio, not the dollar figure alone). A filing with
    no prior period to diff against has no move to size, so it sorts last -- see
    `recent_quarterly_filings`, which puts every `has_prior=False` filing after every
    `has_prior=True` one regardless of this score.
    """
    if not fc.has_prior or fc.changes is None or not fc.current_disclosed_total_usd:
        return 0.0
    if fc.composition_shifted:
        # A book rotating between options and stock makes `diff_holdings` report the
        # stock leg as newly opened, so the largest single move is manufactured by the
        # rotation and ranking on it is circular: the artifact would top the email on
        # the strength of being an artifact. What survives is the change in the whole
        # disclosed book, which does not care which column the exposure sits in.
        prior_disclosed = fc.prior_disclosed_total_usd or 0
        if not prior_disclosed:
            return 0.0
        return abs(
            100.0 * (fc.current_disclosed_total_usd - prior_disclosed) / prior_disclosed
        )
    candidates = (
        [h.value_usd for h in fc.changes.opened]
        + [h.value_usd for h in fc.changes.closed]
        + [abs(delta) for _, delta in fc.changes.increased]
        + [abs(delta) for _, delta in fc.changes.decreased]
    )
    if not candidates:
        return 0.0
    return 100.0 * max(candidates) / fc.current_disclosed_total_usd


def _build_one(store: Store, manager_cik: str, manager_name: str, row: dict) -> FilingChange:
    report_period = date.fromisoformat(row["report_period"])
    accession = row["accession"]
    source_url = row["source_url"]
    filed_date = date.fromisoformat(row["filed_date"])

    current_rows = store.holdings_for_period(manager_cik, report_period)
    current_holdings = [_holding_from_row(r) for r in current_rows]
    current_total = sum(h.value_usd for h in current_holdings if not h.is_option)
    current_count = sum(1 for h in current_holdings if not h.is_option)
    current_option_notional = sum(h.value_usd for h in current_holdings if h.is_option)

    # The period immediately before this one on file, strictly earlier than the
    # report period just filed -- never derived from `filed_date`, which can jump
    # around (a late amendment, a manager catching up two quarters at once).
    earlier = store.report_periods_on_file(manager_cik, before=report_period)

    if not earlier:
        return FilingChange(
            manager_cik=manager_cik,
            manager_name=manager_name,
            report_period=report_period,
            accession=accession,
            source_url=source_url,
            filed_date=filed_date,
            current_holdings=current_holdings,
            current_total_value_usd=current_total,
            current_position_count=current_count,
            current_option_notional_usd=current_option_notional,
            has_prior=False,
        )

    prior_period = earlier[0]
    prior_rows = store.holdings_for_period(manager_cik, prior_period)
    prior_holdings = [_holding_from_row(r) for r in prior_rows]
    prior_total = sum(h.value_usd for h in prior_holdings if not h.is_option)
    prior_count = sum(1 for h in prior_holdings if not h.is_option)
    prior_option_notional = sum(h.value_usd for h in prior_holdings if h.is_option)
    prior_accession = prior_rows[0]["accession"] if prior_rows else ""
    prior_source_url = prior_rows[0]["source_url"] if prior_rows else ""

    changes = diff_holdings(current_holdings, prior_holdings)
    delta_value = current_total - prior_total
    delta_pct = (100.0 * delta_value / prior_total) if prior_total else None

    return FilingChange(
        manager_cik=manager_cik,
        manager_name=manager_name,
        report_period=report_period,
        accession=accession,
        source_url=source_url,
        filed_date=filed_date,
        current_holdings=current_holdings,
        current_total_value_usd=current_total,
        current_position_count=current_count,
        has_prior=True,
        prior_report_period=prior_period,
        prior_accession=prior_accession,
        prior_source_url=prior_source_url,
        prior_total_value_usd=prior_total,
        prior_position_count=prior_count,
        changes=changes,
        total_value_delta_usd=delta_value,
        total_value_delta_pct=delta_pct,
        position_count_delta=current_count - prior_count,
        current_option_notional_usd=current_option_notional,
        prior_option_notional_usd=prior_option_notional,
    )


def recent_quarterly_filings(
    store: Store,
    start: date,
    end: date,
    watchlist: dict[str, str] | None = None,
) -> list[FilingChange]:
    """Every tracked manager whose 13F was FILED in [start, end], ranked by
    `significance_pct` (largest single move against the manager's own book first),
    with every `has_prior=False` filing sorted after every `has_prior=True` one --
    "no prior period on file" is not a zero-sized move, it is an unanswerable
    question, and burying it at the bottom by a fabricated score would misstate that.

    `watchlist` maps manager name -> CIK; defaults to `fund_watchlist.WATCHLIST`. Only
    CIKs present in it are resolved to a name -- a manager_cik in storage that has
    since dropped off the watchlist is skipped rather than shown with a blank name.
    """
    if watchlist is None:
        from whale_agent.ingestion.fund_watchlist import WATCHLIST

        watchlist = WATCHLIST
    cik_to_name = {cik: name for name, cik in watchlist.items()}

    rows = store.filings_between(start, end)
    out: list[FilingChange] = []
    for row in rows:
        cik = row["manager_cik"]
        name = cik_to_name.get(cik)
        if name is None:
            continue
        out.append(_build_one(store, cik, name, row))

    out.sort(key=lambda fc: (not fc.has_prior, -significance_pct(fc)))
    return out
