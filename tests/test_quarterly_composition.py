"""A stock-only percentage is arithmetically right and descriptively wrong when a
manager's book moves between stock and options.

Situational Awareness LP filed its Q2 on 2026-08-14 holding $3.9B of stock against
$9.8B of option notional the quarter before, then $20.2B of stock against almost no
options. Reading the stock line alone calls that 423% growth. The disclosed book went
from $13.7B to $20.2B. Elliott is the mirror case and the more dangerous one: its
stock line fell 10.3% while its disclosed book grew, so the unqualified sentence
described a manager adding as one retreating.
"""

from datetime import date

from whale_agent.ingestion.quarterly_filings import FilingChange, significance_pct


def _change(*, stock_now, stock_prior, opt_now, opt_prior, changes=None):
    return FilingChange(
        manager_cik="0002045724",
        manager_name="Test Manager",
        report_period=date(2026, 6, 30),
        accession="0001193125-26-000001",
        source_url="https://example.invalid/now",
        filed_date=date(2026, 8, 14),
        current_holdings=[],
        current_total_value_usd=stock_now,
        current_position_count=1,
        has_prior=True,
        prior_report_period=date(2026, 3, 31),
        prior_total_value_usd=stock_prior,
        prior_position_count=1,
        current_option_notional_usd=opt_now,
        prior_option_notional_usd=opt_prior,
        changes=changes,
    )


def test_book_turning_from_options_into_stock_is_flagged():
    fc = _change(
        stock_now=20_169_035_068,
        stock_prior=3_855_771_552,
        opt_now=73_257_160,
        opt_prior=9_820_886_025,
    )
    assert fc.composition_shifted
    # The disclosed book grew by about half, not by four hundred percent.
    assert fc.prior_disclosed_total_usd == 13_676_657_577
    assert fc.current_disclosed_total_usd == 20_242_292_228


def test_stock_line_falling_while_the_disclosed_book_grows_is_flagged():
    fc = _change(
        stock_now=14_292_858_303,
        stock_prior=15_934_215_348,
        opt_now=8_374_000_000,
        opt_prior=4_180_000_000,
    )
    assert fc.composition_shifted
    assert fc.current_disclosed_total_usd > (fc.prior_disclosed_total_usd or 0)


def test_ordinary_quarter_drift_is_not_flagged():
    fc = _change(
        stock_now=17_083_392_470,
        stock_prior=15_342_483_843,
        opt_now=4_600_000_000,
        opt_prior=2_200_000_000,
    )
    # Option share moves from 12.5% to 21.2%, under the OPTION_SHARE_SHIFT_THRESHOLD_PTS.
    assert not fc.composition_shifted


def test_ranking_sizes_a_move_against_the_whole_disclosed_book():
    """A manager carrying a stable options sleeve still gets ranked on its largest
    single move, but sized against everything it disclosed rather than the stock line
    alone, so the sleeve cannot inflate the score."""

    class _Changes:
        opened = [type("H", (), {"value_usd": 1_000_000_000})()]
        closed: list = []
        increased: list = []
        decreased: list = []

    fc = _change(
        stock_now=8_000_000_000,
        stock_prior=7_800_000_000,
        opt_now=2_000_000_000,
        opt_prior=1_950_000_000,
        changes=_Changes(),
    )
    assert not fc.composition_shifted
    # 1B against the 10B disclosed book, not against the 8B stock line.
    assert round(significance_pct(fc), 1) == 10.0
