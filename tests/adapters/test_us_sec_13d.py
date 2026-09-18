"""Golden-file parse test for the SEC SC 13D/G adapter."""

from __future__ import annotations

import json
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
    assert ev.filer_name == "Example Activist Partners"


def test_13d_implied_value_computed_from_percentage(fixtures_dir):
    raw = (fixtures_dir / "sc13d_activist.json").read_text()
    adapter = UsSec13DGAdapter()
    ev = adapter.normalize(adapter.parse(raw)[0])
    # value_event returns a Valuation or a Quarantined; the event is valued in place.
    assert value_event(ev)
    # 6.2% of (145M * 15.75) ~= 141.6M
    assert 141_000_000 < ev.implied_usd_value < 142_000_000


# -- Defect #1 regression: the filer must never be the issuer ------------------------


def test_13d_names_the_filer_not_the_issuer(fixtures_dir):
    """The exact bug this defect describes: `filer_name` must be the reporting person,
    and must never equal the issuer's name."""
    raw = (fixtures_dir / "sc13d_activist.json").read_text()
    adapter = UsSec13DGAdapter()
    parsed = adapter.parse(raw)[0]
    ev = adapter.normalize(parsed)
    assert ev.filer_name == "Example Activist Partners"
    assert ev.issuer_name == "Target Industries Inc"
    assert ev.filer_name != ev.issuer_name


def test_parse_drops_a_row_whose_filer_name_equals_the_issuer(fixtures_dir):
    """No code path may attribute a 13D/G filing to its own issuer. `parse()` is the
    single choke point every row passes through (live fetch or fixture), so this is
    where the regression is pinned: a row shaped like the "23 rows disclosing a stake
    in itself" bug must never reach a NormalizedEvent."""
    raw = json.dumps(
        {
            "form": "SC 13D",
            "issuer_name": "GENCO SHIPPING & TRADING LTD",
            "issuer_cusip": "368750102",
            "filer_name": "GENCO SHIPPING & TRADING LTD",
            "filer_cik": "0001045986",
            "percent_of_class": 5.1,
            "shares_owned": 1_000_000,
            "filing_date": "2026-07-25",
        }
    )
    adapter = UsSec13DGAdapter()
    assert adapter.parse(raw) == []


def test_parse_drops_a_row_with_no_filer_name_at_all():
    """An unidentifiable filer must be skipped, never defaulted to anything."""
    raw = json.dumps(
        {
            "form": "SC 13G",
            "issuer_name": "Some Issuer Inc",
            "filer_name": None,
            "percent_of_class": 5.1,
            "filing_date": "2026-07-25",
        }
    )
    adapter = UsSec13DGAdapter()
    assert adapter.parse(raw) == []


class _FakeReportingPerson:
    def __init__(self, name, cik, aggregate_amount, percent_of_class):
        self.name = name
        self.cik = cik
        self.aggregate_amount = aggregate_amount
        self.percent_of_class = percent_of_class


class _FakeIssuerInfo:
    def __init__(self, cik, name, cusip):
        self.cik = cik
        self.name = name
        self.cusip = cusip


class _FakeSchedule13D:
    """Stands in for edgartools' `Schedule13D`/`Schedule13G`: holds `reporting_persons`
    and `issuer_info`, and deliberately has no `filer_name`/`percent_of_class`/
    `aggregate_amount` attributes of its own -- exactly the shape that broke the old
    adapter, which read those three names directly off this object."""

    def __init__(self, issuer_info, reporting_persons):
        self.issuer_info = issuer_info
        self.reporting_persons = reporting_persons


class _FakeFiling:
    def __init__(self, obj, *, form, company, cik, filing_date="2026-07-30"):
        self._obj = obj
        self.form = form
        self.company = company
        self.cik = cik
        self.filing_date = filing_date
        self.accession_no = "0001045986-26-000123"

    def obj(self):
        return self._obj


def test_filing_to_covers_emits_one_event_per_reporting_person():
    """Two joint filers on one 13D cover page must become two rows, each naming the
    actual person -- not the issuer once for the whole filing."""
    issuer_info = _FakeIssuerInfo(
        cik="0000320193", name="Genco Shipping & Trading Ltd", cusip="368750102"
    )
    persons = [
        _FakeReportingPerson("Whale Capital LP", "0001111111", 9_000_000, 6.2),
        _FakeReportingPerson("Whale Capital GP LLC", "0001111112", 9_000_000, 6.2),
    ]
    filing = _FakeFiling(
        _FakeSchedule13D(issuer_info, persons),
        form="SC 13D",
        company="Genco Shipping & Trading Ltd",
        cik="0000320193",
    )

    covers = UsSec13DGAdapter._filing_to_covers(filing)

    assert len(covers) == 2
    names = {c["filer_name"] for c in covers}
    assert names == {"Whale Capital LP", "Whale Capital GP LLC"}
    for cover in covers:
        assert cover["issuer_name"] == "Genco Shipping & Trading Ltd"
        assert cover["filer_name"] != cover["issuer_name"]
        assert cover["percent_of_class"] == 6.2
        assert cover["shares_owned"] == 9_000_000


def test_filing_to_covers_skips_reporting_persons_with_no_name():
    """No fallback to the issuer: a reporting person the library could not name is
    dropped, not attributed to the subject company."""
    issuer_info = _FakeIssuerInfo(
        cik="0000320193", name="Genco Shipping & Trading Ltd", cusip="368750102"
    )
    persons = [_FakeReportingPerson(None, "0001111111", 9_000_000, 6.2)]
    filing = _FakeFiling(
        _FakeSchedule13D(issuer_info, persons),
        form="SC 13D",
        company="Genco Shipping & Trading Ltd",
        cik="0000320193",
    )

    covers = UsSec13DGAdapter._filing_to_covers(filing)

    assert covers == []


def test_filing_to_covers_emits_nothing_when_reporting_persons_is_empty():
    """A filing whose cover page has no reporting persons at all yields zero rows --
    it must never fall back to treating the issuer as its own filer."""
    issuer_info = _FakeIssuerInfo(
        cik="0000320193", name="Genco Shipping & Trading Ltd", cusip="368750102"
    )
    filing = _FakeFiling(
        _FakeSchedule13D(issuer_info, []),
        form="SC 13D",
        company="Genco Shipping & Trading Ltd",
        cik="0000320193",
    )

    covers = UsSec13DGAdapter._filing_to_covers(filing)

    assert covers == []


def test_end_to_end_cover_to_event_never_names_issuer_as_filer():
    """Full path: fake filing -> _filing_to_covers -> parse -> normalize. Confirms the
    fix holds across the adapter's whole surface, not just one function."""
    issuer_info = _FakeIssuerInfo(
        cik="0000320193", name="Genco Shipping & Trading Ltd", cusip="368750102"
    )
    persons = [_FakeReportingPerson("Situational Awareness LP", "0001111111", 9_000_000, 6.2)]
    filing = _FakeFiling(
        _FakeSchedule13D(issuer_info, persons),
        form="SC 13G",
        company="Genco Shipping & Trading Ltd",
        cik="0000320193",
    )

    adapter = UsSec13DGAdapter()
    covers = adapter._filing_to_covers(filing)
    assert len(covers) == 1
    raw = json.dumps(covers[0])
    rows = adapter.parse(raw)
    assert len(rows) == 1
    ev = adapter.normalize(rows[0])
    assert ev.filer_name == "Situational Awareness LP"
    assert ev.issuer_name == "Genco Shipping & Trading Ltd"
    assert ev.filer_name != ev.issuer_name
    assert ev.transaction_type == TransactionType.PASSIVE_13G


def test_a_zero_stake_falls_back_to_the_primary_document(monkeypatch):
    """edgartools zeroes the numerics on filings it cannot read structurally.

    A 13D/G exists because someone crossed 5%, so 0.0 is a parse failure and not a
    fact about the position.
    """
    from whale_agent.ingestion import us_sec_13d

    primary = """<edgarSubmission>
      <issuerName>KUSTOM ENTERTAINMENT, INC.</issuerName>
      <percentOfClass>7.4</percentOfClass>
      <aggregateAmountOwned>1250000</aggregateAmountOwned>
    </edgarSubmission>"""
    monkeypatch.setattr(us_sec_13d, "_primary_document", lambda cik, acc: primary)

    cover = {
        "filer_name": "Martin Ryan Todd",
        "issuer_name": "KUSTOM ENTERTAINMENT, INC.",
        "percent_of_class": 0.0,
        "shares_owned": 0,
        "filer_cik": "1342958",
        "accession": "0002148029-26-000004",
    }
    filled = us_sec_13d.fill_missing_stake(cover)
    assert filled["percent_of_class"] == 7.4
    assert filled["shares_owned"] == 1_250_000


def test_a_stake_the_library_read_correctly_is_left_alone(monkeypatch):
    from whale_agent.ingestion import us_sec_13d

    def explode(cik, acc):  # pragma: no cover - must not be called
        raise AssertionError("should not fetch when the stake is already known")

    monkeypatch.setattr(us_sec_13d, "_primary_document", explode)
    cover = {"percent_of_class": 9.9, "shares_owned": 500, "filer_cik": "1", "accession": "2"}
    assert us_sec_13d.fill_missing_stake(cover) == cover


def test_an_unreadable_primary_document_leaves_the_row_unchanged(monkeypatch):
    from whale_agent.ingestion import us_sec_13d

    monkeypatch.setattr(us_sec_13d, "_primary_document", lambda cik, acc: "")
    cover = {"percent_of_class": 0.0, "shares_owned": 0, "filer_cik": "1", "accession": "2"}
    assert us_sec_13d.fill_missing_stake(cover)["percent_of_class"] == 0.0
