"""Every form a whale filed in the window, not only the two we thought mattered."""

from __future__ import annotations

from datetime import date

from whale_agent.ingestion.fund_watchlist import filings_from_fmp_rows

ROWS = [
    {
        "filingDate": "2026-07-02 00:00:00",
        "formType": "4",
        "finalLink": "https://www.sec.gov/Archives/edgar/data/2045724/000093583626000339/x.htm",
    },
    {
        "filingDate": "2026-06-29 00:00:00",
        "formType": "SC 13G",
        "finalLink": "https://www.sec.gov/Archives/edgar/data/2045724/000093583626000334/x.htm",
    },
    {
        "filingDate": "2026-05-18 00:00:00",
        "formType": "13F-HR",
        "finalLink": "https://www.sec.gov/Archives/edgar/data/2045724/000204572426000008/x.htm",
    },
]


def test_every_form_type_survives():
    """13F-HR used to be dropped on the floor by the FAST_FORMS filter."""
    got = filings_from_fmp_rows(ROWS, date(2026, 1, 1), date(2026, 8, 3))
    assert sorted(f.form for f in got) == ["13F-HR", "4", "SC 13G"]


def test_filings_outside_the_window_are_excluded():
    got = filings_from_fmp_rows(ROWS, date(2026, 6, 1), date(2026, 8, 3))
    assert sorted(f.form for f in got) == ["4", "SC 13G"]


def test_the_accession_is_recovered_from_the_link():
    got = filings_from_fmp_rows(ROWS[:1], date(2026, 1, 1), date(2026, 8, 3))
    assert got[0].accession == "000093583626000339"
    # The readable EDGAR index page, not the bare accession directory. The FMP path
    # hands accessions with their dashes already stripped, so the filename is
    # rebuilt in the standard 10-2-6 grouping rather than omitted.
    assert got[0].url.endswith("/000093583626000339/0000935836-26-000339-index.htm")


def test_a_row_without_a_usable_link_is_dropped_not_guessed():
    assert (
        filings_from_fmp_rows(
            [{"filingDate": "2026-07-02 00:00:00", "formType": "4", "finalLink": ""}],
            date(2026, 1, 1),
            date(2026, 8, 3),
        )
        == []
    )


def test_duplicate_accessions_appear_once():
    """FMP returns one row per ticker, so one filing arrives several times."""
    got = filings_from_fmp_rows(ROWS[:1] * 3, date(2026, 1, 1), date(2026, 8, 3))
    assert len(got) == 1


def test_results_are_newest_first():
    got = filings_from_fmp_rows(ROWS, date(2026, 1, 1), date(2026, 8, 3))
    assert [f.filed.isoformat() for f in got] == ["2026-07-02", "2026-06-29", "2026-05-18"]
