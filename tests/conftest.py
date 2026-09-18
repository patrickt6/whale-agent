"""Shared test fixtures."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from whale_agent.models.enums import Jurisdiction, TransactionType
from whale_agent.models.event import NormalizedEvent

FIXTURES = Path(__file__).parent / "fixtures"

# Prefixes covering every variable `Settings.from_env` reads.
_ENV_PREFIXES = (
    "WHALE_",
    "FMP_",
    "QUIVER_",
    "TAIWAN_",
    "JAPAN_",
    "ARKHAM_",
    "DATAROMA_",
    "GEMINI_",
    "ANTHROPIC_",
    "SMTP_",
    "RESEND_",
    "TELEGRAM_",
    "TWILIO_",
    "SLACK_",
    "OPERATOR_",
    "DIGEST_",
    "DATABASE_URL",
    # Free government sources. Missing these let a real key in the developer's shell
    # leak into the suite and switch on a source the tests assume is off.
    "FRED_",
    "SENATE_",
    "TREASURY_",
    "USASPENDING_",
)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def clean_env():
    """Run every test against a known-empty configuration, and restore afterwards.

    Two problems this solves. First, a developer with a real `.env` exported in their
    shell would otherwise get different results from CI -- a live FMP key would switch on
    sources the tests assume are off. Second, `load_env_file` writes directly into
    `os.environ`, which `monkeypatch` cannot undo because it did not set those keys; one
    test lowering `WHALE_THRESHOLD_USD` would silently disable the $5M gate for every
    test that ran after it.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIXES)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith(_ENV_PREFIXES)]:
            del os.environ[key]
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def isolated_monitoring(tmp_path, monkeypatch):
    """Point the quarantine and sampled-review logs at a per-test directory.

    These logs are files rather than database rows, and their default location is the
    repository root. Without this fixture a test that quarantines a row would append to
    the operator's real record, and a watchdog test would report whatever happened to be
    sitting in that file. Both process-wide singletons are cleared as well, since a log
    resolved in one test would otherwise keep its path for the rest of the session.
    """
    from whale_agent.monitoring import quarantine, sampled_review

    monkeypatch.setenv("WHALE_MONITORING_DIR", str(tmp_path / "monitoring"))
    quarantine.set_default_log(None)
    sampled_review.set_default_review_log(None)
    try:
        yield tmp_path / "monitoring"
    finally:
        quarantine.set_default_log(None)
        sampled_review.set_default_review_log(None)


def make_event(**overrides) -> NormalizedEvent:
    """Build a NormalizedEvent with sensible defaults for scoring/dedup tests.

    filer_id/issuer_id default to a slug of the name so distinct-named filers do not
    accidentally collide on the dedup key. Tests that probe dedup pass explicit ids.
    """
    filer_name = overrides.get("filer_name", "Test Filer")
    base = dict(
        jurisdiction=Jurisdiction.US,
        source="test",
        issuer_name="TestCo",
        issuer_id="CIK123",
        filer_name=filer_name,
        filer_id="CIK-" + filer_name.lower().replace(" ", "-"),
        transaction_type=TransactionType.OPEN_MARKET_BUY,
        transaction_date=date(2026, 7, 24),
        disclosure_date=date(2026, 7, 25),
        usd_value=10_000_000.0,
    )
    base.update(overrides)
    return NormalizedEvent(**base)
