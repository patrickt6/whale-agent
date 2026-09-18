"""The $1.6 quadrillion case, pinned at every layer that can stop it.

One observed vendor row is the whole subject of this file:

    MetLife / FINS   shares=40,000,000   price=40,000,000   ->   $1.6 quadrillion

It reached a reader-facing table once, and it did so while every check that could have
caught it was present in the codebase but optional at the call site. So each layer gets
its own test here rather than one end-to-end test: an end-to-end pass proves the chain
works today, and these prove that no single link is load-bearing on its own.

Layers, in the order the value travels:

    1. vendor adapter    the price field is rejected at parse time
    2. valuation         the multiplication is quarantined, not performed into a figure
    3. storage read      the row does not come back out of a screened read
    4. delivery          the rendered body carrying the figure is not sent
"""

from __future__ import annotations

from datetime import date

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.delivery.email import (
    EGRESS_CEILING_USD,
    figures_in,
    find_egress_violations,
    send_digest_email,
)
from whale_agent.enrichment.valuation import Quarantined, Valuation, value_event
from whale_agent.ingestion.us_sec import UsSecForm4Adapter
from whale_agent.ingestion.vendors.fmp import FmpInsiderAdapter
from whale_agent.models.enums import PriceSource
from whale_agent.storage.db import Store

# The live row, as FMP sent it.
FMP_ROW = {
    "symbol": "MET",
    "companyName": "MetLife Inc",
    "reportingName": "FINS",
    "reportingCik": "0000000001",
    "companyCik": "0001099219",
    "transactionType": "P-Purchase",
    "transactionDate": "2026-07-20",
    "filingDate": "2026-07-21",
    "securitiesTransacted": 40_000_000,
    "price": 40_000_000,
}


def quadrillion_event():
    """An event already carrying the bad figure, for the layers downstream of parsing."""
    return make_event(
        issuer_name="MetLife Inc",
        filer_name="FINS",
        ticker="MET",
        share_count=40_000_000.0,
        price_used=40_000_000.0,
        price_source=PriceSource.FILING_STATED,
        usd_value=1_600_000_000_000_000.0,
    )


# -- layer 1: the vendor adapter -------------------------------------------------
def test_fmp_adapter_rejects_the_quadrillion_price_at_parse_time():
    """A 40,000,000 per-share price is a transaction total, and is not carried forward."""
    event = FmpInsiderAdapter(Settings()).normalize(FMP_ROW)

    # Unknown, never zero: the field we could not believe is absent, not falsified.
    assert event.price_used is None
    assert event.price_source == PriceSource.NOT_PRICED
    # The share count was never in doubt and is kept.
    assert event.share_count == 40_000_000.0
    # The rejection is recorded on the row rather than dropped silently.
    reasons = event.raw_payload["_vendor_rejections"]
    assert any("price" in r for r in reasons)


def test_us_sec_adapter_rejects_the_quadrillion_price_at_parse_time():
    """The same guard on the EDGAR path, which parses its own numbers from XML."""
    event = UsSecForm4Adapter().normalize(
        {
            "issuer_name": "MetLife Inc",
            "filer_name": "FINS",
            "code": "P",
            "acquired_disposed": "A",
            "shares": 40_000_000.0,
            "price": 40_000_000.0,
            "transaction_date": "2026-07-20",
        }
    )
    assert event.price_used is None
    assert event.price_source == PriceSource.NOT_PRICED
    assert event.raw_payload["_vendor_rejections"]


def test_a_real_per_share_price_survives_the_vendor_guard():
    """The true negative that keeps the guard honest: a normal row is untouched."""
    row = {**FMP_ROW, "price": 71.42}
    event = FmpInsiderAdapter(Settings()).normalize(row)
    assert event.price_used == 71.42
    assert event.price_source == PriceSource.FILING_STATED
    assert "_vendor_rejections" not in event.raw_payload


# -- layer 2: valuation ----------------------------------------------------------
def test_valuation_quarantines_the_quadrillion_multiplication():
    """shares x price is not performed into a figure the rest of the pipeline can use."""
    event = make_event(
        issuer_name="MetLife Inc",
        filer_name="FINS",
        share_count=40_000_000.0,
        price_used=40_000_000.0,
        price_source=PriceSource.FILING_STATED,
        usd_value=None,
    )
    result = value_event(event)

    assert isinstance(result, Quarantined)
    assert not isinstance(result, Valuation)
    # Unknown is None and never zero. A quarantined value is not a zero value.
    assert event.usd_value is None
    assert event.effective_usd is None
    # The price band catches this row before the multiplication is even reached, which
    # is the cheaper of the two refusals and the one that names the actual defect.
    assert "transaction total" in result.reason
    assert result.inputs["share_count"] == 40_000_000.0


def test_valuation_quarantines_a_product_past_the_ceiling():
    """The second refusal: a believable price, an unbelievable product.

    The price band cannot catch a bad share count, so the multiplication is checked on
    its own terms as well. Neither guard is load-bearing alone.
    """
    event = make_event(share_count=4e13, price_used=100.0, usd_value=None)
    result = value_event(event)

    assert isinstance(result, Quarantined)
    assert "ceiling" in result.reason
    assert event.usd_value is None


def test_valuation_still_values_an_ordinary_filing():
    """The true negative: a believable multiplication returns a Valuation and lands."""
    event = make_event(share_count=100_000.0, price_used=71.42, usd_value=None)
    result = value_event(event)

    assert isinstance(result, Valuation)
    assert event.usd_value == 100_000.0 * 71.42
    assert result.usd == event.usd_value


# -- layer 3: the storage read ---------------------------------------------------
def test_screened_storage_read_does_not_return_the_quadrillion_row():
    """A reader that forgets to screen is the mechanism by which this shipped once."""
    store = Store(":memory:")
    try:
        bad = quadrillion_event()
        good = make_event(
            issuer_name="TestCo", filer_name="Jane Example", usd_value=12_400_000.0
        )
        store.upsert_many([bad, good])

        kept = store.events_since(date(2020, 1, 1))
        assert [e.issuer_name for e in kept] == ["TestCo"]

        # The row is excluded, not deleted: a silent drop is indistinguishable from a
        # source outage, and the escape hatch has to be typed out to be used.
        raw = store.events_since_UNSCREENED(date(2020, 1, 1))
        assert {e.issuer_name for e in raw} == {"TestCo", "MetLife Inc"}
    finally:
        store.close()


# -- layer 4: delivery -----------------------------------------------------------
def test_egress_gate_blocks_a_body_carrying_the_quadrillion_figure():
    """The last line, and the one no future call site can route around."""
    settings = Settings(
        email_provider="smtp",
        email_to="me@example.com",
        smtp_username="bot@gmail.com",
        smtp_password="app-password",
    )
    digest = "WHALE DIGEST 2026-07-25\n1. FINS bought $1600000.0B in MetLife Inc\n"
    result = send_digest_email(digest, settings)

    assert result.ok is False
    assert "egress blocked" in result.detail
    assert "1600000.0B" in result.detail


def test_egress_gate_blocks_on_the_events_behind_the_render():
    """Even a body whose text looks clean is stopped by the rows it was built from."""
    violations = find_egress_violations(
        "WHALE DIGEST 2026-07-25\nFINS bought a lot of MetLife Inc\n",
        events=[quadrillion_event()],
    )
    assert violations
    assert any("MetLife" in v for v in violations)


def test_egress_gate_lets_an_ordinary_digest_through():
    """The true negative: the gate must not become a reason nothing ever sends."""
    digest = "WHALE DIGEST 2026-07-25\n1. Jane Example bought $12.4M in TestCo [https://example.com/000186332826000002]"
    assert find_egress_violations(digest, events=[make_event(usd_value=12_400_000.0)]) == []


# -- the quarantine policy, end to end -------------------------------------------
def test_the_bad_row_is_dropped_the_rest_is_sent_and_the_reader_is_told():
    """The agreed policy, in one test.

    Dropping the row rather than holding the digest matches the standing product rule
    that a failing source becomes a coverage note and never an exception. The note is
    what makes it a drop rather than a silent disappearance, and the digest still has to
    clear the provenance gate with the note in it.
    """
    from whale_agent.jobs.pipeline import run_daily_digest

    bad = make_event(
        issuer_name="MetLife Inc",
        filer_name="FINS",
        share_count=40_000_000.0,
        price_used=40_000_000.0,
        price_source=PriceSource.FILING_STATED,
        usd_value=None,
    )
    good = make_event(
        issuer_name="TestCo",
        filer_name="Jane Example",
        share_count=100_000.0,
        price_used=71.42,
        usd_value=None,
    )
    store = Store(":memory:")
    try:
        digest, _ = run_daily_digest(
            store,
            Settings(),
            on=date(2026, 7, 25),
            events=[bad, good],
            use_llm=False,
            deliver=False,
        )
    finally:
        store.close()

    assert "TestCo" in digest  # the rest of the digest still went out
    assert "MetLife" not in digest  # the offending row did not
    assert "1 filing withheld" in digest  # and the reader was told so
    assert "quadrillion" not in digest.lower()
    # No withheld row reaches storage as a figure either.
    assert bad.usd_value is None and bad.effective_usd is None


def test_figures_in_reads_every_rendered_scale():
    assert figures_in("$12.4M and $250B and $1600000.0B and $900") == [
        ("$12.4M", 12_400_000.0),
        ("$250B", 250_000_000_000.0),
        ("$1600000.0B", 1.6e15),
        ("$900", 900.0),
    ]
    # The ceiling itself is allowed; only figures above it are not.
    assert find_egress_violations(f"${EGRESS_CEILING_USD / 1e9:.0f}B") == []
