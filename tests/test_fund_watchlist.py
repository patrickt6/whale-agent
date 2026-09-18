"""Tracked-manager ingestion and rendering. No network: every test uses a fixture.

The 13F information table is the only place in this system where a dollar figure arrives
as raw XML from an arbitrary filer, so the parser is tested against the shapes filers
actually produce -- namespaced tags, missing optional fields, and junk in numeric fields.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from whale_agent.ingestion.fund_watchlist import (
    WATCHLIST,
    FundSnapshot,
    Holding,
    parse_infotable,
)
from whale_agent.summarization.tracked_funds import render_tracked_funds

PLAIN = """
<informationTable>
  <infoTable>
    <nameOfIssuer>NVIDIA CORPORATION</nameOfIssuer>
    <value>1568257120</value>
    <shrsOrPrnAmt><sshPrnamt>8992300</sshPrnamt></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>ORACLE CORP</nameOfIssuer>
    <value>1072873230</value>
    <shrsOrPrnAmt><sshPrnamt>7293000</sshPrnamt></shrsOrPrnAmt>
  </infoTable>
</informationTable>
"""

NAMESPACED = """
<ns1:informationTable>
  <ns1:infoTable>
    <ns1:nameOfIssuer>BROADCOM INC</ns1:nameOfIssuer>
    <ns1:value>1006247961</ns1:value>
    <ns1:shrsOrPrnAmt><ns1:sshPrnamt>3251100</ns1:sshPrnamt></ns1:shrsOrPrnAmt>
  </ns1:infoTable>
</ns1:informationTable>
"""


def test_parses_a_plain_information_table():
    holdings = parse_infotable(PLAIN)
    assert [h.issuer for h in holdings] == ["NVIDIA CORPORATION", "ORACLE CORP"]
    assert holdings[0].value_usd == 1_568_257_120
    assert holdings[0].shares == 8_992_300


def test_parses_a_namespaced_information_table():
    """Filers vary the prefix freely; a strict parser would reject real filings."""
    holdings = parse_infotable(NAMESPACED)
    assert len(holdings) == 1
    assert holdings[0].issuer == "BROADCOM INC"
    assert holdings[0].value_usd == 1_006_247_961


def test_a_row_without_a_value_is_dropped_rather_than_guessed():
    xml = "<infoTable><nameOfIssuer>MYSTERY CO</nameOfIssuer></infoTable>"
    assert parse_infotable(xml) == []


def test_a_non_numeric_value_is_dropped_rather_than_coerced():
    xml = (
        "<infoTable><nameOfIssuer>JUNK CO</nameOfIssuer>"
        "<value>not-a-number</value></infoTable>"
    )
    assert parse_infotable(xml) == []


def test_missing_share_count_is_allowed_but_value_is_kept():
    xml = "<infoTable><nameOfIssuer>A CO</nameOfIssuer><value>500</value></infoTable>"
    holdings = parse_infotable(xml)
    assert holdings[0].shares is None
    assert holdings[0].value_usd == 500


def test_filing_index_url_is_the_human_readable_page():
    """Verified against EDGAR: this exact URL renders form name, filer, issuer, dates."""
    from whale_agent.ingestion.fund_watchlist import filing_index_url

    assert filing_index_url("1067983", "0001193125-26-333151") == (
        "https://www.sec.gov/Archives/edgar/data/1067983/000119312526333151/"
        "0001193125-26-333151-index.htm"
    )


def test_filing_index_url_is_empty_without_an_accession():
    from whale_agent.ingestion.fund_watchlist import filing_index_url

    assert filing_index_url("1067983", "") == ""


def test_every_watchlist_cik_is_ten_digits():
    """A malformed CIK silently reports a different manager's portfolio."""
    for name, cik in WATCHLIST.items():
        assert len(cik) == 10 and cik.isdigit(), f"{name} has a malformed CIK: {cik}"


def test_situational_awareness_is_tracked():
    assert WATCHLIST["Situational Awareness LP"] == "0002045724"


ON = date(2026, 8, 3)


def _snap(**kw):
    base = dict(
        name="Situational Awareness LP",
        cik="0002045724",
        latest_13f_filed=date(2026, 5, 18),
        # Stock only: the same filing's $8.46B of puts and $1.36B of calls are the
        # put_/call_ fields, never this one. See FundSnapshot.put_notional_usd.
        total_value_usd=3_855_771_552,
        position_count=26,
        top_holdings=[Holding("BLOOM ENERGY CORP", 878_707_930, 6_485_408)],
        put_notional_usd=8_459_056_999,
        call_notional_usd=1_361_829_026,
        put_name_count=11,
        call_name_count=5,
        latest_fast_form="SCHEDULE 13G",
        latest_fast_date=date(2026, 6, 29),
    )
    base.update(kw)
    return FundSnapshot(**base)


def test_render_states_the_portfolio_and_its_as_of_date():
    lines = render_tracked_funds([_snap()], ON)
    body = "\n".join(lines)
    assert "Situational Awareness LP" in body
    assert "$3.9B in stock" in body
    assert "26 positions" in body


def test_render_dates_the_snapshot_not_only_the_filing():
    """'reported 2026-05-15' implied recency the data does not have."""
    from datetime import date as _dt

    snap = _snap(period_end=_dt(2026, 3, 31), latest_13f_filed=_dt(2026, 5, 18))
    body = "\n".join(render_tracked_funds([snap], ON))
    assert "as of 2026-03-31" in body


def test_no_usable_managers_renders_nothing_rather_than_an_empty_heading():
    assert render_tracked_funds([], ON) == []
    only_failed = [FundSnapshot(name="Broken", cik="0000000001", error="boom")]
    assert render_tracked_funds(only_failed, ON) == []


# --- Defects caught by rendering the real email before sending it --------------------

from whale_agent.ingestion.fund_watchlist import (  # noqa: E402
    THIRTEEN_F_FLOOR_USD,
    dedupe_joint_filings,
)


def test_one_issuer_filed_on_several_rows_is_one_holding():
    """Berkshire's table lists APPLE INC twice; printing it twice reads as a bug."""
    xml = """
    <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><value>20500000000</value>
      <shrsOrPrnAmt><sshPrnamt>100</sshPrnamt></shrsOrPrnAmt></infoTable>
    <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><value>15600000000</value>
      <shrsOrPrnAmt><sshPrnamt>50</sshPrnamt></shrsOrPrnAmt></infoTable>
    """
    holdings = parse_infotable(xml)
    assert len(holdings) == 1
    assert holdings[0].value_usd == 36_100_000_000
    assert holdings[0].shares == 150


def test_escaped_issuer_names_are_unescaped():
    xml = (
        "<infoTable><nameOfIssuer>STATE STR SPDR S&amp;P 500 ETF T</nameOfIssuer>"
        "<value>2800000000</value></infoTable>"
    )
    assert parse_infotable(xml)[0].issuer == "STATE STR SPDR S&P 500 ETF T"


def test_a_table_below_the_filing_floor_is_read_as_thousands():
    """Duquesne reported $3M across 70 positions. 13F is not required below $100M,
    so a total under the floor means the units were thousands, not that the fund is tiny."""
    body = "\n".join(
        render_tracked_funds(
            [
                _snap(
                    name="Duquesne Family Office",
                    total_value_usd=3_000_000_000,
                    top_holdings=[Holding("Natera Inc", 612_691_000, None)],
                    values_scaled_from_thousands=True,
                )
            ],
            ON,
        )
    )
    assert "$3.0B" in body
    assert "reported in thousands" in body


def test_the_floor_is_the_statutory_filing_threshold():
    assert THIRTEEN_F_FLOOR_USD == 100_000_000


def test_related_entities_filing_one_joint_13f_are_not_counted_twice():
    """Two CIKs, one filing, identical tables -- printing both doubles the money."""
    a = _snap(name="Situational Awareness LP")
    b = _snap(name="Situational Awareness Partners LP", cik="0002038540")
    assert [s.name for s in dedupe_joint_filings([a, b])] == ["Situational Awareness LP"]


def test_genuinely_different_managers_are_both_kept():
    a = _snap(name="Fund A")
    b = _snap(name="Fund B", cik="0000000002", total_value_usd=999, position_count=3)
    assert len(dedupe_joint_filings([a, b])) == 2


def test_the_html_email_carries_the_tracked_section_too():
    """The email sends HTML; a section only in the plain-text part reaches nobody."""
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        tracked_funds=[_snap()],
    )
    assert "Tracked managers" in html
    assert "Situational Awareness LP" in html
    assert "$3.9B" in html
    # Typeset, not a slab of preformatted text dumped from the plain-text renderer.
    assert "white-space:pre" not in html
    assert "<table" in html


def test_the_html_omits_the_section_entirely_when_there_is_nothing_to_show():
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        tracked_funds=[],
    )
    assert "Tracked managers" not in html


# --- Quarter activity: what the manager DID, not just what it holds -------------------

from whale_agent.ingestion.fund_watchlist import QuarterActivity, parse_activity  # noqa: E402

FMP_ROW = {
    "date": "2026-03-31",
    "cik": "0002045724",
    "investorName": "Situational Awareness LP",
    "portfolioSize": 42,
    "securitiesAdded": 19,
    "securitiesRemoved": 10,
    "marketValue": 13676657577,
    "previousMarketValue": 5516758345,
    "performancePercentage": 18.4285,
    "performanceRelativeToSP500Percentage": 23.0587,
}


def test_activity_reads_the_filing_derived_fields():
    act = parse_activity([FMP_ROW])
    assert act.portfolio_size == 42
    assert act.added == 19
    assert act.removed == 10
    assert act.market_value == 13_676_657_577
    assert round(act.change_pct) == 148


def test_activity_never_exposes_vendor_computed_performance():
    """The product's guarantee is that figures resolve to filings. FMP's own return and
    S&P-relative numbers do not, so they must not be reachable from the record at all."""
    act = parse_activity([FMP_ROW])
    fields = set(QuarterActivity.__dataclass_fields__)
    assert not any("performance" in f.lower() for f in fields)
    assert not hasattr(act, "performancePercentage")


def test_an_unrecognised_shape_returns_none_rather_than_zeroes():
    """'added 0, exited 0' is a claim about the manager. We would be inventing it."""
    assert parse_activity([{"date": "2026-03-31"}]) is None
    assert parse_activity([]) is None


def test_a_first_ever_filing_has_no_percentage_change():
    act = parse_activity([{**FMP_ROW, "previousMarketValue": 0}])
    assert act.change_pct is None


def test_render_survives_a_manager_with_no_activity_record():
    body = "\n".join(render_tracked_funds([_snap(activity=None)], ON))
    assert "Situational Awareness LP" in body
    assert "over the quarter" not in body


# --- What a manager filed this week, which is the part that is news -------------------

from datetime import date as _d  # noqa: E402

from whale_agent.ingestion.fund_watchlist import (  # noqa: E402
    FastFiling,
    fast_filings_since,
)
from whale_agent.summarization.tracked_funds import render_fund_moves  # noqa: E402

SUBMISSIONS = {
    "form": ["4", "SCHEDULE 13G", "3", "SCHEDULE 13G", "13F-HR", "8-K"],
    "filingDate": [
        "2026-07-02",
        "2026-06-29",
        "2026-06-29",
        "2026-05-27",
        "2026-05-18",
        "2026-07-01",
    ],
    "accessionNumber": [
        "0002045724-26-000012",
        "0002045724-26-000011",
        "0002045724-26-000010",
        "0002045724-26-000009",
        "0002045724-26-000008",
        "0002045724-26-000007",
    ],
}


def test_only_fast_forms_count_as_a_move():
    """13F is the quarterly snapshot, not news. 8-K is not a stake disclosure."""
    got = fast_filings_since(SUBMISSIONS, _d(2026, 1, 1), cik="0002045724")
    assert [f.form for f in got] == ["4", "SCHEDULE 13G", "3", "SCHEDULE 13G"]


def test_filings_before_the_window_are_excluded():
    got = fast_filings_since(SUBMISSIONS, _d(2026, 6, 1))
    assert [f.filed.isoformat() for f in got] == ["2026-07-02", "2026-06-29", "2026-06-29"]


def test_filings_are_newest_first():
    got = fast_filings_since(SUBMISSIONS, _d(2026, 1, 1))
    assert got == sorted(got, key=lambda f: f.filed, reverse=True)


def test_a_filing_carries_a_url_a_reader_can_open():
    """The EDGAR filing index page, not the bare accession directory.

    The directory renders as an Apache file listing with no indication of what the
    filing is; the index page (`{accession}-index.htm`) is the page EDGAR itself
    renders, with form name, filer, issuer and dates.
    """
    got = fast_filings_since(SUBMISSIONS, _d(2026, 7, 1), cik="0002045724")
    assert got[0].url == (
        "https://www.sec.gov/Archives/edgar/data/2045724/000204572426000012/"
        "0002045724-26-000012-index.htm"
    )


def test_a_filing_without_an_accession_has_no_url():
    assert FastFiling(form="4", filed=_d(2026, 7, 2)).url == ""


def test_a_filing_without_a_cik_has_no_url():
    assert FastFiling(form="4", filed=_d(2026, 7, 2), accession="x").url == ""


def test_moves_section_reports_each_filing_with_its_age():
    snap = _snap(recent_filings=fast_filings_since(SUBMISSIONS, _d(2026, 6, 1), "0002045724"))
    body = "\n".join(render_fund_moves([snap], _d(2026, 8, 3), _d(2026, 6, 1)))
    assert "Situational Awareness LP" in body
    assert "SCHEDULE 13G filed 2026-06-29" in body
    assert "32 days ago" in body
    assert "sec.gov/Archives" in body


def test_a_quiet_week_says_so_rather_than_vanishing():
    """Silence and absence must not look identical."""
    body = "\n".join(
        render_fund_moves([_snap(recent_filings=[])], _d(2026, 8, 3), _d(2026, 7, 27))
    )
    assert "No tracked manager filed a stake disclosure" in body


def test_the_html_carries_the_moves_section_too():
    """The email sends HTML; a section only in the text part reaches nobody."""
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    snap = _snap(recent_filings=fast_filings_since(SUBMISSIONS, _d(2026, 6, 1), "0002045724"))
    html = render_weekly_html(
        on=_d(2026, 8, 3),
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        tracked_funds=[snap],
    )
    assert "What the tracked managers filed" in html
    assert "SCHEDULE 13G" in html
    assert "sec.gov/Archives" in html


def test_the_html_moves_section_states_a_quiet_week():
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    html = render_weekly_html(
        on=_d(2026, 8, 3),
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        tracked_funds=[_snap(recent_filings=[])],
    )
    assert "No tracked manager filed a stake disclosure" in html


# --- Which names, not how many: the diff between two quarters ------------------------

from whale_agent.ingestion.fund_watchlist import (  # noqa: E402
    PositionChanges,
    diff_holdings,
)

PRIOR = [
    Holding("NVIDIA CORPORATION", 1_000_000_000, None),
    Holding("TESLA INC", 500_000_000, None),
    Holding("ORACLE CORP", 800_000_000, None),
    Holding("PFIZER INC", 200_000_000, None),
]
CURRENT = [
    Holding("NVIDIA CORPORATION", 1_600_000_000, None),  # added to
    Holding("ORACLE CORP", 300_000_000, None),  # trimmed
    Holding("BROADCOM INC", 900_000_000, None),  # opened
]


def test_a_new_name_is_reported_as_opened():
    ch = diff_holdings(CURRENT, PRIOR)
    assert [h.issuer for h in ch.opened] == ["BROADCOM INC"]


def test_a_vanished_name_is_reported_as_closed():
    ch = diff_holdings(CURRENT, PRIOR)
    assert sorted(h.issuer for h in ch.closed) == ["PFIZER INC", "TESLA INC"]


def test_a_bigger_position_is_reported_as_added_to_with_the_delta():
    ch = diff_holdings(CURRENT, PRIOR)
    assert ch.increased == [("NVIDIA CORPORATION", 600_000_000)]


def test_a_smaller_position_is_reported_as_trimmed():
    ch = diff_holdings(CURRENT, PRIOR)
    assert ch.decreased == [("ORACLE CORP", -500_000_000)]


def test_an_unchanged_book_produces_no_changes():
    assert not diff_holdings(PRIOR, PRIOR)


def test_changes_are_falsy_when_empty_so_the_renderer_can_skip_them():
    assert not PositionChanges()


def test_a_first_ever_filing_has_nothing_to_diff_against():
    snap = _snap(changes=None)
    body = "\n".join(render_tracked_funds([snap], ON))
    assert "opened:" not in body


def test_a_respelled_issuer_is_not_reported_as_opened_and_closed():
    """Berkshire's table renamed Chevron between quarters. Nothing moved."""
    prior = [Holding("Chevron Corp New", 17_000_000_000, None, cusip="166764100")]
    current = [Holding("Chevron Corporation", 17_500_000_000, None, cusip="166764100")]
    ch = diff_holdings(current, prior)
    assert ch.opened == []
    assert ch.closed == []
    assert ch.increased == [("Chevron Corporation", 500_000_000)]


def test_cusip_is_parsed_from_the_information_table():
    xml = (
        "<infoTable><nameOfIssuer>CHEVRON CORP</nameOfIssuer>"
        "<cusip>166764100</cusip><value>17500000000</value></infoTable>"
    )
    assert parse_infotable(xml)[0].cusip == "166764100"


def test_share_classes_of_one_issuer_merge_on_cusip():
    xml = (
        "<infoTable><nameOfIssuer>ALPHABET INC</nameOfIssuer><cusip>02079K305</cusip>"
        "<value>100</value></infoTable>"
        "<infoTable><nameOfIssuer>ALPHABET INC CL C</nameOfIssuer><cusip>02079K305</cusip>"
        "<value>150</value></infoTable>"
    )
    out = parse_infotable(xml)
    assert len(out) == 1 and out[0].value_usd == 250


def test_without_a_cusip_the_name_is_still_the_key():
    assert Holding("A CO", 1, None).key == "A CO"


# --- What the filing actually says, not merely that it exists ------------------------

from whale_agent.ingestion.fund_watchlist import (  # noqa: E402
    StakeDetail,
    parse_stake_filing,
)

DUQUESNE_13G = """<?xml version="1.0"?><edgarSubmission>
<submissionType>SCHEDULE 13G</submissionType>
<issuerCik>0001978954</issuerCik>
<issuerName>BBB Foods Inc</issuerName>
<issuerCusipNumber>G0896C103</issuerCusipNumber>
<reportingPersonName>Duquesne Family Office LLC</reportingPersonName>
<sharedVotingPower>2901733</sharedVotingPower>
<reportingPersonBeneficiallyOwnedAggregateNumberOfShares>2901733</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
<classPercent>3.7</classPercent>
<reportingPersonName>Stanley Druckenmiller</reportingPersonName>
<reportingPersonBeneficiallyOwnedAggregateNumberOfShares>2901733</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
<classPercent>3.7</classPercent>
</edgarSubmission>"""


def test_the_stake_is_read_out_of_the_filing():
    d = parse_stake_filing(DUQUESNE_13G)
    assert d.issuer == "BBB Foods Inc"
    assert d.issuer_cusip == "G0896C103"
    assert d.percent == 3.7
    assert d.shares == 2_901_733


def test_both_reporting_persons_are_named_without_doubling_the_stake():
    """A fund and the person running it report the same shares, not twice as many."""
    d = parse_stake_filing(DUQUESNE_13G)
    assert d.holders == ["Duquesne Family Office LLC", "Stanley Druckenmiller"]
    assert d.shares == 2_901_733


def test_the_stake_reads_the_way_someone_would_say_it():
    d = parse_stake_filing(DUQUESNE_13G)
    assert d.describe() == "3.7% / 2,901,733 shares of BBB Foods Inc"


def test_a_document_without_stake_fields_degrades_to_the_issuer():
    d = parse_stake_filing("<x><issuerName>Some Co</issuerName></x>")
    assert d.percent is None and d.shares is None
    assert d.describe() == "Some Co"


def test_junk_in_a_numeric_field_is_dropped_rather_than_coerced():
    d = parse_stake_filing(
        "<x><issuerName>Co</issuerName><classPercent>n/a</classPercent></x>"
    )
    assert d.percent is None


def test_the_moves_section_reports_the_stake_not_just_the_form():
    """The complaint: 'Duquesne filed a 13G' is not what people actually discuss."""
    from datetime import date as _dd

    filing = FastFiling(
        form="SCHEDULE 13G",
        filed=_dd(2026, 7, 29),
        accession="0000899140-26-000744",
        detail=parse_stake_filing(DUQUESNE_13G),
    )
    body = "\n".join(
        render_fund_moves([_snap(recent_filings=[filing])], _dd(2026, 8, 3), _dd(2026, 7, 27))
    )
    assert "3.7%" in body
    assert "BBB Foods Inc" in body


SCHEDULE_13D = """<?xml version="1.0"?><edgarSubmission>
<submissionType>SCHEDULE 13D</submissionType>
<issuerName>KUSTOM ENTERTAINMENT, INC.</issuerName>
<issuerCusipNumber>25382T606</issuerCusipNumber>
<reportingPersonName>Martin Ryan Todd</reportingPersonName>
<aggregateAmountOwned>1250000</aggregateAmountOwned>
<percentOfClass>7.4</percentOfClass>
</edgarSubmission>"""


def test_a_13d_uses_different_field_names_and_is_still_read():
    """13G says classPercent, 13D says percentOfClass. Both are in the wild."""
    d = parse_stake_filing(SCHEDULE_13D)
    assert d.issuer == "KUSTOM ENTERTAINMENT, INC."
    assert d.percent == 7.4
    assert d.shares == 1_250_000


def test_a_zero_stake_is_treated_as_unparsed_not_as_a_real_figure():
    """A 13D/G exists because someone crossed 5%; 0.0 means the parse failed."""
    xml = (
        "<x><issuerName>Co</issuerName><percentOfClass>0.0</percentOfClass>"
        "<aggregateAmountOwned>0.00</aggregateAmountOwned></x>"
    )
    d = parse_stake_filing(xml)
    assert d.percent is None
    assert d.shares is None


def test_the_13g_shape_still_parses_after_adding_13d_support():
    d = parse_stake_filing(DUQUESNE_13G)
    assert d.percent == 3.7 and d.shares == 2_901_733


# --- The market, not just the watchlist ----------------------------------------------

from whale_agent.ingestion.fund_watchlist import MarketStake  # noqa: E402
from whale_agent.summarization.tracked_funds import render_market_stakes  # noqa: E402


def _stake(pct, holder, issuer, form="SC 13G"):
    return MarketStake(
        filed=_d(2026, 8, 3),
        form=form,
        holders=[holder],
        detail=StakeDetail(issuer=issuer, percent=pct, shares=1000),
        url="https://www.sec.gov/Archives/edgar/data/1/2/",
    )


def test_the_market_section_names_funds_nobody_was_tracking():
    """The point: Farallon is not on the watchlist and still took 9.9% of something."""
    stakes = [
        _stake(9.9, "Farallon Capital Management, L.L.C.", "AGIOS PHARMACEUTICALS"),
        _stake(6.0, "BAILLIE GIFFORD & CO", "Nu Holdings Ltd."),
    ]
    body = "\n".join(render_market_stakes(stakes, _d(2026, 8, 3), _d(2026, 7, 27)))
    assert "Farallon Capital" in body
    assert "AGIOS PHARMACEUTICALS" in body
    assert "9.9%" in body
    assert "sec.gov" in body, "every claim needs a filing behind it"


def test_stakes_are_ranked_by_size():
    stakes = [_stake(9.9, "Big", "A"), _stake(6.0, "Small", "B")]
    body = "\n".join(render_market_stakes(stakes, _d(2026, 8, 3), _d(2026, 7, 27)))
    assert body.index("Big") < body.index("Small")


def test_a_week_with_no_crossings_says_so():
    body = "\n".join(render_market_stakes([], _d(2026, 8, 3), _d(2026, 7, 27)))
    assert "Nobody crossed 5%" in body


def test_the_market_section_appears_in_the_html_with_links():
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    html = render_weekly_html(
        on=_d(2026, 8, 3),
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        market_stakes=[_stake(9.9, "Farallon", "AGIOS")],
    )
    assert "Big stakes disclosed this week" in html
    assert "Farallon" in html
    assert "sec.gov" in html


def test_the_roster_is_a_reference_row_not_a_report():
    """Quarterly detail was cut: the reader asked for what happened since, not before."""
    from datetime import date as _dt

    snap = _snap(
        period_end=_dt(2026, 3, 31),
        latest_13f_url="https://www.sec.gov/Archives/edgar/data/1/2/",
    )
    body = "\n".join(render_tracked_funds([snap], ON))
    assert "Situational Awareness LP" in body
    assert "as of 2026-03-31" in body
    assert "https://www.sec.gov/Archives/edgar/data/1/2/" in body
    # None of the quarterly detail survives here.
    for gone in ("opened:", "closed:", "added to:", "trimmed:", "largest:"):
        assert gone not in body, gone


def test_a_form4_trade_is_reported_with_its_date():
    """The only filing that carries a transaction date, and it was being ignored."""
    from datetime import date as _dt

    from whale_agent.ingestion.fund_watchlist import parse_form4

    xml = """<ownershipDocument>
      <issuerName>SharonAI Holdings Inc.</issuerName>
      <issuerTradingSymbol>SHAZ</issuerTradingSymbol>
      <nonDerivativeTransaction>
        <transactionDate><value>2026-06-30</value></transactionDate>
        <transactionCode>P</transactionCode>
        <transactionShares><value>3700000</value></transactionShares>
        <transactionPricePerShare><value>1.25</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </nonDerivativeTransaction>
    </ownershipDocument>"""
    trades = parse_form4(xml)
    assert len(trades) == 1
    d = trades[0].describe()
    assert "bought" in d
    assert "3,700,000 shares" in d
    assert "SharonAI Holdings Inc. (SHAZ)" in d
    assert "$1.25" in d
    assert "2026-06-30" in d
    assert trades[0].on == _dt(2026, 6, 30)


def test_an_unknown_transaction_code_is_shown_not_guessed():
    from whale_agent.ingestion.fund_watchlist import parse_form4

    xml = (
        "<ownershipDocument><issuerName>Co</issuerName>"
        "<nonDerivativeTransaction><transactionCode>Z</transactionCode>"
        "</nonDerivativeTransaction></ownershipDocument>"
    )
    assert "code Z" in parse_form4(xml)[0].describe()


def test_the_moves_section_shows_the_trade_not_just_the_form():
    from datetime import date as _dt

    from whale_agent.ingestion.fund_watchlist import parse_form4

    xml = """<ownershipDocument><issuerName>SharonAI Holdings Inc.</issuerName>
      <issuerTradingSymbol>SHAZ</issuerTradingSymbol>
      <nonDerivativeTransaction>
        <transactionDate><value>2026-06-30</value></transactionDate>
        <transactionCode>P</transactionCode>
        <transactionShares><value>3700000</value></transactionShares>
      </nonDerivativeTransaction></ownershipDocument>"""
    filing = FastFiling(
        form="4", filed=_dt(2026, 7, 2), accession="x", trades=parse_form4(xml)
    )
    body = "\n".join(
        render_fund_moves([_snap(recent_filings=[filing])], _dt(2026, 7, 3), _dt(2026, 6, 28))
    )
    assert "bought 3,700,000 shares of SharonAI Holdings Inc. (SHAZ)" in body
    assert "2026-06-30" in body


def test_the_resale_section_leads_with_the_intent_to_sell():
    from datetime import date as _dt

    from whale_agent.ingestion.resale_watch import ResaleRegistration
    from whale_agent.summarization.tracked_funds import render_resale_watch

    reg = ResaleRegistration(
        whale_name="Situational Awareness",
        issuer="SharonAI Holdings Inc.",
        symbol="SHAZ",
        form="S-1",
        filed=_dt(2026, 7, 31),
        shares_registered=7_274_842,
        shares_held=7_563_029,
        url="https://www.sec.gov/Archives/edgar/data/2068385/000149315226035629/",
    )
    body = "\n".join(render_resale_watch([reg], _dt(2026, 8, 3), _dt(2026, 7, 27)))
    assert "Situational Awareness" in body
    assert "7,274,842" in body
    assert "S-1" in body
    assert "sec.gov" in body
    assert "registering shares is the step before selling" in body.lower()


def test_a_week_with_no_resale_registrations_says_so():
    from datetime import date as _dt

    from whale_agent.summarization.tracked_funds import render_resale_watch

    body = "\n".join(render_resale_watch([], _dt(2026, 8, 3), _dt(2026, 7, 27)))
    assert "No tracked fund" in body


# -- discovery through SEC's own daily index -----------------------------------
#
# The vendor filing search caps each form type at 100 rows per request. Measured
# against the week of 2026-07-28, that surfaced 96 filings where EDGAR's daily index
# listed 2,246 (1,123 once the filer/subject double-listing is deduped). Anything that
# ranks "the biggest stakes this week" off the vendor feed is ranking a sample whose
# selection is the vendor's page limit, so discovery reads the index instead.

_IDX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Jul 31, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/
 
 
 
 
Form Type   Company Name                                                  CIK
      Date Filed  File Name
------------------------------------------------------------------------------
1-A              FJHL Inc.                              2135411     20260731    edgar/data/2135411/0001213900-26-084888.txt
SCHEDULE 13D     Evofem Biosciences, Inc.               1618835     20260731    edgar/data/1618835/0001213900-26-084889.txt
SCHEDULE 13D     HUB Cyber Security Ltd.                1877461     20260731    edgar/data/1877461/0001213900-26-084889.txt
SCHEDULE 13G     Paymentus Holdings Inc                  814133     20260731    edgar/data/814133/0000814133-26-000106.txt
SCHEDULE 13G/A   e.l.f. Beauty, Inc.                    1088875     20260731    edgar/data/1088875/0001088875-26-000053.txt
13F-HR           Berkshire Hathaway Inc                 1067983     20260731    edgar/data/1067983/0001067983-26-000010.txt
"""


def test_daily_index_keeps_only_stake_forms():
    from whale_agent.ingestion.fund_watchlist import parse_daily_index

    rows = parse_daily_index(_IDX)
    assert {r.form for r in rows} == {"SCHEDULE 13D", "SCHEDULE 13G", "SCHEDULE 13G/A"}
    assert all("13F" not in r.form for r in rows)


def test_daily_index_dedupes_the_filer_and_subject_listing():
    """EDGAR lists one 13D under both the subject issuer and the filer.

    Counting index rows therefore double counts. The accession number is the filing's
    identity, and the two rows for 0001213900-26-084889 must collapse to one.
    """
    from whale_agent.ingestion.fund_watchlist import parse_daily_index

    rows = parse_daily_index(_IDX)
    accessions = [r.accession for r in rows]
    assert len(accessions) == len(set(accessions))
    assert "0001213900-26-084889" in accessions


def test_daily_index_reads_cik_and_accession_for_the_document_url():
    from whale_agent.ingestion.fund_watchlist import parse_daily_index

    rows = parse_daily_index(_IDX)
    row = next(r for r in rows if r.accession == "0000814133-26-000106")
    assert row.cik == "814133"
    assert row.form == "SCHEDULE 13G"


def test_daily_index_survives_a_short_or_empty_file():
    """A day with no index published yet must read as no filings, not an exception."""
    from whale_agent.ingestion.fund_watchlist import parse_daily_index

    assert parse_daily_index("") == []
    assert parse_daily_index("Description: header only\n") == []


# -- who filed: institution or individual --------------------------------------
#
# The market-wide section mixes a founder reporting his own holding with a fund taking
# a position, and those read very differently. The filing itself answers it:
# `typeOfReportingPerson` carries SEC's classification codes, so the label is copied
# from the document rather than guessed from the name.

_STAKE_XML_FUND = """<?xml version="1.0"?>
<edgarSubmission>
  <issuerName>Liquidia Corporation</issuerName>
  <classPercent>9.4</classPercent>
  <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>8365038</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
  <reportingPersonName>Farallon Capital Management, L.L.C.</reportingPersonName>
  <typeOfReportingPerson>IA</typeOfReportingPerson>
  <typeOfReportingPerson>PN</typeOfReportingPerson>
</edgarSubmission>"""

_STAKE_XML_PERSON = """<?xml version="1.0"?>
<edgarSubmission>
  <issuerName>GALECTIN THERAPEUTICS INC</issuerName>
  <percentOfClass>49.3</percentOfClass>
  <aggregateAmountOwned>54673646</aggregateAmountOwned>
  <reportingPersonName>Richard E. Uihlein</reportingPersonName>
  <typeOfReportingPerson>IN</typeOfReportingPerson>
</edgarSubmission>"""


def test_stake_filing_reads_the_reporting_person_codes():
    from whale_agent.ingestion.fund_watchlist import parse_stake_filing

    assert parse_stake_filing(_STAKE_XML_FUND).person_types == ["IA", "PN"]
    assert parse_stake_filing(_STAKE_XML_PERSON).person_types == ["IN"]


def test_an_investment_adviser_reads_as_institutional():
    from whale_agent.ingestion.fund_watchlist import parse_stake_filing

    assert parse_stake_filing(_STAKE_XML_FUND).is_institutional is True


def test_a_natural_person_does_not_read_as_institutional():
    """The 49.3% individual holder must not be labelled a fund."""
    from whale_agent.ingestion.fund_watchlist import parse_stake_filing

    assert parse_stake_filing(_STAKE_XML_PERSON).is_institutional is False


def test_a_filing_with_no_person_code_is_unlabelled_not_guessed():
    """No code means unknown. Silence must not be read as either answer."""
    from whale_agent.ingestion.fund_watchlist import parse_stake_filing

    detail = parse_stake_filing(
        "<x><issuerName>Acme</issuerName><classPercent>7</classPercent></x>"
    )
    assert detail.person_types == []
    assert detail.is_institutional is None


# -- the market-wide section says who is a fund --------------------------------


def _typed_stake(holder, pct, codes, issuer="Acme Corp"):
    from datetime import date as _d

    from whale_agent.ingestion.fund_watchlist import MarketStake, StakeDetail

    return MarketStake(
        filed=_d(2026, 8, 3),
        form="SCHEDULE 13G",
        holders=[holder],
        detail=StakeDetail(
            issuer=issuer, percent=pct, shares=100, holders=[holder], person_types=codes
        ),
        url="https://example.invalid/f/",
    )


def test_market_stakes_marks_an_institution_and_leaves_a_person_alone():
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_market_stakes

    lines = render_market_stakes(
        [
            _typed_stake("Farallon Capital Management", 9.4, ["IA"]),
            _typed_stake("Richard E. Uihlein", 49.3, ["IN"]),
        ],
        _d(2026, 8, 4),
        _d(2026, 7, 28),
    )
    text = "\n".join(lines)
    # The fund leads; the natural person is filed under the second list, not labelled
    # as an institution anywhere.
    assert text.index("Farallon Capital Management") < text.index("Other filers")
    assert text.index("Other filers") < text.index("Richard E. Uihlein")


def test_market_stakes_does_not_label_a_filing_that_declares_nothing():
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_market_stakes

    text = "\n".join(
        render_market_stakes(
            [_typed_stake("Mystery Holder", 8.0, [])], _d(2026, 8, 4), _d(2026, 7, 28)
        )
    )
    assert "Mystery Holder" in text
    # It appears under the unclassified list, never presented as a fund.
    assert text.index("Other filers") < text.index("Mystery Holder")


def test_market_stakes_header_states_the_coverage_it_actually_has():
    """The old header claimed every 13D/G of the week while reading 25 of 1,123."""
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_market_stakes

    text = "\n".join(
        render_market_stakes(
            [_typed_stake("A Fund", 9.0, ["IA"])],
            _d(2026, 8, 4),
            _d(2026, 7, 28),
            examined=1123,
        )
    )
    assert "1,123" in text


# -- institutions and everyone else, told apart -------------------------------
#
# Ranking 1,149 filings purely by percentage puts control blocks on top: a founder with
# 100% of a shell, a sponsor holding its own BDC, a parent holding its subsidiary. Those
# are real filings and are not hidden, but they are a different event from a fund taking
# a position, so each gets its own short list.


def test_market_stakes_splits_institutions_from_everyone_else():
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_market_stakes

    text = "\n".join(
        render_market_stakes(
            [
                _typed_stake("Martin Ryan Todd", 100.0, ["IN"], issuer="KUSTOM"),
                _typed_stake("Farallon Capital Management", 9.4, ["IA"], issuer="Liquidia"),
            ],
            _d(2026, 8, 4),
            _d(2026, 7, 28),
            examined=1149,
        )
    )
    assert "Farallon Capital Management" in text and "Martin Ryan Todd" in text
    # The fund appears before the 100% individual despite the smaller stake.
    assert text.index("Farallon Capital Management") < text.index("Martin Ryan Todd")


def test_each_list_is_still_ranked_by_stake_size():
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_market_stakes

    text = "\n".join(
        render_market_stakes(
            [
                _typed_stake("Small Fund", 6.0, ["IA"], issuer="A"),
                _typed_stake("Big Fund", 30.0, ["IA"], issuer="B"),
            ],
            _d(2026, 8, 4),
            _d(2026, 7, 28),
        )
    )
    assert text.index("Big Fund") < text.index("Small Fund")


def test_an_all_institutional_week_does_not_print_an_empty_second_list():
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_market_stakes

    text = "\n".join(
        render_market_stakes(
            [_typed_stake("Only Fund", 9.0, ["IA"])], _d(2026, 8, 4), _d(2026, 7, 28)
        )
    )
    assert "Only Fund" in text
    assert "Other filers" not in text


def test_an_undeclared_filer_falls_in_with_everyone_else_not_the_funds():
    """No `typeOfReportingPerson` means unknown, and unknown is not an institution."""
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import render_market_stakes

    text = "\n".join(
        render_market_stakes(
            [_typed_stake("Mystery Holder", 40.0, []), _typed_stake("A Fund", 8.0, ["IA"])],
            _d(2026, 8, 4),
            _d(2026, 7, 28),
        )
    )
    assert "Other filers" in text
    assert text.index("A Fund") < text.index("Mystery Holder")


# --- Option rows: a put is not a holding ---------------------------------------------
#
# Pinned against the real Situational Awareness LP table for 2026-03-31 (accession
# 0002045724-26-000008), because the defect this covers shipped to a reader: 62% of the
# "$13.7B portfolio" printed for that fund was put notional, and the issuers of those
# puts were being carried as its largest holdings.

import pathlib  # noqa: E402

from whale_agent.summarization.tracked_funds import option_note  # noqa: E402

SALP_XML = (pathlib.Path(__file__).parent / "fixtures" / "salp_13f_2026q1.xml").read_text()


def _salp():
    return parse_infotable(SALP_XML)


def test_the_real_table_splits_into_put_long_and_call_exactly():
    holdings = _salp()
    by_type = {"": [], "PUT": [], "CALL": []}
    for h in holdings:
        by_type[h.option_type].append(h)
    assert len(holdings) == 42
    assert [len(by_type[t]) for t in ("PUT", "", "CALL")] == [11, 26, 5]
    assert sum(h.value_usd for h in by_type["PUT"]) == 8_459_056_999
    assert sum(h.value_usd for h in by_type[""]) == 3_855_771_552
    assert sum(h.value_usd for h in by_type["CALL"]) == 1_361_829_026
    assert sum(h.value_usd for h in holdings) == 13_676_657_577


def test_nvidias_1_5b_put_is_not_added_to_its_500k_of_stock():
    """The email printed $1,568,755,032 of NVIDIA, which is the two rows summed.

    The table holds $497,912 of the stock and a put on $1,568,257,120 of it. Merging
    them on CUSIP produced a number the filing does not contain, attached to the wrong
    kind of position.
    """
    nvda = sorted(
        (h for h in _salp() if h.issuer == "NVIDIA CORPORATION"),
        key=lambda h: h.value_usd,
    )
    assert [(h.option_type, h.value_usd) for h in nvda] == [
        ("", 497_912),
        ("PUT", 1_568_257_120),
    ]
    assert sum(h.value_usd for h in nvda) == 1_568_755_032  # what shipped


def test_a_put_and_a_call_on_one_cusip_stay_separate():
    """Micron's $583.7M put was being added to its call and printed as $1.01B held."""
    xml = """
    <infoTable><nameOfIssuer>MICRON TECHNOLOGY INC</nameOfIssuer><cusip>595112103</cusip>
      <value>583686168</value><shrsOrPrnAmt><sshPrnamt>100</sshPrnamt></shrsOrPrnAmt>
      <putCall>Put</putCall></infoTable>
    <infoTable><nameOfIssuer>MICRON TECHNOLOGY INC</nameOfIssuer><cusip>595112103</cusip>
      <value>428165578</value><shrsOrPrnAmt><sshPrnamt>70</sshPrnamt></shrsOrPrnAmt>
      <putCall>Call</putCall></infoTable>
    <infoTable><nameOfIssuer>MICRON TECHNOLOGY INC</nameOfIssuer><cusip>595112103</cusip>
      <value>11000000</value><shrsOrPrnAmt><sshPrnamt>2</sshPrnamt></shrsOrPrnAmt>
      </infoTable>
    """
    holdings = parse_infotable(xml)
    assert [(h.option_type, h.value_usd) for h in holdings] == [
        ("PUT", 583_686_168),
        ("CALL", 428_165_578),
        ("", 11_000_000),
    ]
    assert len({h.key for h in holdings}) == 3


def test_two_rows_of_the_same_option_type_still_merge():
    xml = """
    <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip>
      <value>100</value><shrsOrPrnAmt><sshPrnamt>1</sshPrnamt></shrsOrPrnAmt>
      <putCall>PUT</putCall></infoTable>
    <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip>
      <value>50</value><shrsOrPrnAmt><sshPrnamt>2</sshPrnamt></shrsOrPrnAmt>
      <putCall>PUT</putCall></infoTable>
    """
    holdings = parse_infotable(xml)
    assert len(holdings) == 1
    assert (holdings[0].value_usd, holdings[0].shares) == (150, 3)


def test_a_row_with_no_putcall_is_an_ordinary_long_position():
    assert parse_infotable(PLAIN)[0].option_type == ""
    assert parse_infotable(PLAIN)[0].is_option is False


def test_a_diff_never_compares_a_put_against_a_long():
    """Selling stock and buying a put on the same name is not 'unchanged'."""
    prior = [Holding("NVIDIA CORPORATION", 1_000, None, "67066G104")]
    current = [Holding("NVIDIA CORPORATION", 1_500, None, "67066G104", "PUT")]
    changes = diff_holdings(current, prior)
    assert [h.issuer for h in changes.closed] == ["NVIDIA CORPORATION"]
    assert [h.option_type for h in changes.opened] == ["PUT"]
    assert changes.increased == [] and changes.decreased == []


def _salp_snap(**kw):
    """The snapshot fetch_snapshot builds from the real table above."""
    holdings = sorted(_salp(), key=lambda h: h.value_usd, reverse=True)
    stock = [h for h in holdings if not h.is_option]
    base = dict(
        name="Situational Awareness LP",
        cik="0002045724",
        latest_13f_filed=date(2026, 5, 18),
        period_end=date(2026, 3, 31),
        total_value_usd=sum(h.value_usd for h in stock),
        position_count=len(stock),
        top_holdings=stock[:5],
        put_notional_usd=sum(h.value_usd for h in holdings if h.option_type == "PUT"),
        call_notional_usd=sum(h.value_usd for h in holdings if h.option_type == "CALL"),
        put_name_count=11,
        call_name_count=5,
        latest_13f_url="https://example.invalid/13f/",
    )
    base.update(kw)
    return FundSnapshot(**base)


def test_the_headline_figure_is_stock_and_never_the_put_notional():
    body = "\n".join(render_tracked_funds([_salp_snap()], ON))
    assert "$3.9B in stock" in body
    assert "26 positions" in body
    # The figure that shipped, which was 62% puts.
    assert "$13.7B" not in body


def test_the_render_names_no_put_issuer_as_a_holding():
    """Guards the property, not the current absence of a holdings list.

    The original form of this test asserted that put issuers were missing from a
    rendering that prints no issuer names at all, so it passed even when the five
    largest put lines were injected into `top_holdings`. It could not have caught a
    regression. This asserts the real invariant instead: whatever the section prints,
    an option row must never appear as a plain holding line.
    """
    from datetime import date as _d

    from whale_agent.ingestion.fund_watchlist import Holding
    from whale_agent.summarization.tracked_funds import render_tracked_funds

    snap = _salp_snapshot()
    puts = [
        Holding(
            issuer="NVIDIA CORPORATION",
            value_usd=1_568_257_120,
            shares=1,
            cusip="67066G104",
            option_type="PUT",
        ),
        Holding(
            issuer="VANECK ETF TRUST",
            value_usd=2_042_716_860,
            shares=1,
            cusip="92189F676",
            option_type="PUT",
        ),
    ]
    body = "\n".join(render_tracked_funds([replace(snap, top_holdings=puts)], _d(2026, 8, 4)))
    for holding in puts:
        if holding.issuer in body:
            line = next(ln for ln in body.splitlines() if holding.issuer in ln)
            assert "PUT" in line.upper(), f"option rendered as a plain holding: {line!r}"


def test_the_option_note_states_notional_without_implying_a_short():
    note = option_note(_salp_snap())
    assert "11 long put positions on $8.5B of underlying" in note
    assert "5 long call positions on $1.4B of underlying" in note
    assert "not premium paid" in note
    for banned in ("short", "bearish", "against", "betting"):
        assert banned not in note.lower()
    body = "\n".join(render_tracked_funds([_salp_snap()], ON))
    assert " ".join(body.split()).count(" ".join(note.split())) == 1
    assert max(len(line) for line in body.splitlines()) <= 78


def test_a_fund_with_no_options_says_nothing_about_options():
    assert (
        option_note(
            _salp_snap(
                put_name_count=0, call_name_count=0, put_notional_usd=0, call_notional_usd=0
            )
        )
        == ""
    )


def test_the_html_card_separates_stock_from_option_notional():
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        tracked_funds=[_salp_snap()],
    )
    assert "in stock" in html
    assert "long put positions on $8.5B of underlying" in html
    assert "NVIDIA" not in html


def test_a_manager_holding_only_options_still_appears():
    """position_count counts stock, so it cannot be the test for 'we read a table'."""
    snap = _salp_snap(total_value_usd=0, position_count=0, top_holdings=[])
    assert snap.has_13f is True
    assert "Situational Awareness LP" in "\n".join(render_tracked_funds([snap], ON))


def test_the_option_notional_is_allowed_through_the_egress_guard():
    from whale_agent.summarization.tracked_funds import aggregate_figure_tokens

    tokens = aggregate_figure_tokens([_salp_snap()])
    assert {"$3.9B", "$8.5B", "$1.4B"} <= tokens
    # A fund with no option rows must not push "$0" into the allowlist.
    plain = aggregate_figure_tokens(
        [
            _salp_snap(
                put_name_count=0, call_name_count=0, put_notional_usd=0, call_notional_usd=0
            )
        ]
    )
    assert "$0" not in plain


SMALL_TABLE = """
<infoTable><nameOfIssuer>NATERA INC</nameOfIssuer><cusip>632307104</cusip>
  <value>612691</value><shrsOrPrnAmt><sshPrnamt>4000</sshPrnamt></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>NATERA INC</nameOfIssuer><cusip>632307104</cusip>
  <value>100000</value><shrsOrPrnAmt><sshPrnamt>500</sshPrnamt></shrsOrPrnAmt>
  <putCall>Put</putCall></infoTable>
"""


def _fake_get(monkeypatch, document: str):
    """Serve fetch_snapshot's three EDGAR calls from memory."""
    import json as _json

    submissions = {
        "filings": {
            "recent": {
                "form": ["13F-HR"],
                "filingDate": ["2026-05-18"],
                "reportDate": ["2026-03-31"],
                "accessionNumber": ["0002045724-26-000008"],
            }
        }
    }
    listing = {"directory": {"item": [{"name": "table.xml"}]}}

    def fake(url, *a, **kw):
        if "submissions" in url:
            return _json.dumps(submissions).encode()
        if url.endswith("index.json"):
            return _json.dumps(listing).encode()
        return document.encode()

    monkeypatch.setattr("whale_agent.ingestion.fund_watchlist._get", fake)


def test_a_table_in_thousands_is_rescaled_without_losing_the_option_type(monkeypatch):
    """The rescale rebuilt Holdings positionally and dropped the CUSIP and putCall.

    Which silently turned every option row back into a holding on any filer that
    reported in thousands.
    """
    from whale_agent.ingestion.fund_watchlist import fetch_snapshot

    _fake_get(monkeypatch, SMALL_TABLE)
    snap = fetch_snapshot("Duquesne Family Office", "0001536411")
    assert snap.error is None
    assert snap.values_scaled_from_thousands is True
    assert snap.total_value_usd == 612_691_000
    assert snap.position_count == 1
    assert snap.put_notional_usd == 100_000_000
    assert snap.put_name_count == 1
    assert snap.top_holdings[0].cusip == "632307104"


def test_fetch_snapshot_reports_the_real_table_as_stock_plus_options(monkeypatch):
    from whale_agent.ingestion.fund_watchlist import fetch_snapshot

    _fake_get(monkeypatch, SALP_XML)
    snap = fetch_snapshot("Situational Awareness LP", "0002045724")
    assert (snap.total_value_usd, snap.position_count) == (3_855_771_552, 26)
    assert (snap.put_notional_usd, snap.put_name_count) == (8_459_056_999, 11)
    assert (snap.call_notional_usd, snap.call_name_count) == (1_361_829_026, 5)
    assert all(not h.is_option for h in snap.top_holdings)


def test_the_newest_quarter_wins_even_if_the_vendor_reorders_the_rows():
    """FMP returns about six quarters. Reading rows[0] trusts its ordering."""
    rows = [
        {
            "date": "2024-12-31",
            "portfolioSize": 6,
            "securitiesAdded": 6,
            "securitiesRemoved": 0,
            "marketValue": 254813765,
            "previousMarketValue": 0,
        },
        {
            "date": "2026-03-31",
            "portfolioSize": 42,
            "securitiesAdded": 19,
            "securitiesRemoved": 10,
            "marketValue": 13676657577,
            "previousMarketValue": 5516758345,
        },
    ]
    activity = parse_activity(rows)
    assert activity.as_of == date(2026, 3, 31)
    assert activity.portfolio_size == 42


# -- the allowlist may only contain figures this section actually prints -------
#
# `allow_figures` exempts a token from the egress ceiling across the WHOLE email body,
# not just the section that produced it, so a token in there is pre-cleared to appear
# anywhere, including inside model-written prose. Two figures were being allowlisted
# and never printed: the vendor's `marketValue`, which is the option-inclusive
# portfolio value ($13,676,657,577 for Situational Awareness LP, the exact number the
# put/call split exists to stop presenting as ownership), and the top three holdings.


def _salp_snapshot():
    from datetime import date as _d

    from whale_agent.ingestion.fund_watchlist import FundSnapshot, Holding, parse_activity

    activity = parse_activity(
        [
            {
                "date": "2026-03-31",
                "portfolioSize": 42,
                "securitiesAdded": 19,
                "securitiesRemoved": 10,
                "marketValue": 13_676_657_577,
                "previousMarketValue": 5_516_758_345,
            }
        ]
    )
    return FundSnapshot(
        name="Situational Awareness LP",
        cik="2045724",
        total_value_usd=3_855_771_552,
        position_count=26,
        put_notional_usd=8_459_056_999,
        put_name_count=11,
        call_notional_usd=1_361_829_026,
        call_name_count=5,
        top_holdings=[Holding(issuer="SOME CO", value_usd=879_000_000, shares=1, cusip="X")],
        activity=activity,
        period_end=_d(2026, 3, 31),
    )


def test_the_option_inclusive_vendor_total_is_never_allowlisted():
    from whale_agent.summarization.tracked_funds import aggregate_figure_tokens

    assert "$13.7B" not in aggregate_figure_tokens([_salp_snapshot()])


def test_every_allowlisted_figure_is_one_the_section_prints():
    """The general invariant, so the next unprinted figure cannot slip in either."""
    from datetime import date as _d

    from whale_agent.summarization.tracked_funds import (
        aggregate_figure_tokens,
        render_tracked_funds,
    )

    snap = _salp_snapshot()
    body = "\n".join(render_tracked_funds([snap], _d(2026, 8, 4)))
    unprinted = sorted(t for t in aggregate_figure_tokens([snap]) if t not in body)
    assert not unprinted, f"allowlisted but never printed: {unprinted}"


def test_an_unrecognised_putcall_value_is_not_silently_treated_as_stock():
    """Fail closed. An absent tag means stock, but a tag we cannot read does not.

    The original defect was an option counted as ownership. Mapping an unrecognised
    non-empty putCall to "" would reintroduce exactly that, quietly, for whichever
    filer writes it differently. "P" and "Short" both reached the stock bucket before.
    """
    from whale_agent.ingestion.fund_watchlist import parse_infotable

    def one(put_call: str) -> str:
        xml = f"""<infoTable><nameOfIssuer>ACME</nameOfIssuer><cusip>123456789</cusip>
        <value>1000</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt></shrsOrPrnAmt>
        <putCall>{put_call}</putCall></infoTable>"""
        return parse_infotable(xml)[0].option_type

    assert one("Put") == "PUT"
    assert one("CALL") == "CALL"
    # Unreadable, so not stock. It must not merge with a genuine long position.
    assert one("P") != ""
    assert one("Short") != ""


def test_an_absent_or_blank_putcall_is_still_stock():
    from whale_agent.ingestion.fund_watchlist import parse_infotable

    base = """<infoTable><nameOfIssuer>ACME</nameOfIssuer><cusip>123456789</cusip>
    <value>1000</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt></shrsOrPrnAmt>{tag}</infoTable>"""
    assert parse_infotable(base.format(tag=""))[0].option_type == ""
    assert parse_infotable(base.format(tag="<putCall></putCall>"))[0].option_type == ""
    assert parse_infotable(base.format(tag="<putCall>   </putCall>"))[0].option_type == ""


def test_a_diff_never_labels_an_option_move_with_a_bare_issuer_name():
    """`moved` projected to (issuer, delta) and dropped the option type.

    The keying is correct, so a put and a stock row are already distinct entries, but
    the projection threw the distinction away again: two entries both reading "NVIDIA"
    with no way to tell which one grew. Any future consumer would print "added $500M to
    NVIDIA" for a put. No caller renders this today, which is why it is cheap to fix now.
    """
    from whale_agent.ingestion.fund_watchlist import Holding, diff_holdings

    prior = [
        Holding(issuer="NVIDIA", value_usd=1_000, shares=1, cusip="67066G104"),
        Holding(
            issuer="NVIDIA", value_usd=5_000, shares=1, cusip="67066G104", option_type="PUT"
        ),
    ]
    current = [
        Holding(issuer="NVIDIA", value_usd=1_500, shares=1, cusip="67066G104"),
        Holding(
            issuer="NVIDIA", value_usd=9_000, shares=1, cusip="67066G104", option_type="PUT"
        ),
    ]
    changes = diff_holdings(current, prior)
    labels = [label for label, _delta in changes.increased + changes.decreased]
    assert len(labels) == len(set(labels)), f"ambiguous labels: {labels}"
    assert any("PUT" in label for label in labels)


# ---------------------------------------------------------------------------------
# The 5 largest trades: pooled across every tracked manager, ranked by dollar value.
# ---------------------------------------------------------------------------------


def _trade(
    shares=1000,
    price=10.0,
    code="P",
    acquired=True,
    issuer="ACME CORP",
    symbol="ACME",
    on=date(2026, 7, 1),
):
    from whale_agent.ingestion.fund_watchlist import Trade

    return Trade(
        issuer=issuer,
        symbol=symbol,
        on=on,
        code=code,
        shares=shares,
        price=price,
        acquired=acquired,
    )


def _filing_with_trades(
    trades, form="4", filed=date(2026, 7, 2), accession="0001-26-000001", cik="0001067983"
):
    from whale_agent.ingestion.fund_watchlist import FastFiling

    return FastFiling(form=form, filed=filed, accession=accession, trades=trades, _cik=cik)


def _stake_filing(
    percent,
    issuer="ACME CORP",
    form="SCHEDULE 13D",
    filed=date(2026, 7, 2),
    accession="0002-26-000001",
    cik="0001067983",
    shares=None,
):
    from whale_agent.ingestion.fund_watchlist import FastFiling, StakeDetail

    return FastFiling(
        form=form,
        filed=filed,
        accession=accession,
        detail=StakeDetail(issuer=issuer, percent=percent, shares=shares),
        _cik=cik,
    )


def test_trade_dollar_value_is_shares_times_price():
    t = _trade(shares=1000, price=10.5)
    assert t.dollar_value == 10500.0


def test_trade_without_a_price_has_no_dollar_value():
    t = _trade(price=None)
    assert t.dollar_value is None


def test_a_grant_is_not_an_open_market_trade():
    """Code A is a grant/award, not a discretionary decision to buy."""
    assert _trade(code="A").is_open_market is False


def test_tax_withholding_is_not_an_open_market_trade():
    assert _trade(code="F").is_open_market is False


def test_an_open_market_purchase_and_sale_are_recognised():
    assert _trade(code="P").is_open_market is True
    assert _trade(code="S").is_open_market is True


def test_pool_top_trades_ranks_by_dollar_value_across_managers():
    from whale_agent.ingestion.fund_watchlist import pool_top_trades

    big = _snap(
        name="Big Fund",
        recent_filings=[_filing_with_trades([_trade(shares=100_000, price=50.0)])],
    )
    small = _snap(
        name="Small Fund", recent_filings=[_filing_with_trades([_trade(shares=10, price=1.0)])]
    )
    out = pool_top_trades([big, small], top=5)
    assert [t.manager for t in out] == ["Big Fund", "Small Fund"]
    assert out[0].dollar_value == 5_000_000.0


def test_pool_top_trades_excludes_grants_and_tax_withholding():
    from whale_agent.ingestion.fund_watchlist import pool_top_trades

    snap = _snap(
        recent_filings=[
            _filing_with_trades(
                [
                    _trade(code="A", shares=1_000_000, price=100.0),
                    _trade(code="F", shares=1_000_000, price=100.0),
                    _trade(code="P", shares=10, price=1.0),
                ]
            )
        ]
    )
    out = pool_top_trades([snap], top=5)
    assert len(out) == 1
    assert out[0].direction == "bought"


def test_pool_top_trades_caps_at_top_n():
    """Nine different managers, each with one qualifying filing: the ranked list is
    capped at `top`, not at the number of qualifying filings."""
    from whale_agent.ingestion.fund_watchlist import pool_top_trades

    snaps = [
        _snap(
            name=f"Fund {i}",
            cik=f"000{i}",
            total_value_usd=1_000_000,
            recent_filings=[
                _filing_with_trades(
                    [_trade(shares=i, price=1.0)],
                    accession=f"000{i}-26-000001",
                    cik=f"000{i}",
                )
            ],
        )
        for i in range(1, 10)
    ]
    out = pool_top_trades(snaps, top=5)
    assert len(out) == 5


def test_pool_top_trades_skips_a_snapshot_with_an_error():
    from whale_agent.ingestion.fund_watchlist import FundSnapshot, pool_top_trades

    broken = FundSnapshot(name="Broken", cik="0001", error="timeout")
    out = pool_top_trades([broken], top=5)
    assert out == []


def test_pool_top_trades_ranks_by_percent_of_portfolio_not_raw_dollars():
    """A $5M trade against a $10M book (50%) must outrank a $50M trade against a
    $5B book (1%), even though the second is ten times bigger in raw dollars."""
    from whale_agent.ingestion.fund_watchlist import pool_top_trades

    tiny_book = _snap(
        name="Tiny Fund",
        cik="0001",
        total_value_usd=10_000_000,
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=100_000, price=50.0)],
                accession="0001-26-000001",
                cik="0001",
            )
        ],
    )
    huge_book = _snap(
        name="Huge Fund",
        cik="0002",
        total_value_usd=5_000_000_000,
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=500_000, price=100.0)],
                accession="0002-26-000001",
                cik="0002",
            )
        ],
    )
    out = pool_top_trades([tiny_book, huge_book], top=5)
    assert [t.manager for t in out] == ["Tiny Fund", "Huge Fund"]
    assert out[0].percent_of_portfolio == 50.0
    assert out[0].dollar_value == 5_000_000.0
    assert out[1].percent_of_portfolio == 1.0
    assert out[1].dollar_value == 50_000_000.0


def test_pool_top_trades_dedupes_multiple_lines_sharing_one_accession():
    """Two transaction lines from the same Form 4 (same accession) are one filing,
    not two rows -- shares sum, price is a share-weighted average."""
    from whale_agent.ingestion.fund_watchlist import pool_top_trades

    snap = _snap(
        name="Split Filer",
        cik="0003",
        total_value_usd=100_000_000,
        recent_filings=[
            _filing_with_trades(
                [
                    _trade(shares=1000, price=10.0),
                    _trade(shares=3000, price=20.0),
                ],
                accession="0003-26-000001",
                cik="0003",
            )
        ],
    )
    out = pool_top_trades([snap], top=5)
    assert len(out) == 1
    assert out[0].shares == 4000
    # weighted avg: (1000*10 + 3000*20) / 4000 = 17.5
    assert out[0].price == 17.5
    assert out[0].dollar_value == 70_000.0
    assert out[0].accession == "0003-26-000001"


def test_pool_top_trades_caps_at_one_row_per_manager():
    """A manager with several qualifying filings occupies only its best slot; it
    does not crowd out other managers in the top-N."""
    from whale_agent.ingestion.fund_watchlist import pool_top_trades

    one_manager = _snap(
        name="Blackstone",
        cik="0009",
        total_value_usd=1_000_000_000,
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=100_000, price=100.0)], accession="0009-26-000001", cik="0009"
            ),
            _filing_with_trades(
                [_trade(shares=50_000, price=100.0)], accession="0009-26-000002", cik="0009"
            ),
        ],
    )
    out = pool_top_trades([one_manager], top=5)
    assert len(out) == 1
    assert out[0].accession == "0009-26-000001"  # the bigger of the two


def test_pool_top_trades_puts_unrankable_trades_in_a_separate_dollar_ranked_list():
    """A manager with no 13F on file (or a zero/unknown portfolio value) must never
    be divided by zero or silently dropped -- it goes in the dollar-ranked overflow
    with percent_of_portfolio left unset."""
    from whale_agent.ingestion.fund_watchlist import pool_top_trades

    no_13f = _snap(
        name="No Filing Fund",
        cik="0004",
        total_value_usd=0,
        position_count=0,
        put_name_count=0,
        call_name_count=0,
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=1000, price=10.0)],
                accession="0004-26-000001",
                cik="0004",
            )
        ],
    )
    has_book = _snap(
        name="Real Fund",
        cik="0005",
        total_value_usd=1_000_000,
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=1000, price=10.0)],
                accession="0005-26-000001",
                cik="0005",
            )
        ],
    )
    out = pool_top_trades([no_13f, has_book], top=5)
    ranked = [t for t in out if t.percent_of_portfolio is not None]
    unrankable = [t for t in out if t.percent_of_portfolio is None]
    assert [t.manager for t in ranked] == ["Real Fund"]
    assert [t.manager for t in unrankable] == ["No Filing Fund"]
    assert unrankable[0].portfolio_value_usd in (None, 0)


def test_pool_top_stakes_labels_an_initial_filing_as_new():
    from whale_agent.ingestion.fund_watchlist import pool_top_stakes

    snap = _snap(recent_filings=[_stake_filing(9.9, form="SCHEDULE 13D")])
    out = pool_top_stakes([snap], top=5)
    assert out[0].is_new is True


def test_pool_top_stakes_labels_an_amendment_as_not_new():
    from whale_agent.ingestion.fund_watchlist import pool_top_stakes

    snap = _snap(recent_filings=[_stake_filing(9.9, form="SCHEDULE 13D/A")])
    out = pool_top_stakes([snap], top=5)
    assert out[0].is_new is False


def test_pool_top_stakes_ranks_by_percent_not_dollars():
    from whale_agent.ingestion.fund_watchlist import pool_top_stakes

    snap = _snap(
        recent_filings=[
            _stake_filing(4.0, issuer="SMALL STAKE"),
            _stake_filing(12.0, issuer="BIG STAKE"),
        ]
    )
    out = pool_top_stakes([snap], top=5)
    assert [s.issuer for s in out] == ["BIG STAKE", "SMALL STAKE"]


def test_pool_top_stakes_ignores_filings_with_no_percent():
    from whale_agent.ingestion.fund_watchlist import FastFiling, StakeDetail, pool_top_stakes

    filing = FastFiling(
        form="SCHEDULE 13D",
        filed=date(2026, 7, 2),
        accession="x",
        detail=StakeDetail(issuer="NO PERCENT", percent=None),
    )
    snap = _snap(recent_filings=[filing])
    assert pool_top_stakes([snap], top=5) == []


def test_render_top_trades_states_direction_shares_price_and_value():
    from whale_agent.ingestion.fund_watchlist import pool_top_stakes, pool_top_trades
    from whale_agent.summarization.tracked_funds import render_top_trades

    snap = _snap(
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=100_000, price=50.0, issuer="ACME CORP", symbol="ACME")]
            )
        ]
    )
    trades = pool_top_trades([snap])
    stakes = pool_top_stakes([snap])
    body = "\n".join(render_top_trades(trades, stakes, ON))
    assert "bought" in body
    assert "100,000 shares" in body
    assert "ACME CORP" in body and "ACME" in body
    assert "$50.00" in body
    assert "$5M" in body


def test_render_top_trades_never_shows_a_dollar_value_for_a_stake():
    from whale_agent.summarization.tracked_funds import render_top_trades

    snap = _snap(recent_filings=[_stake_filing(9.9, issuer="STAKE CO")])
    from whale_agent.ingestion.fund_watchlist import pool_top_stakes

    stakes = pool_top_stakes([snap])
    body = "\n".join(render_top_trades([], stakes, ON))
    assert "STAKE CO" in body
    assert "9.9%" in body
    assert "new position" in body


def test_render_top_trades_labels_amendments_honestly():
    from whale_agent.ingestion.fund_watchlist import pool_top_stakes
    from whale_agent.summarization.tracked_funds import render_top_trades

    snap = _snap(recent_filings=[_stake_filing(9.9, issuer="STAKE CO", form="SCHEDULE 13D/A")])
    stakes = pool_top_stakes([snap])
    body = "\n".join(render_top_trades([], stakes, ON))
    assert "amended stake" in body
    assert "STAKE CO added to" not in body
    assert "STAKE CO trimmed" not in body


def test_render_top_trades_carries_the_sec_link_and_the_report_link():
    from whale_agent.ingestion.fund_watchlist import pool_top_trades
    from whale_agent.summarization.tracked_funds import render_top_trades

    snap = _snap(
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=1000, price=10.0)], accession="0001067983-26-000042"
            )
        ]
    )
    trades = pool_top_trades([snap])
    body = "\n".join(
        render_top_trades(
            trades,
            [],
            ON,
            our_link=lambda accession: f"https://example.test/report.html#filing-{accession}",
        )
    )
    assert "sec.gov" in body
    assert "https://example.test/report.html#filing-0001067983-26-000042" in body


def test_render_top_trades_says_so_when_nothing_qualified():
    from whale_agent.summarization.tracked_funds import render_top_trades

    body = "\n".join(render_top_trades([], [], ON))
    assert "No open-market trade" in body
    assert "No new or amended stake" in body


def test_the_html_carries_the_top_trades_section():
    from whale_agent.ingestion.fund_watchlist import pool_top_stakes, pool_top_trades
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    snap = _snap(
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=100_000, price=50.0, issuer="ACME CORP", symbol="ACME")],
                accession="0001067983-26-000099",
            )
        ]
    )
    trades = pool_top_trades([snap])
    stakes = pool_top_stakes([snap])
    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        top_trades=trades,
        top_stakes=stakes,
        tracked_report_link="https://example.test/tracked-filings-2026-08-03.html",
    )
    assert "The largest trades" in html
    assert "ACME CORP" in html
    assert "$5M" in html
    # One link per row when a report page is available (see _row_links): "Our report",
    # not the SEC link, which now lives on the report page's own copy of this row.
    assert (
        "https://example.test/tracked-filings-2026-08-03.html#f-0001067983-26-000099" in html
    )


def test_the_html_top_trades_section_reports_a_quiet_week():
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        top_trades=[],
        top_stakes=[],
    )
    assert "No open-market trade" in html


def test_the_html_top_trades_section_omits_the_report_link_when_unset():
    """No --article-base-url must not crash or emit a half-formed anchor URL."""
    from whale_agent.ingestion.fund_watchlist import pool_top_trades
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    snap = _snap(recent_filings=[_filing_with_trades([_trade(shares=1000, price=10.0)])])
    trades = pool_top_trades([snap])
    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        top_trades=trades,
        top_stakes=[],
        tracked_report_link="",
    )
    assert "#f-" not in html
    assert "sec.gov" in html


# ---------------------------------------------------------------------------------
# The tracked-filings appendix page: stable per-filing anchors for the email to link into.
# ---------------------------------------------------------------------------------


def test_filing_anchor_is_derived_from_the_accession_not_the_manager_or_a_list_index():
    from whale_agent.publishing.tracked_filings_html import filing_anchor

    # "f-", not the more readable "filing-": trimmed to save bytes across 200+ links
    # in the email (see filing_anchor's docstring).
    assert filing_anchor("0001067983-26-000042") == "f-0001067983-26-000042"


def test_tracked_filings_page_carries_an_anchor_per_filing():
    from whale_agent.ingestion.fund_watchlist import pool_top_stakes, pool_top_trades
    from whale_agent.publishing.tracked_filings_html import (
        filing_anchor,
        render_tracked_filings_page,
    )

    snap = _snap(
        recent_filings=[
            _filing_with_trades(
                [_trade(shares=1000, price=10.0, issuer="ACME CORP", symbol="ACME")],
                accession="0001067983-26-000042",
            )
        ]
    )
    trades = pool_top_trades([snap])
    stakes = pool_top_stakes([snap])
    html = "\n".join(render_tracked_filings_page(trades, stakes, ON).values())
    assert f'id="{filing_anchor("0001067983-26-000042")}"' in html
    assert "ACME CORP" in html


def test_the_html_roster_is_trimmed_with_a_count_of_the_rest():
    """A watchlist bigger than the email cap shows only the top managers, plus how
    many more are tracked -- never a silent drop."""
    from whale_agent.summarization.render_weekly_html import (
        EMAIL_ROSTER_LIMIT,
        render_weekly_html,
    )

    snaps = [
        _snap(name=f"Fund {i}", cik=f"000{i}", total_value_usd=(10 - i) * 1_000_000_000)
        for i in range(EMAIL_ROSTER_LIMIT + 3)
    ]
    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        tracked_funds=snaps,
    )
    shown = sum(1 for s in snaps if s.name in html)
    assert shown == EMAIL_ROSTER_LIMIT
    assert "more tracked managers" in html
    assert "Full roster in the report" in html


def test_the_html_fund_moves_are_trimmed_with_a_count_of_the_rest():
    from whale_agent.summarization.render_weekly_html import (
        EMAIL_MOVERS_LIMIT,
        render_weekly_html,
    )

    snaps = [
        _snap(
            name=f"Mover {i}",
            cik=f"001{i}",
            position_count=0,
            put_name_count=0,
            call_name_count=0,  # no 13F: not on the roster
            recent_filings=[_stake_filing(9.9, accession=f"001{i}-26-000001", cik=f"001{i}")],
        )
        for i in range(EMAIL_MOVERS_LIMIT + 2)
    ]
    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        tracked_funds=snaps,
    )
    shown = sum(1 for s in snaps if s.name in html)
    assert shown == EMAIL_MOVERS_LIMIT
    assert "more tracked managers filed this week" in html


def test_the_html_overview_section_is_trimmed_with_a_count_of_the_rest():
    from whale_agent.jobs.overview import OverviewRow, Section
    from whale_agent.summarization.render_weekly_html import (
        EMAIL_SECTION_ROW_LIMIT,
        render_weekly_html,
    )

    rows = [
        OverviewRow(
            filer=f"Filer {i}",
            filer_role=None,
            issuer=f"Issuer {i}",
            ticker=f"T{i}",
            what="open market purchase",
            when=ON,
            amount_usd=1_000_000.0 * (10 - i),
            amount_label=f"${10 - i}M",
            is_estimate=False,
            filing_url="https://sec.gov/x",
            jurisdiction="US",
        )
        for i in range(EMAIL_SECTION_ROW_LIMIT + 4)
    ]
    section = Section(key="insider", label="Insider filings", rows=rows)
    html = render_weekly_html(
        on=ON,
        cleared=1,
        recorded=1,
        articles=[],
        sections=[section],
        how_to_read="",
    )
    shown = sum(1 for r in rows if r.filer in html)
    assert shown == EMAIL_SECTION_ROW_LIMIT
    assert "more insider filings this week" in html


def test_rendered_email_is_well_under_gmails_clip_threshold():
    """A representative fifteen-manager watchlist used to render past 180KB, well
    over Gmail's ~102KB clip point -- almost entirely from `style="..."` attributes
    repeated on every table cell. Hoisting that repetition into a `<style>` block
    (see the module docstring) and widening the digest limits back up brings the same
    fixture in under 60KB, with real headroom before Gmail's clip point. This is a
    regression test, not a live render -- it uses in-memory fixtures only, no network."""
    from whale_agent.jobs.overview import OverviewRow, Section
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    snaps = [
        _snap(
            name=f"Fund {i}",
            cik=f"00{i}",
            total_value_usd=(i + 1) * 500_000_000,
            recent_filings=[
                _stake_filing(
                    5.0 + i, issuer=f"ISSUER {i}", accession=f"00{i}-26-000001", cik=f"00{i}"
                )
            ],
        )
        for i in range(15)
    ]
    sections = [
        Section(
            key=k,
            label=lbl,
            rows=[
                OverviewRow(
                    filer=f"Filer {k}{i}",
                    filer_role=None,
                    issuer=f"Issuer {k}{i}",
                    ticker="TK",
                    what="open market purchase",
                    when=ON,
                    amount_usd=1_000_000.0,
                    amount_label="$1M",
                    is_estimate=False,
                    filing_url="https://sec.gov/x",
                    jurisdiction="US",
                )
                for i in range(12)
            ],
        )
        for k, lbl in [
            ("insider", "Insider filings"),
            ("congressional", "Congressional trading"),
            ("institutional", "Institutional buying"),
            ("international", "International plays"),
        ]
    ]
    html = render_weekly_html(
        on=ON,
        cleared=10,
        recorded=30,
        articles=[],
        sections=sections,
        how_to_read="",
        tracked_funds=snaps,
    )
    assert len(html.encode("utf-8")) < 60_000


def test_rendered_email_stays_under_90kb_at_full_52_manager_scale_with_report_links():
    """The real regression: a real 2026-08-17 render with per-item report links
    restored on every row (not just the top-5 trades) put the email at 150,650 bytes,
    over Gmail's ~102,400 clip point, purely from paying for both a "Filing" and an
    "Our report" link on ~217 rows. Fixed by dropping the SEC link from the email body
    once a report link is available (`_row_links` links only to the report; the report
    page itself keeps the SEC citation), a shorter anchor scheme, and capping the
    previously-uncapped resale section and per-manager filing counts.

    This fixture is deliberately at the scale that broke: 52 managers, several stake
    filings apiece, an uncapped-in-the-past resale list, market-wide stakes, and a
    live report_link so every row actually pays for its "Our report" link -- the
    condition the smaller 15-manager fixture above does not exercise. No network."""
    from whale_agent.ingestion.fund_watchlist import (
        MarketStake,
        StakeDetail,
        pool_top_stakes,
        pool_top_trades,
    )
    from whale_agent.ingestion.resale_watch import ResaleRegistration
    from whale_agent.jobs.overview import OverviewRow, Section
    from whale_agent.summarization.render_weekly_html import render_weekly_html

    report_link = "https://whale-weekly.example.workers.dev/tracked-filings-2026-08-17"

    snaps = [
        _snap(
            name=f"Fund {i}",
            cik=f"{i:010d}",
            total_value_usd=(i + 1) * 300_000_000,
            recent_filings=[
                _stake_filing(
                    5.0 + i,
                    issuer=f"ISSUER {i}-{j}",
                    accession=f"000{i:03d}{j}-26-000001",
                    cik=f"{i:010d}",
                )
                for j in range(3)
            ],
        )
        for i in range(52)
    ]
    sections = [
        Section(
            key=k,
            label=lbl,
            rows=[
                OverviewRow(
                    filer=f"Filer {k}{i}",
                    filer_role=None,
                    issuer=f"Issuer {k}{i}",
                    ticker="TK",
                    what="open market purchase",
                    when=ON,
                    amount_usd=1_000_000.0,
                    amount_label="$1M",
                    is_estimate=False,
                    filing_url=f"https://sec.gov/x/{k}/{i}",
                    jurisdiction="US",
                )
                for i in range(12)
            ],
        )
        for k, lbl in [
            ("insider", "Insider filings"),
            ("congressional", "Congressional trading"),
            ("institutional", "Institutional buying"),
            ("international", "International plays"),
        ]
    ]
    market_stakes = [
        MarketStake(
            filed=ON,
            form="SC 13D",
            holders=[f"Market Filer {i}"],
            detail=StakeDetail(issuer=f"Widget {i}", percent=5.5 + i, person_types=["IA"]),
            url=f"https://www.sec.gov/Archives/edgar/data/1/00{i:03d}5600000001/",
            accession=f"00{i:03d}5600000001",
        )
        for i in range(20)
    ]
    resale = [
        ResaleRegistration(
            whale_name=f"Fund {i}",
            issuer=f"Widget {i}",
            symbol=f"W{i}",
            form="S-1",
            filed=ON,
            shares_registered=1000 + i,
            shares_held=2000 + i,
            url=f"https://www.sec.gov/Archives/edgar/data/1/00{i:03d}5600000002/doc.htm",
            accession=f"00{i:03d}5600000002",
        )
        for i in range(15)
    ]
    trades = pool_top_trades(snaps[:5])
    stakes = pool_top_stakes(snaps[:5])

    html = render_weekly_html(
        on=ON,
        cleared=40,
        recorded=90,
        articles=[],
        sections=sections,
        how_to_read="",
        tracked_funds=snaps,
        market_stakes=market_stakes,
        stakes_examined=1200,
        resale=resale,
        top_trades=trades,
        top_stakes=stakes,
        tracked_report_link=report_link,
    )
    size = len(html.encode("utf-8"))
    assert size < 90_000, f"email is {size} bytes, over the 90,000 budget"
    # The fix must not silently drop the headline feature: every row shown still
    # carries a working "Our report" link, just not a duplicate SEC one.
    assert html.count("Our report") > 50
    assert "file://" not in html


def test_public_report_link_is_never_a_file_url():
    """The old fallback resolved a local path to file://, which is dead in an inbox.
    With no --article-base-url, the report has no public link at all."""
    from whale_agent.jobs.digest_weekly import public_report_link

    assert public_report_link("", "tracked-filings-2026-08-17.html") == ""
    assert not public_report_link("", "x.html").startswith("file://")


def test_public_report_link_uses_the_configured_base():
    from whale_agent.jobs.digest_weekly import public_report_link

    # Extensionless canonical form: the site serves it as the 200 directly, the
    # .html form as a redirect to it (see public_report_link's docstring).
    assert (
        public_report_link("https://example.test/reports/", "tracked-filings-2026-08-17.html")
        == "https://example.test/reports/tracked-filings-2026-08-17"
    )


def test_full_report_page_carries_the_whole_roster_and_overview():
    """Everything the email digest trims must still be reachable somewhere -- this
    page, not just the pooled trades and stakes."""
    from whale_agent.jobs.overview import OverviewRow, Section
    from whale_agent.publishing.tracked_filings_html import render_tracked_filings_page

    snaps = [_snap(name=f"Fund {i}", cik=f"00{i}") for i in range(6)]
    section = Section(
        key="insider",
        label="Insider filings",
        rows=[
            OverviewRow(
                filer="Deep Cut Filer",
                filer_role=None,
                issuer="Deep Cut Issuer",
                ticker="DC",
                what="open market purchase",
                when=ON,
                amount_usd=1_000_000.0,
                amount_label="$1M",
                is_estimate=False,
                filing_url="https://sec.gov/x",
                jurisdiction="US",
            )
        ],
    )
    html = "\n".join(
        render_tracked_filings_page([], [], ON, snapshots=snaps, sections=[section]).values()
    )
    for snap in snaps:
        assert snap.name in html
    assert "Deep Cut Filer" in html


def test_write_tracked_filings_page_writes_to_the_articles_dir(tmp_path):
    from whale_agent.publishing.tracked_filings_html import (
        tracked_filings_page_filename,
        write_tracked_filings_page,
    )

    pages = {
        tracked_filings_page_filename(ON, "index"): "<html>index</html>",
        tracked_filings_page_filename(ON, "trades"): "<html>trades</html>",
    }
    written = write_tracked_filings_page(pages, tmp_path, ON)
    assert set(written) == set(pages)
    for filename, path in written.items():
        assert path.name == filename
        assert path.read_text(encoding="utf-8") == pages[filename]
