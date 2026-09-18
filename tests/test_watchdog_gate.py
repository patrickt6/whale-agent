"""The watchdog gate is what makes a core-source outage visible.

`collect_events` deliberately never lets a failing source fail the run (a missing API
key is not an error), so nothing in the ingest job itself will ever exit non-zero for a
source that is down. sec_form4 and sec_13dg both failed on every run for a week because
of this: the only thing that could have caught it is a job that separately asks "are the
core sources still fresh", and nothing was running that job as part of daily-ingest.

These tests pin the behaviour the CI fix depends on: `gate_report` returns
`blocked=True` once a CORE_SOURCES source has gone stale, and it does *not* trip on a
single missed run (one bad night must not block delivery for two more days).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.conftest import make_event
from whale_agent.jobs.watchdog import gate_report
from whale_agent.storage.db import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "watchdog.db"))


def _seed_window(store: Store, on) -> None:
    """Enough recent events that `thin_window` does not itself block the gate --
    these tests are about source freshness, not window volume."""
    store.upsert_many(
        [
            make_event(
                filer_name=f"Filer {i}",
                transaction_date=on,
                disclosure_date=on,
            )
            for i in range(120)
        ]
    )


def test_gate_passes_when_core_sources_are_fresh(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(UTC)
    for name in ("sec_form4", "sec_13dg", "fmp_insider"):
        store.record_source_run(name, ok=True, event_count=5, at=now)
    _seed_window(store, now.date())
    report, blocked = gate_report(store, now=now, on=now.date())
    assert blocked is False
    store.close()


def test_gate_does_not_block_on_one_missed_run(tmp_path):
    """36h of grace (EXPECTED_MAX_AGE_HOURS) covers one bad night without alerting."""
    store = _store(tmp_path)
    now = datetime.now(UTC)
    stale_but_within_grace = now - timedelta(hours=20)
    for name in ("sec_form4", "sec_13dg", "fmp_insider"):
        store.record_source_run(name, ok=True, event_count=5, at=stale_but_within_grace)
    _seed_window(store, now.date())
    report, blocked = gate_report(store, now=now, on=now.date())
    assert blocked is False
    store.close()


def test_gate_blocks_when_a_core_source_has_failed_for_days(tmp_path):
    """This is the exact shape of the sec_form4 / sec_13dg outage: a week of 403s with
    fmp_insider still healthy. The gate must trip on this, not just on total silence."""
    store = _store(tmp_path)
    now = datetime.now(UTC)
    last_good = now - timedelta(hours=173)  # matches the reported outage duration
    store.record_source_run("sec_form4", ok=False, error="403 Forbidden: form.gz", at=now)
    store.record_source_run("sec_form4", ok=True, event_count=3, at=last_good)
    store.record_source_run("sec_13dg", ok=False, error="403 Forbidden: form.gz", at=now)
    store.record_source_run("sec_13dg", ok=True, event_count=1, at=last_good)
    store.record_source_run("fmp_insider", ok=True, event_count=40, at=now)

    report, blocked = gate_report(store, now=now)
    assert blocked is True
    assert "sec_form4" in report
    assert "sec_13dg" in report
    store.close()
