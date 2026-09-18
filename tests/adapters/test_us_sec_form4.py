"""Golden-file parse test for the SEC Form 4 adapter.

If SEC changes the ownershipDocument format, this fails before production breaks.
"""

from __future__ import annotations

from datetime import date

from whale_agent.enrichment.tickers import StaticTickerResolver
from whale_agent.enrichment.valuation import value_event
from whale_agent.ingestion.us_sec import UsSecForm4Adapter, edgar_filing_url
from whale_agent.models.enums import PriceSource, TransactionType


def test_form4_explodes_into_one_row_per_transaction(fixtures_dir):
    raw = (fixtures_dir / "form4_purchase.xml").read_text()
    rows = UsSecForm4Adapter().parse(raw)
    assert len(rows) == 2
    assert rows[0]["code"] == "P"
    assert rows[0]["shares"] == 500_000
    assert rows[0]["price"] == 15.0
    assert rows[0]["issuer_name"] == "Acme Micro Corp"
    assert rows[0]["filer_name"] == "Jane Q. Example"


def test_form4_normalizes_purchase_to_open_market_buy(fixtures_dir):
    raw = (fixtures_dir / "form4_purchase.xml").read_text()
    adapter = UsSecForm4Adapter()
    ev = adapter.normalize(adapter.parse(raw)[0])
    assert ev.transaction_type == TransactionType.OPEN_MARKET_BUY
    assert ev.transaction_date == date(2026, 7, 24)
    assert ev.price_source == PriceSource.FILING_STATED
    assert ev.filer_id == "0001214156"
    assert ev.issuer_id == "0000320193"


def test_form4_value_uses_stated_price_exactly(fixtures_dir):
    raw = (fixtures_dir / "form4_purchase.xml").read_text()
    adapter = UsSecForm4Adapter()
    ev = adapter.normalize(adapter.parse(raw)[0])
    # value_event returns a Valuation or a Quarantined; the event is valued in place.
    assert value_event(ev)
    assert ev.usd_value == 7_500_000  # 500k * $15.00
    assert ev.usd_value_is_estimate is False


def test_form4_includes_derivative_transactions_clearly_typed(fixtures_dir):
    """Defect #4: option exercises/RSU vesting live in derivativeTable, not
    nonDerivativeTable. Dropping that table silently discards them; this asserts they
    are parsed and tagged so nothing downstream can mistake them for a plain
    open-market transaction."""
    raw = (fixtures_dir / "form4_derivative_exercise.xml").read_text()
    rows = UsSecForm4Adapter().parse(raw)
    assert len(rows) == 1
    row = rows[0]
    assert row["instrument_type"] == "derivative"
    assert row["code"] == "M"
    assert row["shares"] == 20_000
    assert row["conversion_or_exercise_price"] == 10.0
    assert row["underlying_security_title"] == "Common Stock"


def test_form4_normalizes_derivative_exercise_to_option_exercise(fixtures_dir):
    raw = (fixtures_dir / "form4_derivative_exercise.xml").read_text()
    adapter = UsSecForm4Adapter()
    ev = adapter.normalize(adapter.parse(raw)[0])
    assert ev.transaction_type == TransactionType.OPTION_EXERCISE
    assert ev.raw_payload["instrument_type"] == "derivative"


def test_form4_mixed_filing_includes_both_derivative_and_non_derivative_rows(fixtures_dir):
    """A filing with both tables must not drop either one."""
    non_derivative_xml = (fixtures_dir / "form4_purchase.xml").read_text()
    derivative_xml = (fixtures_dir / "form4_derivative_exercise.xml").read_text()
    # Splice the derivativeTable from the second fixture into the first's document.
    derivative_block = derivative_xml.split("<derivativeTable>")[1].split(
        "</derivativeTable>"
    )[0]
    combined = non_derivative_xml.replace(
        "</ownershipDocument>",
        f"<derivativeTable>{derivative_block}</derivativeTable></ownershipDocument>",
    )
    rows = UsSecForm4Adapter().parse(combined)
    instrument_types = [r["instrument_type"] for r in rows]
    assert instrument_types.count("non_derivative") == 2
    assert instrument_types.count("derivative") == 1


def test_form4_carries_a_ticker_when_the_resolver_knows_the_issuer_cik(fixtures_dir):
    """The ownership XML names the issuer by CIK only, so enrichment needs a translation.

    Without this the event reaches market_context.py with no ticker and is skipped
    outright, which silently applies the small-cap and percent-of-float terms to only
    the vendor-sourced half of the corpus.
    """
    raw = (fixtures_dir / "form4_purchase.xml").read_text()
    adapter = UsSecForm4Adapter(StaticTickerResolver({"320193": "AAPL"}))
    ev = adapter.normalize(adapter.parse(raw)[0])
    assert ev.ticker == "AAPL"


def test_form4_leaves_the_ticker_unset_when_no_resolver_is_injected(fixtures_dir):
    """parse/normalize stay pure and offline; resolution is opt-in via the constructor."""
    raw = (fixtures_dir / "form4_purchase.xml").read_text()
    adapter = UsSecForm4Adapter()
    assert adapter.normalize(adapter.parse(raw)[0]).ticker is None


def test_form4_ticker_survives_a_resolver_that_raises(fixtures_dir):
    """An unavailable resolver costs the event its enrichment, never the run its digest."""

    class Exploding:
        def resolve_cik(self, cik):
            raise RuntimeError("boom")

    raw = (fixtures_dir / "form4_purchase.xml").read_text()
    adapter = UsSecForm4Adapter(Exploding())
    assert adapter.normalize(adapter.parse(raw)[0]).ticker is None


def test_form4_builds_a_filing_url_from_the_envelope_metadata(fixtures_dir):
    """Every row promises a "read the filing" link; the XML itself contains no URL.

    CIK and accession number are both in the filing metadata, so an unlinked EDGAR row
    is a reconstruction we declined to do rather than information we lack.
    """
    raw = (fixtures_dir / "form4_purchase.xml").read_text()
    adapter = UsSecForm4Adapter()
    rows = adapter.parse(
        {"xml": raw, "accession": "0001193125-26-226661", "filing_cik": "0000320193"}
    )
    ev = adapter.normalize(rows[0])
    assert ev.source_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000119312526226661/"
        "0001193125-26-226661-index.htm"
    )


def test_form4_prefers_a_url_the_source_supplied(fixtures_dir):
    """Reconstruction is a fallback, not an override of a real link."""
    raw = (fixtures_dir / "form4_purchase.xml").read_text()
    adapter = UsSecForm4Adapter()
    rows = adapter.parse({"xml": raw, "source_url": "https://example.test/filing"})
    assert adapter.normalize(rows[0]).source_url == "https://example.test/filing"


def test_edgar_filing_url_refuses_to_guess_from_a_malformed_accession():
    """A plausible-looking 404 is worse than no link at all."""
    assert edgar_filing_url("0000320193", "not-an-accession") is None
    assert edgar_filing_url(None, "0001193125-26-226661") is None
