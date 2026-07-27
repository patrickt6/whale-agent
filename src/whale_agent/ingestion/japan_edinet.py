"""Japan EDINET adapter: 5% Rule large-shareholding reports (大量保有報告書).

Licensing: open public data from the FSA. The API is free but requires a registered
subscription key (`JAPAN_EDINET_API_KEY`), so this source stays off until that key is
set -- see .env.example.

Regime: Article 27-23 FIEA. Anyone crossing 5% of a listed issuer files within 5
business days; a Change Report follows on any move of 1 percentage point or more.

Two-step ingestion, because EDINET splits metadata from content:

  1. `documents.json?date=...&type=2` lists every filing submitted that day. This gives
     the filer and the document type -- but no numbers, and on a large-shareholding
     report no issuer either. Verified live on 2026-07-24: `secCode`, `issuerName` and
     `subjectEdinetCode` are null on every single one of the 34 filings returned. The
     issuer is only identifiable from the document body.
  2. `documents/{docID}?type=5` returns a **ZIP archive**, not text. Inside is one
     `XBRL_TO_CSV/*.csv`, tab separated and **UTF-16 encoded with a BOM**. Decoding the
     response as text throws the numbers away, so `fetch_csv` unpacks the archive.

The CSV is nine columns: element id, label, context id, relative period, consolidated
flag, period type, unit id, unit, value. Facts are reported once per joint holder in a
`...FilerLargeVolumeHolderNMember` context, and, when there is more than one holder, a
second time in the bare `FilingDateInstant` context carrying the group total. The group
total is the number the 5% rule is about, so it wins whenever it is present; a
single-holder filing has no aggregate row at all and the lone member context is the
total. Taking the first matching row instead, as this file used to, silently reported
one co-holder's slice: on filing S100YS2X that would have been 0.42% and 214,871 shares
in place of the real 10.52% and 5,420,371.

Ratios are XBRL `pure` fractions, so 0.1052 means 10.52 percent. `percent_of_company` is
a percentage everywhere else in this system, so the parser multiplies by 100 once, here,
rather than leaving a unit mismatch for valuation to trip over.

`parse()` takes the metadata/CSV pair as one dict so it stays pure and golden-file
testable; only `fetch()` performs the two requests.

Filings are Japanese-only. Nothing here translates anything: the filer and issuer names
are stored verbatim, and only the LLM prose layer -- which cannot touch numeric fields --
ever renders Japanese into English.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import zipfile
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from whale_agent.config import Settings, get_settings
from whale_agent.errors import SourceUnavailableError
from whale_agent.ingestion._http import _RETRYABLE as _RETRYABLE_STATUS
from whale_agent.ingestion._http import request_json
from whale_agent.ingestion.base import Adapter
from whale_agent.models.enums import FilerType, Jurisdiction, TransactionType
from whale_agent.models.event import NormalizedEvent

log = logging.getLogger(__name__)

# 350 covers BOTH 大量保有報告書 (initial 5% report) and 変更報告書 (change report) --
# the live feed returns docTypeCode 350 for both, distinguished only by docDescription.
# 360 is 訂正報告書, a correction to one of those, which is the real amendment case.
# The old comment here had 360 as the change report; it is not.
LARGE_HOLDING_DOC_TYPES = {"350", "360"}
CORRECTION_DOC_TYPE = "360"

# One day is the wrong window for a weekly report; see `fetch`.
DEFAULT_LOOKBACK_DAYS = 7

# Local element names (the part after the `jplvh_cor:` / `jpdei_cor:` prefix), as
# observed live. Exact names, not fragments: `HoldingRatioOfShareCertificatesEtc` is a
# prefix of `HoldingRatioOfShareCertificatesEtcPerLastReport`, and fragment matching is
# exactly how the previous holding ratio used to be mistaken for the current one.
_EL_RATIO = "HoldingRatioOfShareCertificatesEtc"
_EL_PRIOR_RATIO = "HoldingRatioOfShareCertificatesEtcPerLastReport"
_EL_SHARES = "TotalNumberOfStocksEtcHeld"
_EL_SHARES_OUTSTANDING = "TotalNumberOfOutstandingStocksEtc"
_EL_ISSUER_NAME = "NameOfIssuer"
_EL_ISSUER_CODE = "SecurityCodeOfIssuer"
_EL_FILER_NAME_JA = "FilerNameInJapaneseDEI"
_EL_FILER_NAME_EN = "FilerNameInEnglishDEI"
_EL_OBLIGATION_DATE = "DateWhenFilingRequirementAroseCoverPage"
_EL_CHANGE_REASON = "ReasonForFilingChangeReportCoverPage"
_EL_PURPOSE = "PurposeOfHolding"

# Japanese labels, kept as a fallback so a form-version rename of an element id degrades
# to a still-correct read rather than to silence. Same ordering hazard, same fix: the
# prior-ratio label is tested before the current-ratio label.
_LABEL_PRIOR_RATIO = "直前の報告書に記載された株券等保有割合"
_LABEL_RATIO = "株券等保有割合"
_LABEL_SHARES = "保有株券等の数"
_LABEL_SHARES_OUTSTANDING = "発行済株式等総数"
_LABEL_ISSUER_NAME = "発行者の名称"
_LABEL_ISSUER_CODE = "発行者の証券コード"

# EDINET writes an em-width dash for "no value". It is data, not punctuation.
_NULL_TOKENS = {"", "-", "―", "—", "─", "－", "ー", "None"}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if text in _NULL_TOKENS:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text in _NULL_TOKENS else text


def _parse_dt(value: Any) -> date | None:
    if not value:
        return None
    text = _to_text(value)
    if not text:
        return None
    # The old version sliced `text[:len(fmt)]`, which confuses the length of a format
    # string with the length of what it formats: "%Y-%m-%d" is 8 characters, so it cut
    # "2026-07-24" down to "2026-07-" and failed on every date EDINET has ever sent.
    # The result was a silent fallback to today's date on every single filing.
    head = text.split()[0].replace("/", "-")
    try:
        return datetime.strptime(head, "%Y-%m-%d").date()
    except ValueError:
        return None


def decode_edinet_document(payload: bytes | str) -> str:
    """Turn a raw `documents/{docID}?type=5` response body into CSV text.

    The endpoint returns a ZIP archive whose single member is `XBRL_TO_CSV/*.csv`,
    encoded UTF-16 with a BOM. A str is passed straight through so callers that already
    hold decoded text (tests, cached fixtures) need no special case. A body that is
    neither a readable archive nor decodable text yields "" rather than raising, because
    a malformed document must cost one filing, not the run.
    """
    if isinstance(payload, str):
        return payload
    if not payload:
        return ""
    if payload[:2] == b"PK":
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not names:
                return ""
            # Prefer the XBRL_TO_CSV member; an audit-report archive can carry others.
            names.sort(key=lambda n: (0 if "XBRL_TO_CSV" in n else 1, n))
            raw = archive.read(names[0])
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            log.warning("EDINET document archive unreadable: %s", exc)
            return ""
    else:
        raw = payload
    for encoding in ("utf-16", "utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


def _context_rank(context: str) -> tuple[int, str]:
    """Sort key putting the group-total context ahead of per-holder contexts."""
    return (1 if "Member" in context else 0, context)


def parse_holdings_csv(text: str | bytes) -> dict[str, Any]:
    """Pull the reportable facts out of an EDINET large-shareholding CSV.

    Reads by exact element id with a Japanese-label fallback, and resolves the
    per-holder/group-total split described in the module docstring: the bare
    `FilingDateInstant` context wins when present, a lone holder context is used as-is,
    and several holder contexts with no group total are summed, which is what EDINET's
    own aggregate row does (verified against S100YS2X, where the member shares and
    ratios add up to the stated totals exactly).

    Ratios come back as percentages. Anything not found is absent from the result; no
    field is ever defaulted to zero, because a zero would flow straight into a dollar
    figure and read as a real, tiny position rather than as an unknown one.
    """
    text = decode_edinet_document(text)
    if not text:
        return {}

    # element -> context -> value, so the context choice happens after the whole file
    # is seen rather than being decided by row order.
    numeric: dict[str, dict[str, float]] = {}
    strings: dict[str, str] = {}

    reader = csv.reader(io.StringIO(text), delimiter="\t")
    for row in reader:
        if len(row) < 3:
            continue
        element = row[0].split(":")[-1].strip()
        label = row[1].strip()
        context = row[2].strip()
        raw_value = row[-1]

        if element == _EL_PRIOR_RATIO or label == _LABEL_PRIOR_RATIO:
            field = "prior_ratio"
        elif element == _EL_RATIO or label == _LABEL_RATIO:
            field = "ratio"
        elif element == _EL_SHARES or label == _LABEL_SHARES:
            field = "shares"
        elif element == _EL_SHARES_OUTSTANDING or label == _LABEL_SHARES_OUTSTANDING:
            field = "shares_outstanding"
        else:
            if element == _EL_ISSUER_NAME or label == _LABEL_ISSUER_NAME:
                text_value = _to_text(raw_value)
                if text_value:
                    strings["issuer_name"] = text_value
            elif element == _EL_ISSUER_CODE or label == _LABEL_ISSUER_CODE:
                text_value = _to_text(raw_value)
                if text_value:
                    strings["issuer_code"] = text_value
            elif element == _EL_FILER_NAME_JA:
                strings.setdefault("filer_name_ja", _to_text(raw_value) or "")
            elif element == _EL_FILER_NAME_EN:
                strings.setdefault("filer_name_en", _to_text(raw_value) or "")
            elif element == _EL_OBLIGATION_DATE:
                strings.setdefault("obligation_date", _to_text(raw_value) or "")
            elif element == _EL_CHANGE_REASON:
                strings.setdefault("change_reason", _to_text(raw_value) or "")
            elif element == _EL_PURPOSE:
                strings.setdefault("purpose", _to_text(raw_value) or "")
            continue

        value = _to_float(raw_value)
        if value is None:
            continue
        numeric.setdefault(field, {}).setdefault(context, value)

    found: dict[str, Any] = {k: v for k, v in strings.items() if v}

    for field, by_context in numeric.items():
        contexts = sorted(by_context, key=_context_rank)
        totals = [c for c in contexts if "Member" not in c]
        if totals:
            value = by_context[totals[0]]
        elif field == "shares_outstanding":
            # The issuer's own share count, repeated identically per holder; summing it
            # would multiply the denominator by the number of co-filers.
            value = by_context[contexts[0]]
        elif len(contexts) == 1:
            value = by_context[contexts[0]]
        else:
            value = sum(by_context[c] for c in contexts)
        found[field] = value

    # XBRL `pure` ratios are fractions; the rest of the system speaks percent.
    for field in ("ratio", "prior_ratio"):
        if field in found:
            # Rounded because 0.2017 * 100 is 20.169999999999998 in binary floating
            # point, and a holding ratio has no business carrying fifteen digits.
            found[field] = round(found[field] * 100.0, 6)
    # A holding ratio over 100 percent is not a holding ratio. Refuse it rather than
    # let it become a dollar figure.
    if found.get("ratio") is not None and not 0.0 <= found["ratio"] <= 100.0:
        found.pop("ratio", None)
    if found.get("prior_ratio") is not None and not 0.0 <= found["prior_ratio"] <= 100.0:
        found.pop("prior_ratio", None)
    return found


class JapanEdinetAdapter(Adapter):
    """Large-shareholding reports from EDINET v2."""

    name = "japan_edinet"
    jurisdiction = Jurisdiction.JAPAN.value

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # -- network ----------------------------------------------------------------
    def list_documents(self, on: date) -> list[dict]:  # pragma: no cover - network
        key = self.settings.require("japan_edinet_api_key")
        data = request_json(
            f"{self.settings.japan_edinet_base_url.rstrip('/')}/documents.json",
            params={"date": on.isoformat(), "type": 2, "Subscription-Key": key},
            settings=self.settings,
        )
        results = (data or {}).get("results") or []
        return [
            r
            for r in results
            if isinstance(r, dict) and str(r.get("docTypeCode")) in LARGE_HOLDING_DOC_TYPES
        ]

    def fetch_csv(self, doc_id: str) -> str:  # pragma: no cover - network
        """Download one filing body and return its CSV text.

        This reads **bytes**, not text: the endpoint serves a ZIP
        (`Content-Type: application/octet-stream`, magic `PK\\x03\\x04`), and asking for
        text hands back a mojibake decode of compressed data from which no number can be
        recovered. That single wrong call is why this source had never produced a row.
        """
        key = self.settings.require("japan_edinet_api_key")
        url = f"{self.settings.japan_edinet_base_url.rstrip('/')}/documents/{doc_id}"
        params = {"type": 5, "Subscription-Key": key}
        last_error: Exception | None = None
        with httpx.Client(
            timeout=self.settings.http_timeout_seconds, follow_redirects=True
        ) as client:
            for attempt in range(self.settings.http_max_retries):
                try:
                    resp = client.get(url, params=params)
                except httpx.HTTPError as exc:
                    last_error = exc
                else:
                    if resp.status_code < 400:
                        return decode_edinet_document(resp.content)
                    error = SourceUnavailableError(f"GET {url} -> HTTP {resp.status_code}")
                    if resp.status_code not in _RETRYABLE_STATUS:
                        raise error
                    last_error = error
                if attempt < self.settings.http_max_retries - 1:
                    time.sleep(self.settings.http_backoff_seconds * (2**attempt))
        raise SourceUnavailableError(f"GET {url} failed: {last_error}")

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        """Walk back over `days` calendar days ending at `on`.

        A single day is the wrong window for a weekly product. EDINET publishes nothing
        on weekends or Japanese public holidays, so a one-day fetch returns an empty
        list roughly two days in seven and covers at most a fifth of the week the report
        is about. Re-reading a day already ingested is harmless: events carry a stable
        `event_id`, so the store deduplicates.

        Each day is fetched independently and a failed day is logged and skipped, so one
        bad date cannot cost the other six or fail the run.
        """
        on = kwargs.get("on") or date.today()
        days = max(1, int(kwargs.get("days") or DEFAULT_LOOKBACK_DAYS))
        for offset in range(days):
            day = on - timedelta(days=offset)
            try:
                metas = self.list_documents(day)
            except SourceUnavailableError as exc:
                log.warning("EDINET document list for %s unavailable: %s", day, exc)
                continue
            for meta in metas:
                doc_id = meta.get("docID")
                body = ""
                if doc_id:
                    try:
                        body = self.fetch_csv(doc_id)
                    except SourceUnavailableError as exc:
                        # Metadata alone still identifies the filer; the event is worth
                        # recording even when the numbers cannot be retrieved.
                        log.warning("EDINET doc %s body unavailable: %s", doc_id, exc)
                yield {"meta": meta, "csv": body}

    # -- pure -------------------------------------------------------------------
    def parse(self, raw: Any) -> list[dict]:
        meta = (raw or {}).get("meta") or {}
        if str(meta.get("docTypeCode")) not in LARGE_HOLDING_DOC_TYPES:
            return []
        holdings = parse_holdings_csv(raw.get("csv") or "") if raw.get("csv") else {}
        return [{**meta, **holdings}]

    def normalize(self, parsed: dict) -> NormalizedEvent:
        submitted = _parse_dt(parsed.get("submitDateTime")) or date.today()
        # The obligation date is when the holder actually crossed the threshold; the
        # submit date can be five business days later, which is five days of price
        # movement between the event and the number we would price it at.
        arose = _parse_dt(parsed.get("obligation_date")) or submitted

        # The documents.json row does NOT identify the issuer on a large-shareholding
        # report: `secCode`, `issuerName` and `subjectEdinetCode` came back null on all
        # 34 filings listed for 2026-07-24. The body is the only source, so the CSV
        # values are preferred and the metadata is a fallback for the day EDINET starts
        # populating them.
        sec_code = str(parsed.get("issuer_code") or parsed.get("secCode") or "").strip()
        ratio = _to_float(parsed.get("ratio"))
        prior = _to_float(parsed.get("prior_ratio"))

        description = str(parsed.get("docDescription") or "")
        is_correction = (
            str(parsed.get("docTypeCode")) == CORRECTION_DOC_TYPE or "訂正" in description
        )
        # docTypeCode is 350 for both the initial report and the change report, so the
        # distinction lives in the description and in the change-reason element.
        is_change_report = "変更" in description or bool(parsed.get("change_reason"))
        return NormalizedEvent(
            jurisdiction=Jurisdiction.JAPAN,
            source=self.name,
            source_url=(
                f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?"
                f"S100={parsed.get('docID')}"
                if parsed.get("docID")
                else None
            ),
            # On a large-shareholding report `filerName` is the *holder*; the issuer is
            # the subject company, which EDINET names separately (and sometimes omits,
            # leaving only the securities code).
            issuer_name=str(
                parsed.get("issuer_name")
                or parsed.get("issuerName")
                or parsed.get("subjectName")
                or sec_code
                or "UNKNOWN"
            ),
            issuer_id=sec_code or None,
            # `SecurityCodeOfIssuer` in the body is the bare four-digit TSE code
            # ("6951"); the `secCode` metadata field, when populated, carries a trailing
            # placeholder digit ("69510"). Slicing to four handles both.
            ticker=f"{sec_code[:4]}.T" if len(sec_code) >= 4 else None,
            filer_name=str(
                parsed.get("filerName") or parsed.get("filer_name_ja") or "UNKNOWN"
            ),
            filer_id=str(parsed.get("edinetCode") or "") or None,
            filer_type=FilerType.UNKNOWN.value,
            transaction_type=TransactionType.PASSIVE_13G,
            transaction_date=arose,
            disclosure_date=submitted,
            native_currency="JPY",
            share_count=_to_float(parsed.get("shares")),
            percent_of_company=ratio,
            # The filing states the issuer's own share count, which is exact and dated.
            # Preferring it over a price vendor's figure keeps the denominator of every
            # percent-of-company valuation out of a third party's hands.
            shares_outstanding=_to_float(parsed.get("shares_outstanding")),
            # An initial report always crosses 5%; a change report only counts as a
            # crossing when the ratio actually moved.
            percentage_threshold_crossed=bool(
                ratio is not None and (not is_change_report or prior != ratio)
            ),
            is_amendment=is_correction,
            raw_payload=parsed,
        )
