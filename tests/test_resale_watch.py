"""Resale registrations: the filing that says a fund intends to sell.

SharonAI's S-1 of 2026-07-31 registered 7,274,842 of Situational Awareness Partners LP's
7,563,029 shares for resale. No 13F, 13D, 13G or Form 4 carries that, and it is weeks
ahead of any of them.
"""

from __future__ import annotations

from datetime import date

from whale_agent.ingestion.resale_watch import (
    scan_selling_securityholders,
)

# Shape taken from the real filing: a wide table, entity name, then numbers.
S1_TABLE = """
<table>
<tr><td>Selling Securityholder</td><td>Shares Held</td><td>Registered</td><td>After</td></tr>
<tr><td>Some Person (90)</td><td>86,371</td><td>158,803</td><td>&#8212;</td></tr>
<tr><td>Situational Awareness Partners LP (91)</td><td>7,563,029</td><td>7,274,842</td>
    <td>796,108</td></tr>
</table>
"""

NO_MATCH = """
<table><tr><td>Unrelated Holdings LLC</td><td>1,000</td><td>500</td></tr></table>
"""


def test_a_tracked_whale_in_a_selling_table_is_found():
    got = scan_selling_securityholders(
        S1_TABLE, date(2026, 7, 31), "S-1", "https://sec.gov/x/", "SHAZ"
    )
    assert len(got) == 1
    assert got[0].whale_name == "Situational Awareness"
    assert got[0].shares_held == 7_563_029
    assert got[0].shares_registered == 7_274_842


def test_an_untracked_holder_is_ignored():
    assert (
        scan_selling_securityholders(
            NO_MATCH, date(2026, 7, 31), "S-1", "https://sec.gov/x/", "AAA"
        )
        == []
    )


def test_the_generic_phrase_does_not_trigger_a_match():
    """'situational awareness' is ordinary English in defence and AI filings."""
    prose = "<p>improves situational awareness for operators in the field</p>"
    assert (
        scan_selling_securityholders(
            prose, date(2026, 7, 31), "S-1", "https://sec.gov/x/", "AAA"
        )
        == []
    )


def test_footnote_markers_do_not_break_the_name():
    got = scan_selling_securityholders(
        S1_TABLE, date(2026, 7, 31), "S-1", "https://sec.gov/x/", "SHAZ"
    )
    assert "(91)" not in got[0].whale_name


def test_a_row_without_numbers_is_not_reported_as_a_registration():
    html = (
        "<table><tr><td>Situational Awareness Partners LP</td><td>see below</td></tr></table>"
    )
    assert (
        scan_selling_securityholders(
            html, date(2026, 7, 31), "S-1", "https://sec.gov/x/", "AAA"
        )
        == []
    )


def test_the_registration_describes_itself_for_a_reader():
    got = scan_selling_securityholders(
        S1_TABLE, date(2026, 7, 31), "S-1", "https://sec.gov/x/", "SHAZ"
    )[0]
    text = got.describe()
    assert "7,274,842" in text
    assert "SHAZ" in text
    assert "registered for resale" in text


def test_a_missing_symbol_does_not_reach_the_reader_as_the_word_none():
    """FMP returns the string "None" for issuers it has no ticker for."""
    got = scan_selling_securityholders(
        S1_TABLE, date(2026, 7, 31), "S-1", "https://sec.gov/x/", "None"
    )[0]
    assert got.symbol == ""
    assert "None" not in got.describe()


def test_the_issuer_name_is_used_when_there_is_no_ticker():
    got = scan_selling_securityholders(
        S1_TABLE,
        date(2026, 7, 31),
        "S-1",
        "https://sec.gov/x/",
        "None",
        issuer="SharonAI Holdings Inc.",
    )[0]
    assert "in SharonAI Holdings Inc." in got.describe()


def test_the_whole_window_is_scanned_not_only_its_most_recent_day():
    """A week is ~200 registrations and scanning all of them measured 36s.

    The first cap was 40 newest-first, which reached back one day and dropped the
    SharonAI S-1 that is the reason this module exists.
    """
    import inspect

    from whale_agent.ingestion import resale_watch

    default = (
        inspect.signature(resale_watch.fetch_resale_registrations)
        .parameters["max_documents"]
        .default
    )
    assert default >= 250


# -- discovery must not depend on what hour the job runs ------------------------
#
# The vendor filing search caps each form type at 100 rows. Measured on 2026-08-04 for
# the window 2026-07-28..08-04, both S-1 and 424B3 came back full at 100 rows and every
# row was dated 2026-08-04 -- the same query run that morning still reached 2026-07-31
# and found the SharonAI S-1. The window silently shrinks as the current day fills up,
# so the same code returns different answers at different times of day. Discovery reads
# EDGAR's daily index, which has no page cap.


def test_daily_index_can_select_registration_forms():
    from whale_agent.ingestion.fund_watchlist import parse_daily_index

    idx = (
        "Form Type   Company Name                  CIK        Date Filed  File Name\n"
        "-----------------------------------------------------------------------\n"
        "S-1              SharonAI Holdings Inc.   2068385    20260731    edgar/data/2068385/0001493152-26-035629.txt\n"
        "424B3            Some Issuer Inc.         1234567    20260731    edgar/data/1234567/0001111111-26-000001.txt\n"
        "424B2            A Bank                   7654321    20260731    edgar/data/7654321/0002222222-26-000002.txt\n"
        "SCHEDULE 13G     Unrelated Corp           1111111    20260731    edgar/data/1111111/0003333333-26-000003.txt\n"
    )
    rows = parse_daily_index(idx, forms=("S-1", "424B3"))
    assert {r.form for r in rows} == {"S-1", "424B3"}
    assert "0001493152-26-035629" in {r.accession for r in rows}


def test_registration_form_selection_is_exact_not_prefix():
    """424B2 is a shelf takedown, not a resale registration; S-1/A is an amendment.

    A prefix match on "S-1" or "424B" would pull in ~1,000 424B2 filings a day and cost
    a document fetch for each one.
    """
    from whale_agent.ingestion.fund_watchlist import parse_daily_index

    idx = (
        "Form Type   Company Name   CIK   Date Filed  File Name\n"
        "------------------------------------------------------\n"
        "S-1/A            X Corp     111   20260731    edgar/data/111/0000000001-26-000001.txt\n"
        "424B2            Y Corp     222   20260731    edgar/data/222/0000000002-26-000002.txt\n"
    )
    assert parse_daily_index(idx, forms=("S-1", "424B3")) == []


def test_stake_forms_remain_the_default_selection():
    """The 13D/G caller passes no `forms` and must keep its prefix behaviour."""
    from whale_agent.ingestion.fund_watchlist import parse_daily_index

    idx = (
        "Form Type   Company Name   CIK   Date Filed  File Name\n"
        "------------------------------------------------------\n"
        "SCHEDULE 13G/A   A Corp     111   20260731    edgar/data/111/0000000001-26-000001.txt\n"
        "S-1              B Corp     222   20260731    edgar/data/222/0000000002-26-000002.txt\n"
    )
    rows = parse_daily_index(idx)
    assert [r.form for r in rows] == ["SCHEDULE 13G/A"]


def test_primary_document_is_chosen_from_the_filing_index():
    """The daily index yields a directory, not a document.

    The complete-submission .txt for the SharonAI S-1 is 15 MB (it inlines every
    exhibit); the primary document is 4.6 MB. Fetching the .txt for every candidate in a
    week would move hundreds of megabytes to read the same tables.
    """
    from whale_agent.ingestion.resale_watch import pick_primary_document

    listing = {
        "directory": {
            "item": [
                {"name": "0001493152-26-035629-index.htm", "size": "12000"},
                {"name": "forms-1.htm", "size": "4649408"},
                {"name": "ex23-1.htm", "size": "5000"},
                {"name": "R1.htm", "size": "9000"},
            ]
        }
    }
    assert pick_primary_document(listing) == "forms-1.htm"


def test_primary_document_ignores_index_and_exhibit_pages():
    from whale_agent.ingestion.resale_watch import pick_primary_document

    listing = {
        "directory": {
            "item": [
                {"name": "0001-index.htm", "size": "999999"},
                {"name": "ex99-1.htm", "size": "800000"},
                {"name": "d424b3.htm", "size": "300000"},
            ]
        }
    }
    assert pick_primary_document(listing) == "d424b3.htm"


def test_primary_document_returns_empty_when_nothing_usable():
    from whale_agent.ingestion.resale_watch import pick_primary_document

    assert pick_primary_document({"directory": {"item": []}}) == ""
    assert pick_primary_document({}) == ""


def test_empty_resale_section_states_how_many_filings_were_read():
    """An empty section must be evidence of absence, not indistinguishable from a
    failed sweep. Saying nothing was found is only credible with a count attached."""
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_resale_watch

    text = "\n".join(render_resale_watch([], _d(2026, 8, 4), _d(2026, 7, 28), examined=164))
    assert "164" in text


def test_populated_resale_section_also_states_the_count():
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_resale_watch

    text = "\n".join(render_resale_watch([], _d(2026, 8, 4), _d(2026, 7, 28), examined=0))
    assert "164" not in text
