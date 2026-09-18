"""Output-safety controls: the tightened plausibility fallback, the quarantine record,
the advisory verifier, and sampled review of what went out.

The organising idea across all four is that a guarantee is code and never a prompt. The
deterministic tests below assert refusals; the verifier tests assert that the model layer
cannot turn a refusal into an approval.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.conftest import make_event
from whale_agent.models.context import EventContext

NOW = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


# -- item 5: the market-cap-absent fallback ------------------------------------------


def test_the_quadrillion_case_is_blocked_with_no_market_cap():
    """The original incident, on the path where enrichment never ran.

    MetLife/FINS: FMP put the transaction total in the price field, so shares and price
    were both 40,000,000. The decisive market-cap check is unavailable here by
    construction, and the row still must not survive screening.
    """
    from whale_agent.enrichment.plausibility import check_event

    event = make_event(
        share_count=40_000_000.0,
        price_used=40_000_000.0,
        usd_value=1_600_000_000_000_000.0,
        context=EventContext(),
    )
    assert event.context.market_cap_usd is None
    verdict = check_event(event)
    assert not verdict
    assert any("transaction total" in r for r in verdict.reasons)
    assert any("exceeds any real disclosure" in r for r in verdict.reasons)


def test_an_unverifiable_figure_faces_a_tighter_ceiling():
    """Less evidence means a lower ceiling, not the same one.

    $60B is under the absolute ceiling and would have passed the old cap-absent path
    unexamined. With no market capitalisation there is nothing to check it against, so it
    is quarantined for review rather than published.
    """
    from whale_agent.enrichment.plausibility import check_event

    verdict = check_event(make_event(usd_value=60_000_000_000.0, context=EventContext()))
    assert not verdict
    assert any("unverified ceiling" in r for r in verdict.reasons)


def test_the_same_figure_passes_once_a_market_cap_corroborates_it():
    """The tighter ceiling is a stand-in for the decisive check, not an extra rule."""
    from whale_agent.enrichment.plausibility import check_event

    assert check_event(
        make_event(
            usd_value=60_000_000_000.0,
            context=EventContext(market_cap_usd=900_000_000_000.0),
        )
    )


def test_a_zero_market_cap_is_treated_as_absent_not_as_zero():
    """A cap of zero is a missing enrichment, and the old `if cap` read it as absent.

    The conditional gap: falsy-zero silently skipped the decisive check and then also
    skipped nothing else, so a zero cap was the most permissive state in the module.
    """
    from whale_agent.enrichment.plausibility import check_event

    verdict = check_event(
        make_event(usd_value=60_000_000_000.0, context=EventContext(market_cap_usd=0.0))
    )
    assert not verdict
    assert any("unverified ceiling" in r for r in verdict.reasons)


def test_ordinary_unenriched_filings_are_untouched():
    """The tightening must not cost the ordinary case, which has no market cap either."""
    from whale_agent.enrichment.plausibility import check_event

    assert check_event(
        make_event(share_count=65_000.0, price_used=83.05, usd_value=5_398_250.0)
    )


def test_a_take_private_near_the_whole_company_still_passes():
    """Pinned again at this layer: the rigid check must not eat a legitimate outlier."""
    from whale_agent.enrichment.plausibility import check_event

    assert check_event(
        make_event(
            usd_value=90_000_000.0,
            share_count=9_800_000.0,
            shares_outstanding=10_000_000.0,
            context=EventContext(market_cap_usd=94_300_000.0),
        )
    )


def test_more_shares_than_exist_is_rejected_without_any_dollar_figure():
    """The percentage-threshold path has no dollar value and often no market cap."""
    from whale_agent.enrichment.plausibility import check_event

    verdict = check_event(
        make_event(share_count=50_000_000.0, shares_outstanding=10_000_000.0, usd_value=None)
    )
    assert not verdict
    assert any("shares outstanding" in r for r in verdict.reasons)


def test_shares_times_price_disagreeing_with_the_stated_value_is_rejected():
    from whale_agent.enrichment.plausibility import check_event

    verdict = check_event(
        make_event(share_count=1_000.0, price_used=50.0, usd_value=90_000_000.0)
    )
    assert not verdict
    assert any("cannot both be right" in r for r in verdict.reasons)


def test_a_nan_value_does_not_slip_past_the_bounds():
    """Every comparison against NaN is False, so bounds written the obvious way pass it."""
    from whale_agent.enrichment.plausibility import check_event

    verdict = check_event(make_event(usd_value=float("nan")))
    assert not verdict
    assert any("finite" in r for r in verdict.reasons)


def test_an_int64_sentinel_share_count_is_rejected():
    from whale_agent.enrichment.plausibility import check_event

    verdict = check_event(make_event(share_count=-1.0))
    assert not verdict
    assert any("sentinel" in r for r in verdict.reasons)


# -- item 6: the quarantine log ------------------------------------------------------


@pytest.fixture
def qlog(tmp_path):
    from whale_agent.monitoring.quarantine import QuarantineLog

    return QuarantineLog(str(tmp_path / "quarantine.jsonl"))


def test_screening_writes_every_rejection_to_the_quarantine_log(qlog):
    from whale_agent.enrichment.plausibility import screen

    bad = make_event(
        share_count=40_000_000.0, price_used=40_000_000.0, usd_value=1_600_000_000_000_000.0
    )
    kept, rejected = screen([bad], sink=qlog)

    assert kept == [] and len(rejected) == 1
    records = qlog.records_since(NOW - timedelta(days=1))
    assert records
    assert all(r.stage == "plausibility" for r in records)
    # The vendor inputs travel with the record; without them the log says a row was
    # rejected and not that FMP put 40,000,000 in the price field again.
    assert records[0].inputs["price_used"] == 40_000_000.0


def test_screening_without_a_sink_touches_no_files(tmp_path):
    """The default stays pure: no clock, no file handle, no operator record."""
    from whale_agent.enrichment.plausibility import screen

    screen([make_event(issuer_name="NONE")])
    assert list(tmp_path.iterdir()) == []


def test_a_kept_event_is_never_quarantined(qlog):
    from whale_agent.enrichment.plausibility import screen

    kept, rejected = screen(
        [make_event(share_count=65_000.0, price_used=83.05, usd_value=5_398_250.0)],
        sink=qlog,
    )
    assert len(kept) == 1 and not rejected
    assert qlog.records_since(NOW - timedelta(days=1)) == []


def test_the_summary_groups_by_stage_and_source(qlog):
    qlog.record("ingest", "price of 40,000,000 is implausible", source="fmp_insider")
    qlog.record(
        "egress", "position value of 5 exceeds any real disclosure", source="fmp_insider"
    )
    summary = qlog.summary(hours=24)
    assert summary.total == 2
    assert summary.by_stage == {"ingest": 1, "egress": 1}
    assert summary.by_source == {"fmp_insider": 2}


def test_one_egress_block_is_always_an_alert(qlog):
    """A row caught at the send gate means every upstream check missed it."""
    from whale_agent.monitoring.quarantine import alerting_reasons

    qlog.record("egress", "implausible figure in rendered text")
    problems = alerting_reasons(qlog.summary(hours=24))
    assert len(problems) == 1
    assert "EGRESS BLOCK" in problems[0]


def test_a_handful_of_upstream_quarantines_is_not_an_alert(qlog):
    """The guardrail working is not a fault, and alerting on it trains people to ignore it."""
    from whale_agent.monitoring.quarantine import alerting_reasons

    for _ in range(3):
        qlog.record("plausibility", "per-share price is implausible", source="fmp_insider")
    assert alerting_reasons(qlog.summary(hours=24)) == []


def test_a_spike_in_quarantines_is_an_alert(qlog):
    from whale_agent.monitoring.quarantine import QUARANTINE_SPIKE_THRESHOLD, alerting_reasons

    for _ in range(QUARANTINE_SPIKE_THRESHOLD + 1):
        qlog.record("plausibility", "per-share price is implausible", source="fmp_insider")
    problems = alerting_reasons(qlog.summary(hours=24))
    assert any("vendor schema change" in p for p in problems)
    assert any("fmp_insider" in p for p in problems)


def test_records_outside_the_window_are_not_counted(qlog):
    qlog.record("plausibility", "old news")
    old = qlog.summary(hours=24, now=datetime.now(UTC) + timedelta(days=30))
    assert old.total == 0


def test_a_dropped_row_is_never_silent():
    """Decision on record: drop the row, send the rest, and say the row is missing."""
    from whale_agent.monitoring.quarantine import coverage_note

    assert coverage_note(0) is None
    note = coverage_note(2)
    assert "2 filings withheld" in note
    assert coverage_note(1).startswith("1 filing withheld")


def test_a_corrupt_line_does_not_blind_the_log(tmp_path):
    """A half-written final line after a kill must not hide the records above it."""
    from whale_agent.monitoring.quarantine import QuarantineLog

    path = tmp_path / "q.jsonl"
    log = QuarantineLog(str(path))
    log.record("plausibility", "a real rejection")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"stage": "plausibi\n')
    assert log.summary(hours=24).total == 1


def test_the_watchdog_reports_quarantine_activity(tmp_path):
    from whale_agent.jobs.watchdog import build_report
    from whale_agent.monitoring.quarantine import QuarantineLog
    from whale_agent.monitoring.sampled_review import ReviewLog
    from whale_agent.storage.db import Store

    qlog = QuarantineLog(str(tmp_path / "q.jsonl"))
    qlog.record("egress", "implausible figure reached the send gate")
    store = Store(":memory:")
    try:
        report, unhealthy = build_report(
            store,
            quarantine_log=qlog,
            review_log=ReviewLog(str(tmp_path / "s.jsonl"), str(tmp_path / "r.jsonl")),
        )
    finally:
        store.close()
    assert unhealthy
    assert "EGRESS BLOCK" in report


def test_the_watchdog_notes_quiet_quarantine_activity_without_failing(tmp_path):
    from whale_agent.jobs.watchdog import build_report
    from whale_agent.monitoring.quarantine import QuarantineLog
    from whale_agent.monitoring.sampled_review import ReviewLog
    from whale_agent.storage.db import Store

    qlog = QuarantineLog(str(tmp_path / "q.jsonl"))
    qlog.record("plausibility", "one bad row", source="fmp_insider")
    store = Store(":memory:")
    on = date.today()
    store.record_digest_run(on, "daily", "smtp", True, "sent")
    try:
        report, unhealthy = build_report(
            store,
            on,
            quarantine_log=qlog,
            review_log=ReviewLog(str(tmp_path / "s.jsonl"), str(tmp_path / "r.jsonl")),
        )
    finally:
        store.close()
    assert not unhealthy
    assert "healthy" in report
    assert "QUARANTINE" in report


# -- item 7: the advisory verifier ---------------------------------------------------


class StubProvider:
    name = "stub"

    def __init__(self, response: str = "") -> None:
        self.response = response
        self.seen: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.seen.append((system, user))
        return self.response


def test_the_verifier_parses_a_structured_verdict():
    from whale_agent.summarization.verifier import verify_digest

    provider = StubProvider(
        '{"verdict": "KILL", "implausible_figures": ["$1.6 quadrillion in MET"],'
        ' "unsourced_claims": [], "reasoning": "Exceeds global market cap."}'
    )
    verdict = verify_digest(
        "Vanguard disclosed $1,600,000,000,000,000 in MET.", provider=provider
    )
    assert verdict.verdict == "KILL"
    assert verdict.implausible_figures == ("$1.6 quadrillion in MET",)


def test_the_verifier_sees_only_the_rendered_digest():
    """Fresh context: a reviewer holding the writer's reasoning inherits its blind spots."""
    from whale_agent.summarization.verifier import VERIFIER_SYSTEM, verify_digest

    provider = StubProvider('{"verdict": "PASS", "reasoning": "fine"}')
    verify_digest("A rendered digest.", provider=provider)
    system, user = provider.seen[0]
    assert system == VERIFIER_SYSTEM
    assert user == "A rendered digest."


def test_an_unusable_response_becomes_revise_not_pass():
    from whale_agent.summarization.verifier import verify_digest

    verdict = verify_digest("text", provider=StubProvider("I think it looks fine!"))
    assert verdict.verdict == "REVISE"
    assert verdict.error


def test_an_unrecognised_verdict_string_becomes_revise():
    from whale_agent.summarization.verifier import parse_verdict

    verdict = parse_verdict('{"verdict": "APPROVED", "reasoning": "ok"}')
    assert verdict.verdict == "REVISE"


def test_a_transport_failure_becomes_revise():
    from whale_agent.errors import SourceUnavailableError
    from whale_agent.summarization.verifier import verify_digest

    class Broken(StubProvider):
        def complete(self, system: str, user: str) -> str:
            raise SourceUnavailableError("HTTP 500")

    verdict = verify_digest("text", provider=Broken())
    assert verdict.verdict == "REVISE"
    assert "HTTP 500" in (verdict.error or "")


def test_an_unconfigured_verifier_does_not_pass_by_default():
    from whale_agent.config import Settings
    from whale_agent.summarization.verifier import verify_digest

    verdict = verify_digest("text", settings=Settings(llm_provider="none"))
    assert verdict.verdict == "REVISE"


def test_the_verifier_cannot_approve_what_the_code_layer_rejected():
    """The hard rule. A model PASS over a deterministic rejection changes nothing."""
    from whale_agent.summarization.verifier import DigestVerdict, advisory_notes

    notes = advisory_notes(
        DigestVerdict("PASS", reasoning="looks fine"), deterministic_ok=False
    )
    assert len(notes) == 1
    assert "does not change that" in notes[0]
    assert "was not consulted" in notes[0]


def test_the_verifier_exposes_no_send_decision():
    """There is deliberately no function here that returns permission to send."""
    from whale_agent.summarization import verifier

    assert not [n for n in dir(verifier) if "should_send" in n or "approve" in n]
    assert verifier.DigestVerdict("PASS").advisory is True


def test_advisory_notes_surface_specifics_when_the_code_layer_passed():
    from whale_agent.summarization.verifier import DigestVerdict, advisory_notes

    notes = advisory_notes(
        DigestVerdict("REVISE", unsourced_claims=("sector-wide buying",)),
        deterministic_ok=True,
    )
    assert any("unsourced claim" in n for n in notes)
    assert all(n.startswith("ADVISORY") for n in notes)


# -- the advisory pass: wiring verify_digest to the quarantine record ----------------


def test_run_advisory_pass_is_a_no_op_with_no_provider_configured():
    """The advisory pass must degrade to nothing when the LLM is unconfigured, not error."""
    from whale_agent.config import Settings
    from whale_agent.summarization.verifier import run_advisory_pass

    verdict = run_advisory_pass("A rendered digest.", settings=Settings(llm_provider="none"))
    assert verdict.verdict == "REVISE"
    assert verdict.error == "no verifier provider configured"


def test_run_advisory_pass_records_findings_to_the_quarantine_log(qlog):
    """Findings land in the quarantine record under the advisory stage, never as a block."""
    from whale_agent.monitoring.quarantine import STAGE_ADVISORY
    from whale_agent.summarization.verifier import run_advisory_pass

    provider = StubProvider(
        '{"verdict": "KILL", "implausible_figures": ["$1.6 quadrillion in MET"],'
        ' "unsourced_claims": [], "reasoning": "Exceeds global market cap."}'
    )
    verdict = run_advisory_pass(
        "Vanguard disclosed $1,600,000,000,000,000 in MET.",
        provider=provider,
        quarantine_log=qlog,
    )
    assert verdict.verdict == "KILL"
    records = qlog.records_since(NOW - timedelta(days=1))
    assert records
    assert all(r.stage == STAGE_ADVISORY for r in records)
    assert any("$1.6 quadrillion" in r.reason for r in records)


def test_run_advisory_pass_never_records_when_deterministic_already_rejected(qlog):
    """A model opinion over a code-layer refusal is discarded, and so is any noise from it."""
    from whale_agent.summarization.verifier import run_advisory_pass

    provider = StubProvider('{"verdict": "PASS", "reasoning": "looks fine"}')
    run_advisory_pass(
        "text",
        provider=provider,
        deterministic_ok=False,
        quarantine_log=qlog,
    )
    records = qlog.records_since(NOW - timedelta(days=1))
    assert len(records) == 1
    assert "was not consulted" in records[0].reason


def test_run_advisory_pass_survives_a_broken_quarantine_sink():
    """Recording is best-effort: a sink that raises must not cost the caller the verdict."""
    from whale_agent.summarization.verifier import run_advisory_pass

    class BrokenSink:
        def record(self, *args, **kwargs):
            raise OSError("disk full")

    provider = StubProvider('{"verdict": "KILL", "implausible_figures": ["x"]}')
    verdict = run_advisory_pass("text", provider=provider, quarantine_log=BrokenSink())
    assert verdict.verdict == "KILL"


def test_advisory_findings_do_not_count_toward_the_quarantine_spike_alert(qlog):
    """Advisory records are the model's opinion, not evidence a check failed upstream."""
    from whale_agent.monitoring.quarantine import QUARANTINE_SPIKE_THRESHOLD, alerting_reasons

    for _ in range(QUARANTINE_SPIKE_THRESHOLD + 5):
        qlog.record("advisory", "verifier flagged a figure")
    assert alerting_reasons(qlog.summary(hours=24)) == []


# -- item 8: sampled review ----------------------------------------------------------


@pytest.fixture
def reviews(tmp_path):
    from whale_agent.monitoring.sampled_review import ReviewLog

    return ReviewLog(str(tmp_path / "sent.jsonl"), str(tmp_path / "reviews.jsonl"))


def test_sampling_is_deterministic():
    """The same digest is always in or always out, so the queue is stable across reruns."""
    from whale_agent.monitoring.sampled_review import is_sampled

    first = [is_sampled(f"digest-{i}", 0.5) for i in range(50)]
    second = [is_sampled(f"digest-{i}", 0.5) for i in range(50)]
    assert first == second
    assert 0 < sum(first) < 50  # neither everything nor nothing


def test_every_segment_gets_reviewed_even_when_it_is_tiny(reviews):
    """Aggregate accuracy masks segment failure: a rare jurisdiction is not averaged away."""
    for i in range(40):
        reviews.record_sent(f"us-{i}", "daily", jurisdictions=["US"], rate=0.0)
    reviews.record_sent("tw-1", "daily", jurisdictions=["TW"], rate=0.0)

    pending = reviews.pending_reviews()
    strata = {s for d in pending for s in d.strata}
    assert "daily/TW" in strata
    assert "daily/US" in strata


def test_a_reviewed_digest_leaves_the_queue(reviews):
    reviews.record_sent("d-1", "daily", jurisdictions=["US"], rate=1.0)
    assert [d.digest_id for d in reviews.pending_reviews()] == ["d-1"]
    reviews.record_review("d-1", "ok", reviewer="operator")
    assert reviews.pending_reviews() == []


def test_an_unrecognised_verdict_is_recorded_as_unclear_not_lost(reviews):
    rec = reviews.record_review("d-1", "looks great", notes="no idea")
    assert rec.verdict == "unclear"
    assert rec.notes == "no idea"


def test_coverage_is_reported_per_segment(reviews):
    reviews.record_sent("d-1", "daily", jurisdictions=["US", "TW"], rate=1.0)
    reviews.record_review("d-1", "ok")
    coverage = reviews.coverage()
    assert coverage["daily/US"] == {"sent": 1, "selected": 1, "reviewed": 1}
    assert coverage["daily/TW"] == {"sent": 1, "selected": 1, "reviewed": 1}


def test_a_fresh_review_queue_is_not_a_problem(reviews):
    from whale_agent.monitoring.sampled_review import review_backlog_reasons

    reviews.record_sent("d-1", "daily", jurisdictions=["US"], rate=1.0)
    assert review_backlog_reasons(reviews.pending_reviews()) == []


def test_an_ignored_review_queue_is_surfaced(reviews):
    from whale_agent.monitoring.sampled_review import REVIEW_STALE_DAYS, review_backlog_reasons

    reviews.record_sent("d-1", "daily", jurisdictions=["US"], rate=1.0)
    later = datetime.now(UTC) + timedelta(days=REVIEW_STALE_DAYS + 1)
    pending = reviews.pending_reviews(days=365, now=later)
    problems = review_backlog_reasons(pending, now=later)
    assert problems and "REVIEW QUEUE" in problems[0]


def test_berkshire_a_survives_the_price_band():
    """The band named Berkshire A as its example and then excluded it.

    BRK.A trades around $700,000 a share, which is six figures, not five. Insider and
    superinvestor filings on it are real, so a ceiling of $10,000 quietly dropped the
    single most famous holding in the dataset. The band still has to catch the two
    observed incidents: FMP price fields of 40,000,000 and 2,110,482.
    """
    from datetime import date as _d

    from whale_agent.enrichment.plausibility import check_event
    from whale_agent.models.context import EventContext
    from whale_agent.models.enums import Jurisdiction, TransactionType
    from whale_agent.models.event import NormalizedEvent

    def _ev(shares, price, value):
        return NormalizedEvent(
            jurisdiction=Jurisdiction.US,
            source="fmp_insider",
            issuer_name="Co",
            filer_name="F",
            transaction_type=TransactionType.OPEN_MARKET_BUY,
            transaction_date=_d(2026, 7, 20),
            disclosure_date=_d(2026, 7, 21),
            share_count=shares,
            price_used=price,
            usd_value=value,
            context=EventContext(),
        )

    assert check_event(_ev(10.0, 700_000.0, 7_000_000.0)).plausible
    # Both real incidents stay blocked.
    assert not check_event(_ev(40_000_000.0, 40_000_000.0, 1.6e15)).plausible
    assert not check_event(_ev(3_523.0, 2_110_482.0, 7.4e9)).plausible


def test_the_evidence_block_claims_only_the_classification_we_actually_apply():
    """`is_routine` was read in seven places and set in none.

    The block once said "Every row here is classified on that basis", which was false on
    every digest sent; the claim was withdrawn, and is now restored because the pipeline
    genuinely applies the classifier. If the wiring is ever removed, this fails.
    """
    import inspect

    from whale_agent.jobs import pipeline
    from whale_agent.jobs.digest_weekly import HOW_TO_READ
    from whale_agent.models.event import NormalizedEvent

    # The classifier is wired, so the copy may describe it. What the copy may not do is
    # promise a split the data cannot deliver: no filer on record has more than one year
    # of disclosure dates, and the test needs three, so today nothing is classified.
    assert "mark_routine" in inspect.getsource(pipeline)
    assert "classified on that basis where" in HOW_TO_READ
    assert "unclassified rather than as opportunistic" in HOW_TO_READ
    assert NormalizedEvent.model_fields["is_routine"].default is False


# --- A 13F portfolio is legitimately above the per-disclosure ceiling ----------------


def test_a_huge_figure_is_still_blocked_by_default():
    """The guard that caught the $1.6 quadrillion row must keep working."""
    from whale_agent.delivery.email import find_egress_violations

    assert find_egress_violations("Berkshire holds $263.1B")


def test_a_declared_aggregate_is_allowed_through():
    from whale_agent.delivery.email import find_egress_violations

    assert not find_egress_violations("Berkshire holds $263.1B", allow_figures={"$263.1B"})


def test_declaring_one_aggregate_does_not_permit_a_different_one():
    """The allowlist is exact tokens, not a raised ceiling."""
    from whale_agent.delivery.email import find_egress_violations

    violations = find_egress_violations(
        "Berkshire holds $263.1B and something holds $999.9B",
        allow_figures={"$263.1B"},
    )
    assert len(violations) == 1
    assert "$999.9B" in violations[0]


def test_the_tracked_section_declares_exactly_what_it_prints():
    from datetime import date

    from whale_agent.delivery.email import find_egress_violations
    from whale_agent.ingestion.fund_watchlist import FundSnapshot, Holding
    from whale_agent.summarization.tracked_funds import (
        aggregate_figure_tokens,
        render_tracked_funds,
    )

    snap = FundSnapshot(
        name="Berkshire Hathaway",
        cik="0001067983",
        latest_13f_filed=date(2026, 5, 15),
        total_value_usd=263_100_000_000,
        position_count=26,
        top_holdings=[Holding("APPLE INC", 57_800_000_000, None)],
    )
    body = "\n".join(render_tracked_funds([snap], date(2026, 8, 3)))
    # Without the declaration the real section is blocked, which is what happened.
    assert find_egress_violations(body)
    # With it, nothing is.
    assert not find_egress_violations(body, allow_figures=aggregate_figure_tokens([snap]))


def test_the_overview_button_is_omitted_rather_than_pointing_at_a_dead_path():
    """A file:// link from a CI runner resolves nowhere for any reader."""
    from datetime import date

    from whale_agent.summarization.render_weekly_html import render_weekly_html

    html = render_weekly_html(
        on=date(2026, 8, 3),
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        issue_link="",
    )
    assert "file://" not in html
    assert "Read the weekly overview" not in html

    linked = render_weekly_html(
        on=date(2026, 8, 3),
        cleared=1,
        recorded=1,
        articles=[],
        sections=[],
        how_to_read="",
        issue_link="https://example.workers.dev/issue.html",
    )
    assert "Read the weekly overview" in linked


def test_no_module_defines_the_same_function_twice():
    """A duplicate definition silently wins and the earlier one never runs.

    `_tracked_funds_block` was defined twice in render_weekly_html.py on 2026-08-03. The
    rewritten version sat above the stale one, Python bound the stale one, and the email
    rendered old copy describing content that had been removed. Tests all passed.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "whale_agent"
    offenders = []
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            names = [
                n.name
                for n in scope.body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            offenders += [f"{path.name}:{n}" for n in names if names.count(n) > 1]
    assert not offenders, f"duplicate definitions: {sorted(set(offenders))}"
