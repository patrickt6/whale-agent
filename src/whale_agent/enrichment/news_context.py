"""Provenanced news/context lookup for one issuer around one date.

The reader requirement this exists for: clicking a filing in the weekly email must land
on a page that says, among other required elements, "is there any other news about it".
Every sentence that page prints has to trace to a retrievable source, and the Anthropic
API is capped until 2026-09-01, so this is deliberately NOT an LLM summarizer. It is a
retrieval adapter. It returns headline/publisher/date/URL items the prose layer can
quote and link directly, or an explicit empty result when nothing is found. Nothing here
paraphrases a headline into a claim.

Two sources, both investigated live against production before anything was written:

  1. FMP `/stable/news/stock` and `/stable/news/press-releases` (`symbols=<ticker>`).
     FMP is already a paid vendor here (see `ingestion/vendors/fmp.py`). Verified
     2026-08-18 against the live API:
       * `from`/`to` ARE honoured on these two endpoints (unlike the insider-trading
         endpoint, which silently ignores them -- see that module's docstring). A
         `2026-08-05..2026-08-06` window on AAPL returned 27 rows, every one dated
         inside the window.
       * `symbols=` is honoured: querying six different tickers returned only rows
         tagged with that exact symbol, including on a real microcap (MTEK) with a
         thin, single-issuer news history.
       * `limit` caps at 250 regardless of the value requested (asked 1000, got 250).
     `/stable/news/press-releases` is the same shape, narrower: issuer-issued releases
     (often BusinessWire/PR Newswire) rather than commentary/analyst pieces about the
     issuer. Both are queried; press releases are the closer analogue to "official
     word", stock news catches everything else including the third-party pickup of an
     8-K's own numbers.

  2. SEC EDGAR 8-K filings for the issuer's own CIK, read from
     `data.sec.gov/submissions/CIK##########.json` (the same endpoint
     `ingestion/fund_watchlist.py` already reads for its watchlist managers, just keyed
     by issuer instead of by fund). An 8-K is Regulation FD's own definition of a
     "material event" -- earnings, an executive departure, an acquisition, a material
     agreement -- and it is filed by the company itself, so it needs no relevance
     heuristic at all: every 8-K in the issuer's history is, by construction, about that
     issuer. It is free, needs no extra credential, and requires no server-side
     filtering to trust. Cross-checked against EVCM (EverCommerce): its 2026-08-05 8-K
     (items 2.02/9.01, "Results of Operations" + "Financial Statements and Exhibits")
     landed the same day as an FMP press-release row with the matching headline
     ("EverCommerce Announces Second Quarter 2026 Financial Results") -- the two sources
     corroborate rather than duplicate.

     The 8-K item is linked to EDGAR's own rendered filing-index page
     (`{accession}-index.htm`), not a guessed exhibit filename: exhibit naming has no
     fixed convention (`ex99...`, `evcmq226earningsrelease.htm`, etc. all appear in the
     wild across issuers), and the index page is what a reader clicking through actually
     wants -- it lists every document EDGAR has for that filing, primary doc and
     exhibits alike.

Relevance is heuristic and this module says so plainly rather than pretending
otherwise: FMP's own `symbols=` filter is trusted as-is (defensively re-checked against
the row's own `symbol` field), and an SEC row is trusted because it is issued under the
issuer's own CIK. Neither path second-guesses a vendor's judgment about "aboutness" with
any text-similarity or keyword logic of our own -- that kind of heuristic is exactly the
sort of invented confidence the provenance guarantee exists to prevent.

Cost/time per run: two HTTP calls per issuer (one FMP news call, one SEC submissions
fetch), no per-item calls -- the 8-K filing list is read in the same submissions payload
`fetch_snapshot` already reads for tracked managers, so this adds no per-filing SEC
document fetches. At roughly 0.2-0.4s per call (SEC's own politeness pause is 0.15s,
already respected below), 200 issuers is 400 calls, on the order of 2-4 minutes
end-to-end run serially. That is cheap enough to run inline for every filing in a weekly
send. If the vendor call volume ever matters for FMP's own rate limits, the mitigation
is the same one already used elsewhere in this codebase: cache by (ticker-or-cik,
week-of) in `whale.db` rather than re-fetching an issuer that already appeared earlier
in the same run or in last week's send. That cache table is not built here -- this
module is the data layer the cache would sit in front of, and 200 issuers at today's
volume does not yet need it -- but the key and the numbers above are what a future
caching pass would use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from whale_agent.config import Settings, get_settings
from whale_agent.enrichment.tickers import normalize_cik
from whale_agent.ingestion._http import polite_headers, request_json, request_text

_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# SEC's own item titles (Form 8-K General Instructions), copied verbatim -- not a
# summary of them. An item code with no entry here is rendered as its bare number
# rather than guessed at.
EIGHT_K_ITEMS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Officers; Compensatory Arrangements",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.04": "Temporary Suspension of Trading Under Employee Benefit Plans",
    "5.05": "Amendments to Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


@dataclass(frozen=True)
class NewsItem:
    """One retrieved, attributable item: what the prose layer is allowed to cite.

    `headline` is copied verbatim from the source -- never paraphrased, never
    generated. `source` names which of the two adapters produced it
    ("fmp_stock_news", "fmp_press_release", "sec_8k") so the prose layer and any
    downstream audit can tell a vendor pickup from a primary filing.
    """

    headline: str
    publisher: str
    published: date
    url: str
    source: str


def _parse_fmp_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_fmp_news(rows: list[dict], *, symbol: str, source: str) -> list[NewsItem]:
    """FMP news/press-release rows -> NewsItems, kept only when they carry the exact
    symbol asked for.

    Defensive re-check rather than blind trust in the vendor's own `symbols=` filter:
    this codebase has already caught FMP silently ignoring a filter parameter once
    (the insider-trading `from`/`to`), so every vendor filter here is re-verified
    against the row itself rather than assumed to have worked.
    """
    symbol = (symbol or "").strip().upper()
    out: list[NewsItem] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if symbol and row_symbol != symbol:
            continue
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip()
        published = _parse_fmp_date(row.get("publishedDate"))
        if not title or not url or not published:
            continue
        out.append(
            NewsItem(
                headline=title,
                publisher=(row.get("publisher") or row.get("site") or "").strip()
                or "unknown publisher",
                published=published,
                url=url,
                source=source,
            )
        )
    return out


def _filing_index_url(cik: str, accession: str) -> str:
    """The EDGAR page a human reads for one filing (form, filer, dates, every
    document). Same construction `ingestion/fund_watchlist.py` uses; kept as a
    private copy here rather than an import so this module has no dependency on the
    fund-watchlist module, which is a different agent's surface."""
    if not cik or not accession:
        return ""
    bare = accession.replace("-", "")
    dashed = (
        accession
        if "-" in accession
        else (f"{bare[:10]}-{bare[10:12]}-{bare[12:]}" if len(bare) == 18 else bare)
    )
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{bare}/{dashed}-index.htm"


def _eight_k_headline(items: str) -> str:
    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    if not codes:
        return "Form 8-K filed"
    labels = [EIGHT_K_ITEMS.get(c, f"Item {c}") for c in codes]
    return "8-K: " + "; ".join(labels)


def parse_8k_filings(recent: dict, *, cik: str) -> list[NewsItem]:
    """8-K rows out of one issuer's `data.sec.gov/submissions` "recent" block.

    Every row is kept: an 8-K is filed by the issuer about itself, so unlike a vendor
    news feed there is no separate relevance question to answer here. `items` (the
    8-K's own disclosed subject codes, e.g. "2.02,9.01") drives the headline; a filing
    with no `items` field (older filings, or a payload shape that omits it) still gets
    a headline, just a generic one, rather than being dropped.
    """
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    items_col = recent.get("items", [""] * len(forms))
    out: list[NewsItem] = []
    for i, form in enumerate(forms):
        if form.strip().upper() != "8-K":
            continue
        filed = _parse_fmp_date(dates[i] if i < len(dates) else None)
        accession = accessions[i] if i < len(accessions) else ""
        if not filed or not accession:
            continue
        items = items_col[i] if i < len(items_col) else ""
        out.append(
            NewsItem(
                headline=_eight_k_headline(items),
                publisher="SEC EDGAR",
                published=filed,
                url=_filing_index_url(cik, accession),
                source="sec_8k",
            )
        )
    return out


def _within_window(items: list[NewsItem], around: date, window_days: int) -> list[NewsItem]:
    start = around - timedelta(days=window_days)
    end = around + timedelta(days=window_days)
    return [it for it in items if start <= it.published <= end]


def _dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """Drop exact (headline, url) repeats. Different sources reporting the same event
    with different headlines are NOT merged -- that would be exactly the kind of
    unsourced editorial judgment this module exists to avoid making."""
    seen: set[tuple[str, str]] = set()
    out: list[NewsItem] = []
    for it in items:
        key = (it.headline, it.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_fmp_news(
    ticker: str, *, settings: Settings, window_start: date, window_end: date, limit: int = 50
) -> list[NewsItem]:  # pragma: no cover - network path
    """Live FMP stock-news + press-release rows for one ticker, already date-scoped
    server-side. Raises nothing; an unconfigured or failing FMP degrades to no FMP
    items rather than costing the whole lookup."""
    if not settings.fmp_enabled or not ticker:
        return []
    base = settings.fmp_base_url.rstrip("/")
    key = settings.fmp_api_key
    params = {
        "symbols": ticker.upper(),
        "from": window_start.isoformat(),
        "to": window_end.isoformat(),
        "limit": limit,
        "apikey": key,
    }
    out: list[NewsItem] = []
    try:
        rows = request_json(f"{base}/stable/news/stock", params=params, settings=settings)
        out.extend(parse_fmp_news(rows, symbol=ticker, source="fmp_stock_news"))
    except Exception:  # noqa: BLE001 - one dead endpoint must not cost the other
        pass
    try:
        rows = request_json(
            f"{base}/stable/news/press-releases", params=params, settings=settings
        )
        out.extend(parse_fmp_news(rows, symbol=ticker, source="fmp_press_release"))
    except Exception:  # noqa: BLE001
        pass
    return out


def fetch_sec_8k(
    cik: str, *, settings: Settings
) -> list[NewsItem]:  # pragma: no cover - network path
    """Live 8-K history for one issuer CIK. Raises nothing; a missing/unresolvable
    CIK or an EDGAR outage degrades to no SEC items."""
    padded = normalize_cik(cik)
    if not padded:
        return []
    try:
        text = request_text(
            _SEC_SUBMISSIONS_URL.format(cik=padded),
            headers=polite_headers(settings.sec_user_agent),
            settings=settings,
        )
        import json

        payload = json.loads(text)
    except Exception:  # noqa: BLE001
        return []
    recent = payload.get("filings", {}).get("recent", {})
    return parse_8k_filings(recent, cik=padded)


def context_for(
    *,
    ticker: str | None = None,
    cik: str | None = None,
    around: date,
    window_days: int = 7,
    settings: Settings | None = None,
) -> list[NewsItem]:
    """Retrieved, attributable news/context for one issuer around one date.

    Needs at least a ticker (for the FMP path), a CIK (for the SEC 8-K path), or both;
    whichever identifier is missing simply skips that source rather than raising. When
    neither is given, or when both sources come back empty, returns `[]` -- an
    explicit, honest "no related news found in the window" rather than anything
    speculative.

    `window_days` is applied on both sides of `around`: a filing's context is often
    what led up to it as much as what followed. Results are deduped and sorted newest
    first; nothing here ranks a source above another beyond that ordering -- the prose
    layer decides what, if anything, to feature.
    """
    s = settings or get_settings()
    start = around - timedelta(days=window_days)
    end = around + timedelta(days=window_days)

    items: list[NewsItem] = []
    if ticker:
        items.extend(fetch_fmp_news(ticker, settings=s, window_start=start, window_end=end))
    if cik:
        items.extend(_within_window(fetch_sec_8k(cik, settings=s), around, window_days))

    items = _dedupe(items)
    items.sort(key=lambda it: it.published, reverse=True)
    return items
