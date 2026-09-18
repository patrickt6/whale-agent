"""Determinism: the same corpus rendered twice must produce byte-identical output.

This is not a hypothetical property. The product's claim rests on every digest being
reconstructible from the same rows, and a render that is not deterministic breaks that
claim quietly -- a diff between two runs over an unchanged corpus would look like a bug
report nobody could reproduce, because "rerun it" would sometimes make it disappear.

The corpus below is built once, frozen, and fed through the daily and weekly renders
twice each. Every explicit `on=`/`today=` argument is required precisely so the render
paths never fall back to `date.today()` -- that fallback exists for real callers, not for
this test, and calling it here would make the test itself the nondeterministic thing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from tests.conftest import make_event
from whale_agent.jobs.overview import build_overview
from whale_agent.models.context import EventContext
from whale_agent.models.enums import Jurisdiction, TransactionType
from whale_agent.scoring.dedup import dedup_events
from whale_agent.scoring.score import rank_events
from whale_agent.summarization.render import render_digest
from whale_agent.summarization.render_html import render_digest_html
from whale_agent.summarization.render_weekly_html import render_weekly_html

ON = date(2026, 7, 25)


def _corpus():
    """A fixed, varied corpus: enough filers, sectors, and jurisdictions that any
    hidden dependence on set/dict iteration order or hash seed has somewhere to hide."""
    return [
        make_event(
            filer_name="Warren Buffett",
            filer_id="F-BUFFETT",
            issuer_name="Occidental Petroleum",
            issuer_id="CIK-OXY",
            ticker="OXY",
            jurisdiction=Jurisdiction.US,
            transaction_type=TransactionType.OPEN_MARKET_BUY,
            usd_value=210_000_000.0,
            context=EventContext(
                market_cap_usd=55_000_000_000.0,
                sector="Energy",
                percent_of_float=1.2,
                filer_prior_filings=40,
            ),
        ),
        make_event(
            filer_name="Jane Fraser",
            filer_id="F-FRASER",
            issuer_name="Citigroup",
            issuer_id="CIK-C",
            ticker="C",
            jurisdiction=Jurisdiction.US,
            transaction_type=TransactionType.OPEN_MARKET_SELL,
            usd_value=8_500_000.0,
            context=EventContext(
                market_cap_usd=120_000_000_000.0,
                sector="Financial Services",
                filer_prior_filings=3,
            ),
        ),
        make_event(
            filer_name="Third Point LLC",
            filer_id="F-THIRDPOINT",
            issuer_name="Sculptor Capital",
            issuer_id="CIK-SCU",
            ticker="SCU",
            jurisdiction=Jurisdiction.US,
            transaction_type=TransactionType.ACTIVIST_13D,
            usd_value=720_000_000.0,
            is_first_time_filer=True,
            context=EventContext(
                market_cap_usd=730_000_000.0,
                sector="Financial Services",
                filer_prior_filings=0,
            ),
        ),
        make_event(
            filer_name="Nomura Holdings",
            filer_id="F-NOMURA",
            issuer_name="Toyota Motor",
            issuer_id="CIK-7203",
            ticker="7203.T",
            jurisdiction=Jurisdiction.JAPAN,
            transaction_type=TransactionType.PASSIVE_13G,
            usd_value=45_000_000.0,
            context=EventContext(
                market_cap_usd=260_000_000_000.0, sector="Industrials", filer_prior_filings=12
            ),
        ),
        make_event(
            filer_name="Cathie Wood",
            filer_id="F-WOOD",
            issuer_name="Tesla",
            issuer_id="CIK-TSLA",
            ticker="TSLA",
            jurisdiction=Jurisdiction.US,
            transaction_type=TransactionType.FUND_NEW_POSITION,
            usd_value=15_500_000.0,
            context=EventContext(
                market_cap_usd=900_000_000_000.0,
                sector="Consumer Cyclical",
                filer_prior_filings=8,
                is_new_position=True,
            ),
        ),
        make_event(
            filer_name="Insider Alpha",
            filer_id="F-ALPHA",
            issuer_name="Micro Devices Co",
            issuer_id="CIK-MICRO",
            ticker="MCRO",
            jurisdiction=Jurisdiction.US,
            transaction_type=TransactionType.GRANT,
            usd_value=6_100_000.0,
            is_routine=True,
            context=EventContext(market_cap_usd=310_000_000.0, sector="Technology"),
        ),
        make_event(
            filer_name="Insider Beta",
            filer_id="F-BETA",
            issuer_name="Micro Devices Co",
            issuer_id="CIK-MICRO",
            ticker="MCRO",
            jurisdiction=Jurisdiction.US,
            transaction_type=TransactionType.OPEN_MARKET_BUY,
            usd_value=5_900_000.0,
            context=EventContext(market_cap_usd=310_000_000.0, sector="Technology"),
        ),
        make_event(
            filer_name="Insider Gamma",
            filer_id="F-GAMMA",
            issuer_name="Micro Devices Co",
            issuer_id="CIK-MICRO",
            ticker="MCRO",
            jurisdiction=Jurisdiction.US,
            transaction_type=TransactionType.OPEN_MARKET_BUY,
            usd_value=6_050_000.0,
            context=EventContext(market_cap_usd=310_000_000.0, sector="Technology"),
        ),
    ]


def _ranked():
    return rank_events(dedup_events(_corpus()), today=ON)


def test_the_daily_text_digest_is_byte_identical_across_two_runs():
    ranked_a = _ranked()
    ranked_b = _ranked()
    first = render_digest(ranked_a, on=ON)
    second = render_digest(ranked_b, on=ON)
    assert first == second


def test_the_daily_html_digest_is_byte_identical_across_two_runs():
    first = render_digest_html(_ranked(), on=ON)
    second = render_digest_html(_ranked(), on=ON)
    assert first == second


def test_the_weekly_html_report_is_byte_identical_across_two_runs():
    def build():
        ranked = _ranked()
        sections = build_overview(ranked, on=ON)
        return render_weekly_html(
            on=ON,
            cleared=len(ranked),
            recorded=len(ranked),
            articles=[],
            sections=sections,
            how_to_read="",
            no_thesis_line="Nothing to report.",
        )

    assert build() == build()


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUBPROCESS_SCRIPT = (
    "from tests.test_determinism import _corpus, ON\n"
    "from whale_agent.scoring.dedup import dedup_events\n"
    "from whale_agent.scoring.score import rank_events\n"
    "from whale_agent.summarization.render import render_digest\n"
    "ranked = rank_events(dedup_events(_corpus()), today=ON)\n"
    "print(render_digest(ranked, on=ON))\n"
)


def test_scoring_is_stable_regardless_of_pythonhashseed():
    """A hash-order dependence would only show up when the seed actually changes.

    PYTHONHASHSEED randomizes `hash(str)` and therefore the iteration order of any
    `set`/`dict` built from strings unless insertion order is preserved end to end. This
    runs the daily render in a subprocess with a pinned, different seed each time and
    diffs the output against this process's own render over the identical corpus.
    """
    baseline = render_digest(_ranked(), on=ON)
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{_REPO_ROOT / 'src'}{os.pathsep}{_REPO_ROOT}"
    for seed in ("0", "1", "2026"):
        env["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip("\n") == baseline, (
            f"render_digest differs under PYTHONHASHSEED={seed}; "
            f"stderr={result.stderr[-2000:]}"
        )
