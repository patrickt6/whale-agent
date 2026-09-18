"""Who the filer actually is: the single most useful thing the table was missing.

A row reading "HALVORSEN ERIK R bought $131.4M of Meridian Pharmaceuticals" asks the
reader to go and find out whether that is the CEO or a passive holder. FMP has been
sending the answer all along in `typeOfOwner`, on 96 percent of rows, and it was being
dropped into `raw_payload` and never read.
"""

from __future__ import annotations

from tests.conftest import make_event
from whale_agent.enrichment.roles import role_for, tidy_role


def test_a_plain_title_is_capitalised_not_shouted():
    assert tidy_role("officer: Chief Financial Officer") == "Chief Financial Officer"


def test_a_bare_relationship_is_kept_as_a_role():
    assert tidy_role("director") == "Director"
    assert tidy_role("10 percent owner") == "10% owner"


def test_a_combined_relationship_keeps_both_parts():
    assert (
        tidy_role("director, officer: Chief Executive Officer")
        == "Director, Chief Executive Officer"
    )


def test_a_trailing_colon_with_no_title_does_not_leave_dangling_punctuation():
    """FMP emits 'director, 10 percent owner: ' with an empty title on some rows."""
    assert tidy_role("director, 10 percent owner: ") == "Director, 10% owner"


def test_an_empty_or_missing_role_is_none_never_a_placeholder():
    for value in ("", "   ", None, "n/a", "unknown"):
        assert tidy_role(value) is None


def test_no_em_dashes_are_introduced():
    assert "—" not in (tidy_role("director, officer: Chairman, President & CEO") or "")


def test_role_is_read_from_the_stored_vendor_payload():
    """Existing rows keep their role without a re-backfill: it is already in raw_payload."""
    event = make_event(
        filer_name="A",
        raw_payload={"typeOfOwner": "director, officer: Chairman, President & CEO"},
    )
    assert role_for(event) == "Director, Chairman, President & CEO"


def test_an_explicit_field_wins_over_the_raw_payload():
    event = make_event(
        filer_name="A",
        filer_role="Chief Executive Officer",
        raw_payload={"typeOfOwner": "director"},
    )
    assert role_for(event) == "Chief Executive Officer"


def test_a_row_with_no_role_anywhere_returns_none():
    assert role_for(make_event(filer_name="A")) is None


def test_form4_relationship_flags_become_a_role():
    """SEC ships these as separate booleans plus a free-text officer title."""
    event = make_event(
        filer_name="A",
        raw_payload={"is_director": True, "is_officer": True, "officer_title": "President"},
    )
    assert role_for(event) == "Director, President"


def test_form4_ten_percent_owner_flag_alone():
    event = make_event(filer_name="A", raw_payload={"is_ten_percent_owner": True})
    assert role_for(event) == "10% owner"
