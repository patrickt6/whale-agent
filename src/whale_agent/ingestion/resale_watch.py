"""Registration statements that name a tracked fund as a selling securityholder.

The signal nothing else carries. A 13F says what a fund held three months ago; a 13D/G
fires when it crosses 5%; a Form 4 fires when a 10% holder trades. None of them fire when
a fund arranges to sell. That happens in the issuer's registration statement, weeks
earlier, in a table of selling securityholders.

SharonAI filed an S-1 on 2026-07-31 registering 7,274,842 of Situational Awareness
Partners LP's 7,563,029 shares. That document, filed by somebody else entirely, is what
the story circulating about the fund was actually about.

Matching is on entity names, since these tables carry no CIK. `Whale.matches` requires a
whole-phrase hit followed by an entity suffix, because "situational awareness" is also
ordinary English in defence and AI filings.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, replace
from datetime import date
from html import unescape

from whale_agent.ingestion.fund_watchlist import _get, _parse_date, fetch_index_candidates
from whale_agent.ingestion.whales import WHALES

RESALE_FORMS = ("S-1", "424B3")


@dataclass(frozen=True)
class ResaleRegistration:
    whale_name: str
    issuer: str
    symbol: str
    form: str
    filed: date
    shares_registered: int | None
    shares_held: int | None
    url: str = ""
    accession: str = ""  # bare (dashless) form, same as PooledTrade/PooledStake

    def describe(self) -> str:
        bits = []
        if self.shares_registered:
            bits.append(f"{self.shares_registered:,} shares registered for resale")
        else:
            bits.append("shares registered for resale")
        if self.shares_held:
            bits.append(f"of {self.shares_held:,} held")
        where = self.symbol or self.issuer
        if where:
            bits.append(f"in {where}")
        return " ".join(bits)


def _cells(row: str) -> list[str]:
    raw = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
    return [unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in raw]


def _number(text: str) -> int | None:
    m = re.fullmatch(r"[\d,]+", text.replace("\xa0", "").strip())
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def scan_selling_securityholders(
    html: str,
    filed: date,
    form: str,
    url: str,
    symbol: str,
    issuer: str = "",
    accession: str = "",
) -> list[ResaleRegistration]:
    """Rows of a selling-securityholder table naming a tracked whale.

    Reads the document's table rows rather than trying to identify which table is the
    selling-securityholder one. Filers title that section a dozen different ways, and a
    row that both names a tracked entity and carries share counts is the thing wanted
    however the table above it was labelled.
    """
    # FMP returns the literal string "None" for issuers it holds no ticker for, and it
    # reached the reader as "in None" on the first live render.
    symbol = "" if symbol.strip().lower() in {"", "none", "null"} else symbol.strip()
    out: list[ResaleRegistration] = []
    seen: set[str] = set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = _cells(row)
        if len(cells) < 2:
            continue
        label = cells[0]
        whale = next((w for w in WHALES if w.matches(label)), None)
        if whale is None or whale.name in seen:
            continue
        numbers = [n for n in (_number(c) for c in cells[1:]) if n is not None]
        if not numbers:
            continue
        seen.add(whale.name)
        held = numbers[0] if numbers else None
        registered = numbers[1] if len(numbers) > 1 else None
        out.append(
            ResaleRegistration(
                whale_name=whale.name,
                issuer=issuer,
                symbol=symbol,
                form=form,
                filed=filed,
                shares_registered=registered,
                shares_held=held,
                url=url,
                accession=accession,
            )
        )
    return out


_ISSUER_NAMES: dict[str, str] = {}


def _issuer_name(cik: str) -> str:
    """The registrant's name from EDGAR. Only called for rows that already matched.

    FMP's filing rows carry no company name and often no ticker, so a hit would otherwise
    be described without saying whose stock is being registered. One small request per
    hit, and there is roughly one hit a week.
    """
    if not cik:
        return ""
    if cik in _ISSUER_NAMES:
        return _ISSUER_NAMES[cik]
    try:
        data = json.loads(
            _get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json").decode()
        )
        name = str(data.get("name") or "").strip()
    except Exception:  # noqa: BLE001 - a nameless issuer still reports its filing
        name = ""
    _ISSUER_NAMES[cik] = name
    return name


def pick_primary_document(listing: dict) -> str:
    """The filing's own document, from an EDGAR directory listing. "" when unclear.

    Largest .htm wins, minus the two kinds that are reliably not the filing: the
    `-index.htm` wrapper EDGAR generates, and `ex*` exhibits. Rendered XBRL fragments
    (`R1.htm`, `R2.htm`, ...) are excluded for the same reason -- they are slices of the
    filing, not the document a selling-securityholder table lives in.
    """
    items = ((listing or {}).get("directory") or {}).get("item") or []
    best, best_size = "", -1
    for item in items:
        name = (item.get("name") or "").strip()
        low = name.lower()
        if not low.endswith((".htm", ".html")):
            continue
        if low.endswith("-index.htm") or low.startswith("ex"):
            continue
        if re.fullmatch(r"r\d+\.htm", low):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > best_size:
            best, best_size = name, size
    return best


def fetch_primary_document_url(cik: str, bare_accession: str) -> str:
    """Resolve a filing directory to its primary document URL. "" when unreadable."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{bare_accession}/"
    try:
        listing = json.loads(_get(base + "index.json", timeout=30).decode())
    except Exception:  # noqa: BLE001 - an unreadable listing is skipped, not fatal
        return ""
    name = pick_primary_document(listing)
    return base + name if name else ""


def fetch_resale_registrations(
    api_key: str,
    since: date,
    until: date,
    *,
    forms: tuple[str, ...] = RESALE_FORMS,
    max_documents: int = 250,
    with_count: bool = False,
):
    """Scan the window's registration statements for tracked whales. Never raises.

    The cap is a backstop against an unbounded window, not a budget. It was 40 newest
    first, which reached back a single day and dropped the SharonAI S-1 this module was
    written for. A full week is about 200 documents and measured 36 seconds end to end,
    so the whole window is scanned; newest first only decides the reading order.
    """
    candidates: list[tuple[date, str, str, str, str, str]] = []
    seen: set[str] = set()

    # Discovery from EDGAR's daily index, which lists every filing for a day with no
    # page cap. The vendor search below is kept only for days the index has not
    # published yet (it lags by about a day), because on its own it silently shrinks the
    # window: measured 2026-08-04, S-1 and 424B3 both returned a full 100 rows dated
    # entirely to that same day, so a filing from earlier in the window was unreachable
    # by the afternoon even though the morning run had found it.
    for filed, form, cik, accession in fetch_index_candidates(since, until, forms=forms):
        bare = accession.replace("-", "")
        if bare in seen:
            continue
        seen.add(bare)
        candidates.append((filed, form, "", f"IDX:{cik}:{bare}", cik, bare))

    for form in forms:
        if not api_key:
            break
        url = (
            "https://financialmodelingprep.com/stable/sec-filings-search/form-type"
            f"?formType={urllib.parse.quote(form)}&from={since.isoformat()}"
            f"&to={until.isoformat()}&limit=100&apikey={api_key}"
        )
        try:
            rows = json.loads(_get(url).decode())
        except Exception:  # noqa: BLE001 - one form type failing is not fatal
            continue
        for row in rows:
            link = row.get("finalLink") or row.get("link") or ""
            m = re.search(r"/data/(\d+)/(\d+)/", link)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            filed = _parse_date((row.get("filingDate") or "")[:10])
            if filed:
                candidates.append(
                    (
                        filed,
                        row.get("formType") or form,
                        row.get("symbol") or "",
                        link,
                        m.group(1),
                        m.group(2),
                    )
                )

    candidates.sort(reverse=True)
    out: list[ResaleRegistration] = []
    for filed, form, symbol, link, cik, accession in candidates[:max_documents]:
        # Index-discovered rows carry a directory, not a document; resolve it lazily so
        # the extra request is only paid for filings actually being read.
        if link.startswith("IDX:"):
            _, idx_cik, bare = link.split(":", 2)
            link = fetch_primary_document_url(idx_cik, bare)
            if not link:
                continue
        try:
            html = _get(link, timeout=45).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - an unreadable document is skipped
            continue
        hits = scan_selling_securityholders(
            html, filed, form, link, symbol, accession=accession
        )
        if hits:
            issuer = _issuer_name(cik)
            hits = [replace(h, issuer=issuer) for h in hits] if issuer else hits
        out.extend(hits)
    # The number discovered, not the number read: an empty result is only meaningful
    # alongside how many registrations were actually in the window.
    return (out, len(candidates)) if with_count else out
