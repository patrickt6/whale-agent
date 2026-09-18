"""Taiwan MOPS / TWSE OpenAPI adapter -- the first non-US jurisdiction.

Licensing: open public data. The TWSE OpenAPI is keyless; politeness (a real contact
string in User-Agent, backoff between calls) is the only requirement.

Datasets used:
  t187ap08_L  insider and major-shareholder holding balances (the 5%+ signal)
  t187ap03_L  director / supervisor / manager holdings
  t187ap04_L  material announcements

Two things about this source shape the design.

*It publishes holdings, not transactions.* TWSE reports a monthly balance per insider,
so there is no "transaction" to normalize. Rather than invent one, each row becomes an
event describing the disclosed stake as of the report date. The stable dedup key already
includes the share count, so an unchanged balance produces the same key month after
month and upserts over itself; only an actual change in holdings creates a new event.
That is the correct behaviour and it falls out of the existing key design for free.

*Amounts are in TWD and often absent entirely.* Rows carry share counts and sometimes a
holding percentage, never a dollar value. Valuation therefore runs through the percent
and share-count branches, which need a price and a share count from the `PriceProvider`
(FMP, using the `.TW` suffix) plus the TWD FX rate. With no price provider configured
these events are ingested and stored but cannot clear the $5M gate -- they show up the
day FMP is switched on, without any re-ingestion.

Field names arrive in Chinese and TWSE has changed them before. `_pick` accepts every
spelling seen so far and rows missing an essential field are skipped rather than
defaulted, so a rename degrades to "fewer Taiwan rows today" and the golden-file test
catches it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.ingestion._http import polite_headers, request_json
from whale_agent.ingestion.base import Adapter
from whale_agent.models.enums import FilerType, Jurisdiction, TransactionType
from whale_agent.models.event import NormalizedEvent

DATASETS = {
    "insider_holdings": "t187ap08_L",
    "director_holdings": "t187ap03_L",
    "material_announcements": "t187ap04_L",
}

# Percentage at which Taiwan's major-shareholder disclosure regime bites.
MAJOR_SHAREHOLDER_PERCENT = 5.0

_COMPANY_CODE_KEYS = ("公司代號", "CompanyCode", "Code", "公司代号")
_COMPANY_NAME_KEYS = ("公司名稱", "CompanyName", "Name", "公司简称")
_PERSON_KEYS = ("姓名", "Name", "PersonName", "股東名稱")
_TITLE_KEYS = ("職稱", "Title", "JobTitle")
_SHARES_KEYS = (
    "目前持有股數",
    "現持股數",
    "持股數",
    "Shares",
    "CurrentShares",
    "選任時持股數",
)
_PERCENT_KEYS = ("持股比率", "持股比例", "HoldingPercentage", "Percentage")
_DATE_KEYS = ("出表日期", "資料日期", "Date", "ReportDate", "公告日期")


def _pick(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_roc_date(value: Any) -> date | None:
    """Parse a TWSE date, which may be ROC-era (民國) or Gregorian.

    ROC year 114 is Gregorian 2025; TWSE emits both `1140731` and `2025-07-31` across
    datasets, so both are handled. Anything unrecognized returns None and the row is
    dropped rather than dated to today.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().replace("/", "-")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    digits = text.replace("-", "")
    if digits.isdigit() and len(digits) in (6, 7):
        # ROC-era: YYYMMDD (7 digits) or YYMMDD (6). Year offset is 1911.
        year = int(digits[:-4]) + 1911
        try:
            return date(year, int(digits[-4:-2]), int(digits[-2:]))
        except ValueError:
            return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(digits if fmt == "%Y%m%d" else text, fmt).date()
        except ValueError:
            continue
    return None


class TaiwanMopsAdapter(Adapter):
    """Holdings disclosures from the keyless TWSE OpenAPI."""

    name = "taiwan_mops"
    jurisdiction = Jurisdiction.TAIWAN.value

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def fetch(self, **kwargs: Any) -> Iterable[Any]:  # pragma: no cover - network path
        dataset = str(kwargs.get("dataset", "insider_holdings"))
        endpoint = DATASETS.get(dataset, dataset)
        yield request_json(
            f"{self.settings.taiwan_base_url.rstrip('/')}/opendata/{endpoint}",
            headers=polite_headers(self.settings.taiwan_mops_user_agent),
            settings=self.settings,
        )

    def parse(self, raw: Any) -> list[dict]:
        """TWSE OpenAPI returns a bare JSON array of flat records."""
        if isinstance(raw, str):
            import json

            raw = json.loads(raw)
        if isinstance(raw, dict):
            raw = raw.get("data") or []
        return [row for row in (raw or []) if isinstance(row, dict)]

    def normalize(self, parsed: dict) -> NormalizedEvent:
        code = _pick(parsed, _COMPANY_CODE_KEYS)
        report_date = parse_roc_date(_pick(parsed, _DATE_KEYS)) or date.today()
        percent = _float(_pick(parsed, _PERCENT_KEYS))
        shares = _float(_pick(parsed, _SHARES_KEYS))
        title = str(_pick(parsed, _TITLE_KEYS) or "").strip()
        person = str(_pick(parsed, _PERSON_KEYS) or "UNKNOWN").strip()

        # Board members and executives are insiders; anyone else on this list is there
        # because of the size of their stake.
        is_insider = bool(title)
        filer_type = FilerType.INSIDER.value if is_insider else FilerType.UNKNOWN.value

        return NormalizedEvent(
            jurisdiction=Jurisdiction.TAIWAN,
            source=self.name,
            source_url="https://mops.twse.com.tw/mops/web/index",
            issuer_name=str(_pick(parsed, _COMPANY_NAME_KEYS) or code or "UNKNOWN"),
            issuer_id=str(code) if code else None,
            # FMP and most vendors suffix Taiwan listings with `.TW`.
            ticker=f"{code}.TW" if code else None,
            filer_name=f"{person} ({title})" if title else person,
            filer_id=f"TW:{code}:{person}" if code else None,
            filer_type=filer_type,
            # A disclosed balance, not a trade: PASSIVE_13G is the taxonomy's existing
            # "large stake, no stated intent" bucket.
            transaction_type=TransactionType.PASSIVE_13G,
            transaction_date=report_date,
            disclosure_date=report_date,
            native_currency="TWD",
            share_count=shares,
            percent_of_company=percent,
            percentage_threshold_crossed=bool(
                percent is not None and percent >= MAJOR_SHAREHOLDER_PERCENT
            ),
            raw_payload=parsed,
        )
