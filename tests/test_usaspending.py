"""Golden-file tests for the USASpending federal-award adapter.

The fixture is a real response captured from api.usaspending.gov. No network here: if
USASpending renames `Award Amount` or drops `Start Date`, this fails long before the
weekly report quietly loses its contract context.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from whale_agent.config import Settings
from whale_agent.ingestion.gov.usaspending import (
    CONTRACT_AWARD_TYPES,
    UsaSpendingAdapter,
    recipient_name_for,
)

KEYLESS = Settings()  # USASpending needs no credential at all


@pytest.fixture
def awards(fixtures_dir) -> list[dict]:
    raw = json.loads((fixtures_dir / "usaspending_awards.json").read_text())
    return UsaSpendingAdapter(KEYLESS).parse(raw)


def test_it_parses_every_award_row_from_a_real_response(awards):
    assert len(awards) == 3
    assert awards[0]["Award ID"] == "W31P4Q26C0013"
    assert awards[0]["Recipient Name"] == "LOCKHEED MARTIN CORPORATION"


def test_it_turns_awards_into_dated_dollar_annotations(awards):
    annotations = UsaSpendingAdapter(KEYLESS).annotations(awards, ticker="lmt")
    top = annotations[0]
    assert top.kind == "gov_contract"
    assert top.ticker == "LMT"
    assert top.amount_usd == 4_761_000_000.0
    assert top.as_of == date(2026, 4, 9)
    assert "Department of the Army" in top.label
    assert top.source == "usaspending"


def test_an_award_with_no_start_date_is_dropped_rather_than_dated_today():
    rows = [{"Award ID": "X", "Recipient Name": "ACME", "Award Amount": 1_000_000}]
    assert UsaSpendingAdapter(KEYLESS).annotations(rows) == []


def test_the_search_body_asks_only_for_newly_awarded_contracts():
    body = UsaSpendingAdapter(KEYLESS).search_body(
        "Lockheed Martin", start=date(2026, 1, 1), end=date(2026, 7, 26)
    )
    period = body["filters"]["time_period"][0]
    # Without this the API filters on transaction date and happily returns a contract
    # that started in 1993 -- verified live, and badly misleading next to a Form 4.
    assert period["date_type"] == "new_awards_only"
    assert body["filters"]["award_type_codes"] == CONTRACT_AWARD_TYPES
    assert body["filters"]["recipient_search_text"] == ["Lockheed Martin"]


def test_tickers_map_to_the_recipient_name_the_api_matches_on():
    assert recipient_name_for("lmt") == "Lockheed Martin"
    assert recipient_name_for("ZZZZ") is None
    assert recipient_name_for(None) is None


def test_it_refuses_to_pretend_an_award_is_a_whale_event(awards):
    with pytest.raises(NotImplementedError):
        UsaSpendingAdapter(KEYLESS).normalize(awards[0])


def test_a_junk_payload_yields_no_rows_instead_of_raising():
    adapter = UsaSpendingAdapter(KEYLESS)
    assert adapter.parse(None) == []
    assert adapter.parse({"results": "not a list"}) == []


def test_an_award_annotation_links_back_to_its_public_page():
    row = {"generated_internal_id": "CONT_AWD_X_9700_-NONE-_-NONE-"}
    assert UsaSpendingAdapter.award_url(row) == (
        "https://www.usaspending.gov/award/CONT_AWD_X_9700_-NONE-_-NONE-"
    )
    assert UsaSpendingAdapter.award_url({}) is None
