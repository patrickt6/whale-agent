"""Live probe of every configured source: do the endpoints and field names still exist?

Golden-file tests prove the parsers handle a response shape we recorded. They cannot
prove the vendor still serves that shape, or that the URL is even right -- the FMP and
Quiver adapters were written against published documentation, without a key to test
against. This job closes that gap: one minimal real request per endpoint, reporting
whether it answered and whether the specific fields the normalizer reads are present.

Run it the day a key is purchased, and after any vendor migration notice.

    python -m whale_agent.jobs.verify_sources          # every configured source
    python -m whale_agent.jobs.verify_sources --fmp    # just one vendor

Exit code is non-zero if any probe failed, so it works as a CI or cron check.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from whale_agent.config import Settings, get_settings, load_env_file
from whale_agent.enrichment.price import FmpPriceProvider
from whale_agent.errors import NotConfiguredError, SourceUnavailableError
from whale_agent.ingestion.vendors.fmp import (
    FmpCongressAdapter,
    FmpInsiderAdapter,
    FmpInstitutionalAdapter,
)
from whale_agent.ingestion.vendors.quiver import QuiverCongressAdapter

log = logging.getLogger(__name__)


@dataclass
class Probe:
    """One endpoint check and what it found."""

    source: str
    endpoint: str
    ok: bool
    detail: str = ""
    missing_fields: list[str] = field(default_factory=list)

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        text = f"  [{mark}] {self.endpoint:<34} {self.detail}"
        if self.missing_fields:
            text += f"\n         missing field(s): {', '.join(self.missing_fields)}"
        return text


def _recent_trading_day(today: date | None = None) -> date:
    """A weekday a few days back -- recent enough to exist, old enough to be settled."""
    day = (today or date.today()) - timedelta(days=4)
    while day.weekday() >= 5:  # Sat/Sun
        day -= timedelta(days=1)
    return day


def _check_fields(rows: list[dict], required: tuple[str, ...]) -> list[str]:
    """Which required field names are absent from every row we got back.

    Checked across all rows rather than the first: vendors routinely omit a null field
    rather than sending it, so a single row is not evidence the field is gone.
    """
    if not rows:
        return []
    present: set[str] = set()
    for row in rows:
        present.update(row.keys())
    return [name for name in required if name not in present]


# -- FMP ------------------------------------------------------------------------
def probe_fmp(settings: Settings, ticker: str = "AAPL") -> list[Probe]:
    """Probe every FMP endpoint the agent actually calls."""
    probes: list[Probe] = []
    on = _recent_trading_day()

    # 1. Daily closes. This is the endpoint that makes non-US filings valuable at all.
    price = FmpPriceProvider(settings)
    try:
        quote = price.close_on(ticker, on)
        if quote is None:
            probes.append(
                Probe(
                    "fmp",
                    "historical-price-eod/full",
                    False,
                    f"reachable but returned no bar for {ticker} on or before {on}",
                )
            )
        else:
            probes.append(
                Probe(
                    "fmp",
                    "historical-price-eod/full",
                    True,
                    f"{ticker} close {quote.price} ({quote.source.value} {quote.as_of})",
                )
            )
    except (NotConfiguredError, SourceUnavailableError) as exc:
        probes.append(Probe("fmp", "historical-price-eod/full", False, str(exc)[:160]))

    # 2. Shares outstanding, for percent-of-company filings (Taiwan, Japan, 13D/G).
    try:
        shares = price.shares_outstanding(ticker)
        probes.append(
            Probe(
                "fmp",
                "profile",
                shares is not None,
                f"{ticker} shares outstanding {shares:,.0f}"
                if shares
                else "reachable but no sharesOutstanding/marketCap field found",
            )
        )
    except (NotConfiguredError, SourceUnavailableError) as exc:
        probes.append(Probe("fmp", "profile", False, str(exc)[:160]))

    # 3-5. The three ingestion endpoints.
    for label, adapter, kwargs, required in (
        (
            "insider-trading/search",
            FmpInsiderAdapter(settings),
            {"limit": 5},
            ("symbol", "transactionDate", "transactionType", "securitiesTransacted", "price"),
        ),
        (
            "senate-latest",
            FmpCongressAdapter(settings),
            {"chamber": "senate"},
            ("symbol", "transactionDate", "type", "amount"),
        ),
        (
            "house-latest",
            FmpCongressAdapter(settings),
            {"chamber": "house"},
            ("symbol", "transactionDate", "type", "amount"),
        ),
    ):
        probes.append(_probe_adapter("fmp", label, adapter, kwargs, required))

    # 6. The 13F extract is per-manager: it takes the filer's CIK, not a ticker.
    #    Berkshire Hathaway is used as the probe because its CIK is stable and public.
    #    The quarter has to be one that is actually filed -- 13F is due 45 days after
    #    quarter end, so asking for the current quarter returns an empty list and looks
    #    like a broken endpoint. Going back 135 days lands on the last certain filing.
    filed = on - timedelta(days=135)
    probes.append(
        _probe_adapter(
            "fmp",
            "institutional-ownership/extract",
            FmpInstitutionalAdapter(settings),
            {"cik": "0001067983", "year": filed.year, "quarter": (filed.month - 1) // 3 + 1},
            # The live snapshot schema, confirmed by probe: it carries no delta fields
            # and identifies the filer only by CIK, which is why the per-holder analytics
            # endpoint exists alongside it.
            ("nameOfIssuer", "shares", "value", "symbol"),
        )
    )
    return probes


# -- Quiver ---------------------------------------------------------------------
def probe_quiver(settings: Settings) -> list[Probe]:
    return [
        _probe_adapter(
            "quiver",
            "live/congresstrading",
            QuiverCongressAdapter(settings),
            {},
            ("Ticker", "TransactionDate", "Transaction", "Amount"),
        )
    ]


def _probe_adapter(
    source: str, label: str, adapter, kwargs: dict, required: tuple[str, ...]
) -> Probe:
    """Fetch + parse one endpoint, then report row count and any absent fields."""
    try:
        rows: list[dict] = []
        for raw in adapter.fetch(**kwargs):
            rows.extend(adapter.parse(raw))
    except (NotConfiguredError, SourceUnavailableError) as exc:
        return Probe(source, label, False, str(exc)[:160])
    except Exception as exc:  # an unexpected shape is a finding, not a crash
        return Probe(source, label, False, f"{type(exc).__name__}: {exc}"[:160])

    missing = _check_fields(rows, required)
    if not rows:
        return Probe(source, label, False, "reachable but returned zero rows")
    if missing:
        return Probe(source, label, False, f"{len(rows)} rows", missing)
    return Probe(source, label, True, f"{len(rows)} rows, all expected fields present")


def probe_links(settings: Settings, sample: int = 5) -> list[Probe]:
    """Check that the filing links a vendor hands us actually resolve.

    Every row in the digest carries a "read the filing" link, and that link is the only
    thing letting a reader verify a figure themselves. A dead one silently converts the
    product's central claim into a broken promise -- and nothing else in the test suite
    can catch it, because golden-file tests assert on saved bytes rather than on whether
    a URL is real. So this samples live rows and follows their links.
    """
    import httpx

    adapter = FmpInsiderAdapter(settings)
    try:
        rows: list[dict] = []
        for raw in adapter.fetch(limit=sample):
            rows.extend(adapter.parse(raw))
    except (NotConfiguredError, SourceUnavailableError) as exc:
        return [Probe("links", "source_url sample", False, str(exc)[:160])]

    urls = [r.get("url") for r in rows if r.get("url")][:sample]
    if not urls:
        return [Probe("links", "source_url sample", False, "no rows carried a url field")]

    dead: list[str] = []
    for url in urls:
        try:
            # SEC blocks generic library agents, so identify properly here too.
            resp = httpx.get(
                url,
                headers={"User-Agent": settings.sec_user_agent},
                timeout=settings.http_timeout_seconds,
                follow_redirects=True,
            )
            if resp.status_code >= 400:
                dead.append(f"{resp.status_code} {url}")
        except httpx.HTTPError as exc:
            dead.append(f"{type(exc).__name__} {url}")

    if dead:
        return [
            Probe(
                "links", "source_url sample", False, f"{len(dead)}/{len(urls)} dead: {dead[0]}"
            )
        ]
    return [Probe("links", "source_url sample", True, f"{len(urls)}/{len(urls)} resolve")]


def run_probes(settings: Settings, only: str = "") -> list[Probe]:
    probes: list[Probe] = []
    if (not only or only == "fmp") and settings.fmp_enabled:
        probes.extend(probe_fmp(settings))
    if (not only or only == "quiver") and settings.quiver_enabled:
        probes.extend(probe_quiver(settings))
    if (not only or only == "links") and settings.fmp_enabled:
        probes.extend(probe_links(settings))
    return probes


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe live vendor endpoints")
    ap.add_argument("--fmp", action="store_true", help="probe FMP only")
    ap.add_argument("--quiver", action="store_true", help="probe Quiver only")
    ap.add_argument("--links", action="store_true", help="check filing links resolve")
    ap.add_argument("--ticker", default="AAPL", help="ticker to probe prices with")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    load_env_file()
    settings = get_settings()
    only = "fmp" if args.fmp else "quiver" if args.quiver else "links" if args.links else ""

    if not settings.fmp_enabled and only in ("", "fmp"):
        print("FMP: no key set (FMP_API_KEY) — nothing to probe.")
    if not settings.quiver_enabled and only in ("", "quiver"):
        print("Quiver: no key set (QUIVER_QUANT_API_KEY) — nothing to probe.")

    probes = run_probes(settings, only)
    if not probes:
        raise SystemExit(0)

    current = ""
    for probe in probes:
        if probe.source != current:
            current = probe.source
            print(f"\n{current.upper()}")
        print(probe.line())

    failed = [p for p in probes if not p.ok]
    print(f"\n{len(probes) - len(failed)}/{len(probes)} endpoints healthy.")
    if failed:
        print("Failing endpoints need their path or field mapping corrected in the adapter.")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
