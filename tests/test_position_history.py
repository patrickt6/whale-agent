"""Prior-position history: persisting 13F holdings and answering "what did they hold
before" with provenance, without the caller doing arithmetic (GAMEPLAN: prior position).

No network. Every test writes fixture Holdings straight into an in-memory Store.
"""

from __future__ import annotations

from datetime import date

import pytest

from whale_agent.ingestion.fund_watchlist import Holding
from whale_agent.ingestion.position_history import (
    PriorPositionFact,
    persist_snapshot_holdings,
    prior_position,
)
from whale_agent.storage.db import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


CIK = "0001067983"


def _seed_two_quarters(store):
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
        CIK,
        date(2025, 12, 31),
        "0001-25-000111",
        "https://sec.gov/prior-index.htm",
        prior,
        filed_date=date(2026, 2, 10),
    )
    store.record_13f_holdings(
        CIK,
        date(2026, 3, 31),
        "0001-26-000222",
        "https://sec.gov/current-index.htm",
        current,
        filed_date=date(2026, 5, 12),
    )


def test_no_history_at_all_is_reported_plainly(store):
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "NVIDIA CORPORATION", as_of=date(2026, 6, 1)
    )
    assert fact.status == "no_history_on_file"
    assert fact.current_shares is None
    assert fact.prior_shares is None
    assert "no 13f filing on file" in fact.note.lower()


def test_first_quarter_on_file_has_nothing_to_compare_against(store):
    holdings = [Holding("NVIDIA CORPORATION", 1_000_000_000, 500_000, cusip="67066G104")]
    store.record_13f_holdings(
        CIK,
        date(2026, 3, 31),
        "ACC1",
        "https://sec.gov/index.htm",
        holdings,
        filed_date=date(2026, 5, 1),
    )
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "67066G104", as_of=date(2026, 6, 1)
    )
    assert fact.status == "no_prior_quarter_on_file"
    assert fact.current_shares == 500_000
    assert fact.prior_shares is None
    assert fact.source_accession == "ACC1"
    assert fact.source_url == "https://sec.gov/index.htm"
    assert fact.as_of_report_period == date(2026, 3, 31)


def test_an_increased_position_carries_both_quarters_and_the_delta(store):
    _seed_two_quarters(store)
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "67066G104", as_of=date(2026, 6, 1)
    )
    assert fact.status == "increased"
    assert fact.current_shares == 700_000
    assert fact.prior_shares == 500_000
    assert fact.current_value_usd == 1_600_000_000
    assert fact.prior_value_usd == 1_000_000_000
    assert fact.delta_shares == 200_000
    assert fact.delta_value_usd == 600_000_000
    assert fact.delta_pct == pytest.approx(60.0)
    assert fact.as_of_report_period == date(2026, 3, 31)
    assert fact.prior_report_period == date(2025, 12, 31)
    assert fact.source_accession == "0001-26-000222"
    assert fact.source_url == "https://sec.gov/current-index.htm"
    assert fact.prior_source_accession == "0001-25-000111"
    assert fact.prior_source_url == "https://sec.gov/prior-index.htm"


def test_a_decreased_position_is_labelled_decreased(store):
    _seed_two_quarters(store)
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "68389X105", as_of=date(2026, 6, 1)
    )
    assert fact.status == "decreased"
    assert fact.delta_value_usd == -500_000_000


def test_a_brand_new_name_is_labelled_new_not_no_record(store):
    _seed_two_quarters(store)
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "11135F101", as_of=date(2026, 6, 1)
    )
    assert fact.status == "new"
    assert fact.prior_shares is None
    assert fact.current_shares == 50_000


def test_a_name_dropped_since_last_quarter_is_labelled_closed(store):
    _seed_two_quarters(store)
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "717081103", as_of=date(2026, 6, 1)
    )
    assert fact.status == "closed"
    assert fact.current_shares is None
    assert fact.prior_shares == 100_000


def test_an_issuer_never_seen_in_either_quarter_is_no_record_not_zero(store):
    """Absence from a 13F is not proof of zero. A name that appears in neither quarter
    on file is reported as "we have no record of it", never as a holding of zero."""
    _seed_two_quarters(store)
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "APPLE INC", as_of=date(2026, 6, 1)
    )
    assert fact.status == "no_record"
    assert fact.current_shares is None
    assert fact.prior_shares is None
    assert "no record" in fact.note.lower()


def test_lookup_by_issuer_name_falls_back_when_no_cusip_given(store):
    _seed_two_quarters(store)
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "Nvidia Corporation", as_of=date(2026, 6, 1)
    )
    assert fact.status == "increased"


def test_as_of_excludes_report_periods_in_the_future(store):
    _seed_two_quarters(store)
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "67066G104", as_of=date(2026, 1, 15)
    )
    # Only the prior (2025-12-31) quarter had been filed by 2026-01-15.
    assert fact.status == "no_prior_quarter_on_file"
    assert fact.as_of_report_period == date(2025, 12, 31)
    assert fact.current_shares == 500_000


def test_persist_snapshot_holdings_writes_both_documents(store):
    prior = [Holding("PFIZER INC", 200_000_000, 100_000, cusip="717081103")]
    current = [Holding("NVIDIA CORPORATION", 1_600_000_000, 700_000, cusip="67066G104")]
    persist_snapshot_holdings(
        store,
        manager_cik=CIK,
        current=(
            date(2026, 3, 31),
            "ACC-CUR",
            "https://sec.gov/cur.htm",
            date(2026, 5, 1),
            current,
        ),
        prior=(
            date(2025, 12, 31),
            "ACC-PRI",
            "https://sec.gov/pri.htm",
            date(2026, 2, 1),
            prior,
        ),
    )
    assert store.report_periods_on_file(CIK) == [date(2026, 3, 31), date(2025, 12, 31)]
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "67066G104", as_of=date(2026, 6, 1)
    )
    assert fact.status == "new"


def test_result_is_a_frozen_dataclass_with_full_provenance_fields(store):
    _seed_two_quarters(store)
    fact = prior_position(
        store, CIK, "Berkshire Hathaway", "67066G104", as_of=date(2026, 6, 1)
    )
    assert isinstance(fact, PriorPositionFact)
    for field_name in (
        "manager_cik",
        "manager_name",
        "issuer",
        "cusip",
        "status",
        "as_of_report_period",
        "prior_report_period",
        "current_shares",
        "current_value_usd",
        "prior_shares",
        "prior_value_usd",
        "delta_shares",
        "delta_value_usd",
        "delta_pct",
        "is_option",
        "option_type",
        "source_accession",
        "source_url",
        "prior_source_accession",
        "prior_source_url",
        "note",
    ):
        assert hasattr(fact, field_name)
