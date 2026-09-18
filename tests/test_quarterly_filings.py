"""The 13F-filed-this-week headline: turning `thirteenf_holdings` rows already sitting
in storage into a ranked, provenanced list of what each tracked manager just filed.

No network. Every test writes fixture Holdings straight into an in-memory Store, same
convention as test_position_history.py.
"""

from __future__ import annotations

from datetime import date

import pytest

from whale_agent.ingestion.fund_watchlist import Holding
from whale_agent.ingestion.quarterly_filings import recent_quarterly_filings
from whale_agent.storage.db import Store

CIK_A = "0001067983"
CIK_B = "0001086364"


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _seed_two_quarters(store, cik, *, prior_shift=0):
    prior = [
        Holding("NVIDIA CORPORATION", 1_000_000_000, 500_000, cusip="67066G104"),
        Holding("ORACLE CORP", 800_000_000, 200_000, cusip="68389X105"),
        Holding("PFIZER INC", 200_000_000, 100_000, cusip="717081103"),
    ]
    current = [
        Holding("NVIDIA CORPORATION", 1_600_000_000, 700_000, cusip="67066G104"),  # up
        Holding("ORACLE CORP", 300_000_000, 75_000, cusip="68389X105"),  # down
        Holding("BROADCOM INC", 900_000_000, 50_000, cusip="11135F101"),  # new
    ]
    store.record_13f_holdings(
        cik,
        date(2025, 12, 31),
        f"0001-25-00{prior_shift}111",
        "https://sec.gov/prior-index.htm",
        prior,
        filed_date=date(2026, 2, 10),
    )
    store.record_13f_holdings(
        cik,
        date(2026, 3, 31),
        f"0001-26-00{prior_shift}222",
        "https://sec.gov/current-index.htm",
        current,
        filed_date=date(2026, 8, 14),
    )


def test_finds_a_manager_who_filed_inside_the_window_with_a_prior_period_to_diff(store):
    _seed_two_quarters(store, CIK_A)
    out = recent_quarterly_filings(
        store, date(2026, 8, 10), date(2026, 8, 16), watchlist={"Berkshire Hathaway": CIK_A}
    )
    assert len(out) == 1
    fc = out[0]
    assert fc.manager_cik == CIK_A
    assert fc.manager_name == "Berkshire Hathaway"
    assert fc.report_period == date(2026, 3, 31)
    assert fc.filed_date == date(2026, 8, 14)
    assert fc.accession == "0001-26-000222"
    assert fc.has_prior is True
    assert fc.prior_report_period == date(2025, 12, 31)
    assert fc.changes is not None
    assert fc.changes.opened[0].issuer == "BROADCOM INC"


def test_filed_date_outside_window_is_excluded(store):
    _seed_two_quarters(store, CIK_A)
    out = recent_quarterly_filings(
        store, date(2026, 1, 1), date(2026, 1, 7), watchlist={"Berkshire Hathaway": CIK_A}
    )
    assert out == []


def test_no_prior_period_is_reported_as_no_prior_not_as_a_new_portfolio(store):
    """Non-negotiable: a manager with only one 13F on file must never be described as
    having filed a wholly "new" portfolio -- our history may just be one population
    pass old. `has_prior` must be False and `changes` must be None so no caller can
    accidentally label every position "new"."""
    holdings = [Holding("NVIDIA CORPORATION", 1_600_000_000, 700_000, cusip="67066G104")]
    store.record_13f_holdings(
        CIK_A,
        date(2026, 3, 31),
        "0001-26-000222",
        "https://sec.gov/current-index.htm",
        holdings,
        filed_date=date(2026, 8, 14),
    )
    out = recent_quarterly_filings(
        store, date(2026, 8, 10), date(2026, 8, 16), watchlist={"Berkshire Hathaway": CIK_A}
    )
    assert len(out) == 1
    fc = out[0]
    assert fc.has_prior is False
    assert fc.changes is None
    assert fc.prior_report_period is None


def test_ranked_by_largest_single_move_against_the_manager_own_book(store):
    """Ranking basis: the largest single position change (opened/closed/increased/
    decreased, by dollar value) as a percentage of the manager's own current
    portfolio value -- the same "size against its own book" convention `pool_top_trades`
    already uses, not raw AUM and not alphabetical."""
    _seed_two_quarters(store, CIK_A, prior_shift=0)
    # Manager B: a much bigger book, but its only move is proportionally tiny.
    prior_b = [Holding("APPLE INC", 50_000_000_000, 100_000_000, cusip="037833100")]
    current_b = [
        Holding("APPLE INC", 50_000_000_000, 100_000_000, cusip="037833100"),
        Holding("PFIZER INC", 10_000_000, 5_000, cusip="717081103"),  # tiny new position
    ]
    store.record_13f_holdings(
        CIK_B,
        date(2025, 12, 31),
        "0002-25-000111",
        "https://sec.gov/prior-index-b.htm",
        prior_b,
        filed_date=date(2026, 2, 10),
    )
    store.record_13f_holdings(
        CIK_B,
        date(2026, 3, 31),
        "0002-26-000222",
        "https://sec.gov/current-index-b.htm",
        current_b,
        filed_date=date(2026, 8, 13),
    )
    out = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK_A, "BlackRock": CIK_B},
    )
    assert [fc.manager_cik for fc in out] == [CIK_A, CIK_B]


def test_managers_with_no_prior_period_are_still_included_ranked_after_diffable_ones(store):
    holdings = [Holding("NVIDIA CORPORATION", 1_600_000_000, 700_000, cusip="67066G104")]
    store.record_13f_holdings(
        CIK_B,
        date(2026, 3, 31),
        "0002-26-000222",
        "https://sec.gov/current-index-b.htm",
        holdings,
        filed_date=date(2026, 8, 13),
    )
    _seed_two_quarters(store, CIK_A)
    out = recent_quarterly_filings(
        store,
        date(2026, 8, 10),
        date(2026, 8, 16),
        watchlist={"Berkshire Hathaway": CIK_A, "BlackRock": CIK_B},
    )
    assert [fc.manager_cik for fc in out] == [CIK_A, CIK_B]
