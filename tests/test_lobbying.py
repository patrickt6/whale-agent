"""Golden-file tests for the Senate LDA lobbying adapter.

The fixture holds three real filings, chosen because they cover the three shapes the
dataset actually has: an outside firm reporting `income`, a company reporting its own
`expenses`, and a registration reporting neither. The income/expenses distinction is the
thing most likely to be silently broken by a later edit, so most of these tests are
about it.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from whale_agent.config import Settings
from whale_agent.ingestion.gov.lobbying import (
    SenateLdaAdapter,
    period_start,
    reported_amount,
    total_spend,
)

KEYLESS = Settings()  # the LDA API needs no credential


@pytest.fixture
def filings(fixtures_dir) -> list[dict]:
    raw = json.loads((fixtures_dir / "lda_filings.json").read_text())
    return SenateLdaAdapter(KEYLESS).parse(raw)


def test_it_parses_the_filings_envelope_into_rows(filings):
    assert len(filings) == 3
    assert filings[0]["client"]["name"] == "LOCKHEED MARTIN CORPORATION"
    assert filings[1]["registrant"]["name"] == "BOEING COMPANY"


def test_an_outside_firms_filing_reports_income_and_no_expenses(filings):
    amount, basis = reported_amount(filings[0])
    assert (amount, basis) == (50_000.0, "income")
    assert filings[0]["expenses"] is None


def test_a_self_filing_company_reports_expenses_and_no_income(filings):
    amount, basis = reported_amount(filings[1])
    assert (amount, basis) == (2_450_000.0, "expenses")
    assert filings[1]["income"] is None


def test_a_registration_reports_neither_and_is_not_read_as_zero(filings):
    amount, basis = reported_amount(filings[2])
    assert amount is None
    assert basis == ""


def test_annotations_carry_the_quarter_start_not_the_posting_date(filings):
    annotations = SenateLdaAdapter(KEYLESS).annotations(filings, ticker="lmt")
    assert annotations[0].as_of == date(2026, 1, 1)  # first_quarter of 2026
    assert annotations[0].ticker == "LMT"
    assert annotations[0].kind == "lobbying"


def test_a_self_filers_annotation_names_its_accounting_method(filings):
    annotation = SenateLdaAdapter(KEYLESS).annotations(filings)[1]
    assert annotation.amount_usd == 2_450_000.0
    assert "in-house lobbying spend" in annotation.label
    assert "IRC 162(e)" in annotation.label


def test_an_unpriced_filing_keeps_a_null_amount_rather_than_zero(filings):
    annotation = SenateLdaAdapter(KEYLESS).annotations(filings)[2]
    assert annotation.amount_usd is None
    assert "registration" in annotation.label


def test_unpriced_filings_can_be_excluded_when_a_caller_wants_only_spend(filings):
    adapter = SenateLdaAdapter(KEYLESS)
    assert len(adapter.annotations(filings, include_unpriced=False)) == 2


def test_totalling_spend_skips_the_filings_that_reported_nothing(filings):
    annotations = SenateLdaAdapter(KEYLESS).annotations(filings)
    assert total_spend(annotations) == 2_500_000.0


def test_annotations_link_to_the_filings_public_document(filings):
    annotation = SenateLdaAdapter(KEYLESS).annotations(filings)[0]
    assert annotation.source_url.startswith("https://lda.senate.gov/filings/public/")


def test_pagination_follows_the_absolute_next_url_the_api_returns(fixtures_dir):
    """The envelope hands back a full `next` URL; page 2 has none, which ends the walk."""
    page1 = json.loads((fixtures_dir / "lda_filings.json").read_text())
    page2 = json.loads((fixtures_dir / "lda_filings_page2.json").read_text())
    assert page1["next"].startswith("https://lda.senate.gov/api/v1/filings/")
    assert page2["next"] is None

    adapter = SenateLdaAdapter(KEYLESS)
    rows = adapter.parse(page1) + adapter.parse(page2)
    assert len(rows) == 4


def test_a_semiannual_archive_period_still_resolves_to_a_date():
    assert period_start({"filing_year": 2005, "filing_period": "year_end"}) == date(2005, 7, 1)


def test_an_undatable_filing_falls_back_to_when_it_was_posted():
    row = {"filing_period": "unknown", "dt_posted": "2026-04-02T16:20:15-04:00"}
    assert period_start(row) == date(2026, 4, 2)
    assert period_start({}) is None


def test_it_refuses_to_pretend_lobbying_spend_is_a_whale_event(filings):
    with pytest.raises(NotImplementedError):
        SenateLdaAdapter(KEYLESS).normalize(filings[0])


def test_the_base_url_stays_on_the_senate_host():
    # lda.gov answers 403 to programmatic clients; only lda.senate.gov works.
    assert Settings().senate_lda_base_url == "https://lda.senate.gov/api/v1"
