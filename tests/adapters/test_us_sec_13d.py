"""Golden-file parse test for the SEC SC 13D/G adapter."""

from __future__ import annotations

from datetime import date

from whale_agent.enrichment.valuation import value_event
from whale_agent.ingestion.us_sec_13d import UsSec13DGAdapter
from whale_agent.models.enums import TransactionType


def test_13d_normalizes_to_activist_with_percent(fixtures_dir):
    raw = (fixtures_dir / "sc13d_activist.json").read_text()
    adapter = UsSec13DGAdapter()
    ev = adapter.normalize(adapter.parse(raw)[0])
    assert ev.transaction_type == TransactionType.ACTIVIST_13D
    assert ev.percent_of_company == 6.2
    assert ev.percentage_threshold_crossed is True
    assert ev.disclosure_date == date(2026, 7, 25)
    assert ev.filer_name == "Elliott Management"


def test_13d_implied_value_computed_from_percentage(fixtures_dir):
    raw = (fixtures_dir / "sc13d_activist.json").read_text()
    adapter = UsSec13DGAdapter()
    ev = adapter.normalize(adapter.parse(raw)[0])
    # value_event returns a Valuation or a Quarantined; the event is valued in place.
    assert value_event(ev)
    # 6.2% of (145M * 15.75) ~= 141.6M
    assert 141_000_000 < ev.implied_usd_value < 142_000_000
