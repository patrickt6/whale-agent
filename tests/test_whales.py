"""One fund, many CIKs.

Situational Awareness files under four registrants. Its SHAZ position is disclosed
under Partners LP while its 13G and Form 4 come from LP, so without aliasing the
report shows pieces of one fund as unrelated entities.
"""

from __future__ import annotations

from whale_agent.ingestion.whales import (
    WHALES,
    all_ciks,
    whale_for_cik,
    whale_for_name,
)


def test_every_cik_is_ten_digits():
    for whale in WHALES:
        for cik in whale.ciks:
            assert len(cik) == 10 and cik.isdigit(), f"{whale.name}: {cik}"


def test_no_cik_belongs_to_two_whales():
    seen: dict[str, str] = {}
    for whale in WHALES:
        for cik in whale.ciks:
            assert cik not in seen, f"{cik} in both {seen.get(cik)} and {whale.name}"
            seen[cik] = whale.name


def test_all_four_situational_awareness_entities_are_one_whale():
    whale = whale_for_cik("0002045724")
    assert whale is not None
    assert whale.name == "Situational Awareness"
    for cik in ("0002038540", "0002048430", "0002047424"):
        assert whale_for_cik(cik) is whale


def test_an_unknown_cik_is_not_invented():
    assert whale_for_cik("0000000001") is None


def test_a_name_in_a_filing_resolves_to_its_whale():
    """S-1 tables say 'Situational Awareness Partners LP', not a CIK."""
    assert whale_for_name("Situational Awareness Partners LP").name == "Situational Awareness"
    assert whale_for_name("SITUATIONAL AWARENESS LP").name == "Situational Awareness"


def test_an_unrelated_name_does_not_match():
    """The phrase is generic in defence and AI writing; only the entity counts."""
    assert whale_for_name("situational awareness training for pilots") is None


def test_berkshire_resolves_by_name():
    assert whale_for_name("BERKSHIRE HATHAWAY INC").name == "Berkshire Hathaway"


def test_all_ciks_covers_every_whale():
    assert len(all_ciks()) == sum(len(w.ciks) for w in WHALES)
    assert "0001067983" in all_ciks()


def test_whale_is_hashable_so_it_can_key_a_dict():
    assert len({whale_for_cik("0002045724"), whale_for_cik("0002038540")}) == 1
