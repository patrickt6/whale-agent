"""Named managers, queried directly, rather than filtered out of a firehose.

The rest of the pipeline sweeps a market and keeps what clears a threshold. That is the
wrong shape for "what is this fund doing": a manager who files nothing this week produces
no rows, and absence in a sweep is indistinguishable from absence of interest. So this
module asks EDGAR about specific CIKs and reports what it finds, including "nothing
recently", which is itself an answer.

Two clocks are read for each manager, because they say different things:

  * 13F-HR is a quarterly snapshot, up to ~135 days stale. It is the only source of the
    long tail of sub-5% positions, and it is where a portfolio's shape is visible.
  * Schedule 13D/13G and Forms 3/4 are fast -- days, sometimes two business days. They
    carry the concentrated positions that move, and they are what makes a manager news.

Every figure here is copied from the filing's own information table. Nothing is derived,
inferred, or modelled, which is what lets this sit inside a product whose guarantee is
that figures resolve to filings.

EDGAR asks for a descriptive User-Agent and rate-limits at 10 requests/second. Both are
respected below; do not raise the rate.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from whale_agent.storage.db import Store
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from html import unescape

from whale_agent.config import SEC_USER_AGENT_DEFAULT
from whale_agent.redact import redact

# The same contact string every other SEC-facing module defaults to (see
# `whale_agent.config.SEC_USER_AGENT_DEFAULT`) -- kept as one constant so the two never
# drift apart the way they did before this module and `Settings.sec_user_agent` each
# hardcoded their own copy.
USER_AGENT = SEC_USER_AGENT_DEFAULT
_SEC_PAUSE = 0.15  # seconds between calls; EDGAR's published ceiling is 10/s

# The managers the weekly reports on by name. CIKs resolved against EDGAR's company
# search on 2026-08-02 rather than typed from memory -- a wrong CIK here silently
# reports somebody else's portfolio, which is worse than reporting nothing.
WATCHLIST: dict[str, str] = {
    "Situational Awareness LP": "0002045724",
    "Situational Awareness Partners LP": "0002038540",
    "Berkshire Hathaway": "0001067983",
    "Pershing Square Capital": "0001336528",
    "Scion Asset Management": "0001649339",
    "Bridgewater Associates": "0001350694",
    "Third Point": "0001040273",
    "Duquesne Family Office": "0001536411",
    "Coatue Management": "0001135730",
    "Tiger Global Management": "0001167483",
    "Lone Pine Capital": "0001061165",
    # Added 2026-08-04. Every CIK below was resolved through EDGAR company
    # search and then confirmed against data.sec.gov/submissions to be an entity
    # that files 13F-HR itself. That second step is the point: name search alone
    # returns the subject company of a filing as readily as its filer.
    "Appaloosa Management": "0001006438",
    "AQR Capital Management": "0001167557",
    "Ancora Advisors": "0001446114",
    "Baillie Gifford & Co": "0001088875",
    "Baupost Group": "0001054420",
    "Citadel Advisors": "0001423053",
    "Corvex Management": "0001535472",
    "D. E. Shaw": "0001009268",
    "Elliott Investment Management": "0001791786",
    "Engine Capital": "0001665590",
    "Farallon Capital Management": "0000909661",
    "Glenview Capital": "0001138995",
    "Greenlight Capital": "0001079114",
    "Hudson Bay Capital": "0001393825",
    "Carl Icahn": "0000921669",
    "Jana Partners": "0001159159",
    "Land & Buildings": "0001536520",
    "Marshall Wace": "0001318757",
    "Millennium Management": "0001273087",
    "Point72 Asset Management": "0001603466",
    "Renaissance Technologies": "0001037389",
    "Soros Fund Management": "0001029160",
    "Sculptor Capital": "0001054587",
    "Starboard Value": "0001517137",
    "Tudor Investment": "0000923093",
    "Two Sigma Investments": "0001179392",
    "Trian Fund Management": "0001345472",
    "Viking Global Investors": "0001103804",
    "ValueAct Capital": "0001351069",
    # Added because BlackRock, Blackstone and several other obvious
    # names were missing. Same rule as above: each CIK below was resolved
    # through EDGAR company search (browse-edgar and, where the name search came up
    # empty, EDGAR full-text search) and confirmed against
    # data.sec.gov/submissions/CIK##########.json to carry "13F-HR" in its own filing
    # history, not merely "13F-NT" (a notice that someone else files on the entity's
    # behalf). Several obvious-looking CIKs for these names -- most of BlackRock's other
    # entities, all of Apollo's and Vanguard's search hits before the ones kept -- file
    # 13F-NT only and were rejected for that reason. Apollo, Balyasny and State Street
    # do not surface under their brand name in EDGAR's company-name search at all; their
    # filer entities were found via EDGAR full-text search instead.
    "BlackRock": "0001086364",  # BlackRock Advisors LLC
    "Blackstone": "0001393818",  # Blackstone Inc.
    "Vanguard Group": "0000102909",  # The Vanguard Group, Inc.
    "State Street": "0000093751",  # State Street Corp
    "Fidelity (FMR)": "0000315066",  # FMR LLC
    "KKR": "0001520692",  # KKR Credit Advisors (US) LLC
    "Apollo Global Management": "0001449434",  # Apollo Management Holdings, L.P.
    "Carlyle Group": "0001527166",  # Carlyle Group Inc.
    "Man Group": "0001637460",  # Man Group plc
    "Balyasny Asset Management": "0001218710",  # Balyasny Asset Management L.P.
    "ExodusPoint Capital Management": "0001736225",  # ExodusPoint Capital Management, LP
    "Brevan Howard": "0001415453",  # Brevan Howard Asset Management LLP
}

# Forms that mean "this manager moved recently", as opposed to the quarterly snapshot.
FAST_FORMS = ("SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A", "4", "3")


# A manager is only required to file 13F once it runs $100M or more in 13F securities, so
# a table totalling less than that has not been read in the units it was written in. Some
# filers still report value in thousands, the pre-2023 convention, and nothing in the XML
# declares which. The threshold is the filing requirement itself rather than a guess, and
# the scaling is disclosed in the rendered line instead of being applied silently.
THIRTEEN_F_FLOOR_USD = 100_000_000


@dataclass(frozen=True)
class Holding:
    issuer: str
    value_usd: int
    shares: int | None
    # The stable identity. Filers respell issuer names freely between quarters --
    # "Chevron Corp New" became "Chevron Corporation", "Chubb Limited" became "Chubb Ltd
    # Switz" -- and diffing on the name reported both an opening and a closing of a
    # position that never moved. CUSIP does not drift.
    cusip: str = ""
    # "" for an ordinary long position in the security, "PUT" or "CALL" for a long
    # option position on it. Copied from the filing's own `putCall`, which is absent on
    # ordinary rows. Situational Awareness LP's 2026-03-31 table is 42 rows of which 11
    # are puts totalling $8.46B against $3.86B of stock, so dropping this field turned a
    # book that is 62% put notional into a single "$13.7B portfolio" number and printed
    # the issuers of those puts as things the fund owns.
    option_type: str = ""

    @property
    def key(self) -> str:
        # The option type is part of the identity, not a label on it. A put on NVIDIA
        # and a holding of NVIDIA are different positions that a filer reports on
        # separate rows; merging them on CUSIP alone adds their values together and
        # makes a quarter-over-quarter diff compare unlike things.
        base = self.cusip or self.issuer.upper()
        return f"{base}|{self.option_type}" if self.option_type else base

    @property
    def is_option(self) -> bool:
        return bool(self.option_type)


@dataclass(frozen=True)
class StakeDetail:
    """What a 13D/G actually says: whose stake, in what, how big.

    Read from the filing's own `primary_doc.xml`, which is fully structured and carries
    `issuerName`, `classPercent` and the aggregate share count. This is the level of
    detail people post within minutes of a filing hitting EDGAR -- "X took a 3.7% stake
    in Y" -- and reporting only that a form was filed stops one step short of it.

    Also the fix for zeroed stake sizes: edgartools falls back to
    a header-only parse and reports 0, while these fields are right there in the document.
    """

    issuer: str = ""
    issuer_cusip: str = ""
    percent: float | None = None
    shares: int | None = None
    holders: list[str] = field(default_factory=list)
    # SEC's own classification of who filed, copied from `typeOfReportingPerson`.
    person_types: list[str] = field(default_factory=list)

    @property
    def is_institutional(self) -> bool | None:
        """Did an institution file this, or a person? None when the filing does not say.

        A founder reporting 49.3% of his own company and a fund building a 9.4% position
        are different events that otherwise render identically. The codes are the
        filing's own: IA is a registered investment adviser, IC an investment company,
        BD a broker-dealer, PN a partnership, HC a parent holding company, IN a natural
        person. A filing with no code is left unlabelled rather than assumed either way.
        """
        # "OO" (other) is deliberately absent: it is used by trusts, estates and
        # individuals' holding vehicles alike, so it decides nothing on its own.
        institutional = {"IA", "IC", "BD", "PN", "HC", "FI", "SA", "CO"}
        if any(code in institutional for code in self.person_types):
            return True
        if "IN" in self.person_types:
            return False
        return None

    def describe(self) -> str:
        bits = []
        if self.percent is not None:
            bits.append(f"{self.percent:g}%")
        if self.shares:
            bits.append(f"{self.shares:,} shares")
        stake = " / ".join(bits)
        if self.issuer and stake:
            return f"{stake} of {self.issuer}"
        return self.issuer or stake


def _first_number(number, one, tags: tuple[str, ...]) -> float | None:
    """First tag that yields a usable figure. Zero counts as no figure.

    A 13D/G exists because someone crossed 5%, so a reported 0.0 means the document was
    not parsed rather than that the stake is nothing.
    """
    for tag in tags:
        value = number(one(tag))
        if value:
            return value
    return None


def parse_stake_filing(xml: str) -> StakeDetail:
    """Pull the stake out of a 13D/G primary document. Never raises."""

    def one(tag: str) -> str | None:
        m = re.search(rf"<(?:\w+:)?{tag}>(.*?)</(?:\w+:)?{tag}>", xml, re.S | re.I)
        return unescape(m.group(1)).strip() if m else None

    def many(tag: str) -> list[str]:
        return [
            unescape(v).strip()
            for v in re.findall(rf"<(?:\w+:)?{tag}>(.*?)</(?:\w+:)?{tag}>", xml, re.S | re.I)
        ]

    def number(raw: str | None) -> float | None:
        if raw is None:
            return None
        try:
            return float(raw.replace(",", "").replace("%", "").strip())
        except ValueError:
            return None

    # 13G and 13D use different names for the same two facts, and both appear in the
    # wild: a 13G carries classPercent, a 13D carries percentOfClass. Trying one and
    # giving up is why the market-wide sweep produced issuers with no stake attached.
    percent = _first_number(
        number, one, ("classPercent", "percentOfClass", "percentageOfClassSecurities")
    )
    shares = _first_number(
        number,
        one,
        (
            "reportingPersonBeneficiallyOwnedAggregateNumberOfShares",
            "aggregateAmountOwned",
            "numberOfShares",
        ),
    )
    # Several reporting persons on one filing are usually the fund and the person who
    # runs it, reporting the same shares. Names are kept, the figure is not doubled.
    holders = []
    for name in many("reportingPersonName"):
        if name and name not in holders:
            holders.append(name)
    codes = []
    for code in many("typeOfReportingPerson"):
        code = code.strip().upper()
        if code and code not in codes:
            codes.append(code)
    return StakeDetail(
        issuer=one("issuerName") or "",
        issuer_cusip=(one("issuerCusipNumber") or "").upper(),
        percent=percent,
        shares=int(shares) if shares is not None else None,
        holders=holders,
        person_types=codes,
    )


def fetch_stake_detail(cik: str, accession: str) -> StakeDetail | None:
    """Read one 13D/G's primary document. None when it cannot be had."""
    if not accession or not cik:
        return None
    bare = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{bare}/primary_doc.xml"
    try:
        return parse_stake_filing(_get(url).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - detail is an enrichment, not a precondition
        return None


# Form 4 transaction codes, in the words a reader uses. Only the ones that describe a
# change of position are spelled out; anything else is shown as its bare code rather than
# guessed at.
TRANSACTION_CODES = {
    "P": "bought",
    "S": "sold",
    "A": "granted",
    "D": "disposed",
    "F": "withheld for tax",
    "M": "exercised derivative",
    "X": "exercised option",
    "G": "gifted",
    "C": "converted",
}


@dataclass(frozen=True)
class Trade:
    """One dated transaction from a Form 4. The only public filing that carries a date."""

    issuer: str
    symbol: str
    on: date | None
    code: str
    shares: int | None
    price: float | None
    acquired: bool

    @property
    def dollar_value(self) -> float | None:
        """shares * price, both copied from the Form 4 itself. None when either is
        missing, never estimated."""
        if self.shares and self.price:
            return self.shares * self.price
        return None

    @property
    def is_open_market(self) -> bool:
        """P (bought in the open market) or S (sold in the open market).

        Everything else -- A grants/awards, F tax withholding, M/X derivative
        exercises, G gifts, C conversions -- is not a discretionary decision to buy or
        sell, and ranking "largest trades" by dollar value would otherwise report a
        tax payment as a sale.
        """
        return self.code.upper() in {"P", "S"}

    def describe(self) -> str:
        verb = TRANSACTION_CODES.get(self.code.upper(), f"code {self.code}")
        parts = [verb]
        if self.shares:
            parts.append(f"{self.shares:,} shares")
        if self.issuer:
            parts.append(f"of {self.issuer}")
        if self.symbol:
            parts[-1] += f" ({self.symbol})"
        if self.price:
            parts.append(f"at ${self.price:,.2f}")
        if self.on:
            parts.append(f"on {self.on.isoformat()}")
        return " ".join(parts)


def parse_form4(xml: str) -> list[Trade]:
    """Dated transactions out of a Form 4 ownership document. Never raises."""

    def one(tag: str, scope: str) -> str | None:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", scope, re.S)
        if not m:
            return None
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() or None

    issuer = one("issuerName", xml) or ""
    symbol = one("issuerTradingSymbol", xml) or ""
    out: list[Trade] = []
    blocks = re.findall(
        r"<(?:nonDerivative|derivative)Transaction>(.*?)</(?:nonDerivative|derivative)Transaction>",
        xml,
        re.S,
    )
    for block in blocks:

        def number(tag: str, block: str = block) -> float | None:
            raw = one(tag, block)
            try:
                return float(raw.replace(",", "")) if raw else None
            except ValueError:
                return None

        shares = number("transactionShares")
        out.append(
            Trade(
                issuer=issuer,
                symbol=symbol,
                on=_parse_date(one("transactionDate", block)),
                code=(one("transactionCode", block) or "").strip(),
                shares=int(shares) if shares else None,
                price=number("transactionPricePerShare"),
                acquired=(one("transactionAcquiredDisposedCode", block) or "A") == "A",
            )
        )
    return out


def fetch_form4_trades(cik: str, accession: str) -> list[Trade]:
    """Read one Form 4's transactions. Empty when it cannot be had."""
    if not cik or not accession:
        return []
    bare = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{bare}/"
    try:
        listing = json.loads(_get(base + "index.json").decode())
        for name in (i["name"] for i in listing["directory"]["item"]):
            if name.endswith(".xml"):
                return parse_form4(_get(base + name).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - detail is an enrichment
        return []
    return []


@dataclass(frozen=True)
class FastFiling:
    """One stake disclosure, on the clock that actually moves.

    13F is quarterly and up to 135 days stale. These are not: a 13D is due within five
    business days of crossing 5%, an amendment within two of a material change, and a
    Form 4 within two of the trade once a holder passes 10%. EDGAR publishes them within
    seconds of acceptance, which is why a fund's moves are discussed the same week rather
    than the next quarter. This is that stream, filtered to the managers being tracked.
    """

    form: str
    filed: date
    accession: str = ""
    detail: StakeDetail | None = None
    trades: list[Trade] = field(default_factory=list)

    @property
    def url(self) -> str:
        """The EDGAR filing index page: form name, filer, issuer and dates.

        Not the bare accession directory. That renders as an unlabelled Apache file
        listing -- XML and HTML with no indication of what any of it is -- which is
        useless to a reader clicking through from the digest.
        """
        if not self.accession or not self._cik:
            return ""
        return filing_index_url(self._cik, self.accession)

    _cik: str = ""


@dataclass(frozen=True)
class PositionChanges:
    """Names opened, closed, added to and trimmed between two 13F filings."""

    opened: list[Holding] = field(default_factory=list)
    closed: list[Holding] = field(default_factory=list)
    increased: list[tuple[str, int]] = field(default_factory=list)
    decreased: list[tuple[str, int]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.opened or self.closed or self.increased or self.decreased)


@dataclass(frozen=True)
class QuarterActivity:
    """What a manager did over the quarter, as opposed to what it holds.

    Every field here is derivable from the 13F itself: counting positions that appeared
    and disappeared between two filings, and the reported market values of each.

    The same FMP response also carries `performance`, `performancePercentage` and
    `performanceRelativeToSP500Percentage`. Those are deliberately NOT read. They are the
    vendor's own computation, not a figure any filer disclosed, and this product's whole
    claim is that its numbers resolve to filings. A return figure that cannot be traced to
    a document does not belong in the same email as ones that can.
    """

    as_of: date | None
    portfolio_size: int
    added: int
    removed: int
    market_value: int
    previous_market_value: int

    @property
    def change_pct(self) -> float | None:
        if not self.previous_market_value:
            return None
        delta = self.market_value - self.previous_market_value
        return 100.0 * delta / self.previous_market_value


@dataclass(frozen=True)
class FundSnapshot:
    """What one manager last told the public, on both clocks."""

    name: str
    cik: str
    latest_13f_filed: date | None = None
    # Stock only. See the comment on `put_notional_usd`.
    total_value_usd: int = 0
    position_count: int = 0
    top_holdings: list[Holding] = field(default_factory=list)
    # Long option positions, reported separately and never folded into the figures
    # above. Three reasons, all of them about not saying something the filing does not:
    #  1. A 13F `value` on an option row is the notional of the underlying, not the
    #     premium paid, so adding it to stock value overstates capital at risk. SALP's
    #     2026-03-31 table is $3.86B of stock and $8.46B of put notional; the old code
    #     summed them and printed "$13.7B", 62% of which was puts.
    #  2. A put is not ownership. Printing the issuer of a put among a fund's holdings
    #     reports the opposite of what was filed.
    #  3. A 13F discloses long option positions only, and says nothing about what
    #     offsets them, so a put does not establish a net short view. The rendering
    #     therefore states the notional and the count and stops there.
    put_notional_usd: int = 0
    call_notional_usd: int = 0
    put_name_count: int = 0
    call_name_count: int = 0
    latest_fast_form: str | None = None
    latest_fast_date: date | None = None
    values_scaled_from_thousands: bool = False
    latest_13f_url: str = ""
    period_end: date | None = None
    prior_period_end: date | None = None
    prior_13f_url: str = ""
    activity: QuarterActivity | None = None
    changes: PositionChanges | None = None
    recent_filings: list[FastFiling] = field(default_factory=list)
    error: str | None = None

    @property
    def has_13f(self) -> bool:
        """Did we read a table at all?

        Not `position_count` alone: that counts stock only, and a manager whose whole
        table is options would otherwise vanish from the roster without a word.
        """
        return bool(self.position_count or self.put_name_count or self.call_name_count)

    def moved_recently(self, on: date, within_days: int = 30) -> bool:
        if self.latest_fast_date is None:
            return False
        return (on - self.latest_fast_date).days <= within_days


def parse_activity(rows: list[dict]) -> QuarterActivity | None:
    """Newest quarter from FMP's holder-performance-summary, or None.

    Returns None rather than a zeroed record when the shape is unrecognised: an activity
    line reading "added 0, exited 0" is a claim about the manager, and we would be making
    it up.
    """
    if not rows:
        return None
    # FMP returns about six quarters, newest first as of 2026-08 (verified against
    # CIK 0002045724: 2026-03-31 down to 2024-12-31). Picking the newest date rather
    # than trusting that order costs nothing and stops a vendor-side reordering from
    # silently dating the section eighteen months in the past.
    row = max(
        rows,
        key=lambda r: _parse_date(r.get("date")) or date.min,
    )
    try:
        return QuarterActivity(
            as_of=_parse_date(row.get("date")),
            portfolio_size=int(row["portfolioSize"]),
            added=int(row["securitiesAdded"]),
            removed=int(row["securitiesRemoved"]),
            market_value=int(row["marketValue"]),
            previous_market_value=int(row.get("previousMarketValue") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def fetch_activity(cik: str, api_key: str) -> QuarterActivity | None:
    """One call per manager. Never raises; the section survives without it."""
    if not api_key:
        return None
    url = (
        "https://financialmodelingprep.com/stable/institutional-ownership/"
        f"holder-performance-summary?cik={cik}&apikey={api_key}"
    )
    try:
        return parse_activity(json.loads(_get(url).decode()))
    except Exception:  # noqa: BLE001 - an optional enrichment must not cost the section
        return None


def filing_index_url(cik: str, accession: str) -> str:
    """The EDGAR page a human reads, not the raw accession directory.

    The directory (`.../{bare}/`) lists every XML and HTML file in the filing with no
    labels, which renders as gibberish to a reader who is not looking for a specific
    file by name. The index page (`.../{bare}/{accession}-index.htm`) is what EDGAR
    itself renders when you click through from a filing search: form type, filer,
    subject company, and dates. Verified working:
    https://www.sec.gov/Archives/edgar/data/1067983/000119312526333151/
    0001193125-26-333151-index.htm
    """
    if not cik or not accession:
        return ""
    bare = accession.replace("-", "")
    # Some callers (the FMP filing-search path) hand this an accession already
    # stripped of its dashes. The index filename always carries them, so a bare
    # 18-digit accession is redashed into the standard 10-2-6 grouping rather than
    # producing a filename EDGAR does not serve.
    dashed = (
        accession
        if "-" in accession
        else (f"{bare[:10]}-{bare[10:12]}-{bare[12:]}" if len(bare) == 18 else bare)
    )
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{bare}/{dashed}-index.htm"


def _get(url: str, *, timeout: int = 30) -> bytes:
    from whale_agent.config import get_settings

    # The configured contact (WHALE_SEC_USER_AGENT), not the placeholder constant.
    req = urllib.request.Request(url, headers={"User-Agent": get_settings().sec_user_agent})
    time.sleep(_SEC_PAUSE)
    return urllib.request.urlopen(req, timeout=timeout).read()


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def parse_infotable(xml: str) -> list[Holding]:
    """Pull holdings out of a 13F information table.

    Regex rather than an XML parser because filers vary the namespace prefix freely
    (`<infoTable>`, `<ns1:infoTable>`, others) and a strict parser rejects real filings
    that readers can see rendered fine on EDGAR. The fields wanted are flat and
    unambiguous, so the looser tool is the correct one here.
    """

    def field_of(block: str, tag: str) -> str | None:
        m = re.search(rf"<(?:\w+:)?{tag}>(.*?)</(?:\w+:)?{tag}>", block, re.S)
        return m.group(1).strip() if m else None

    # (issuer, option type) -> (value, shares). A 13F carries one row per share class and
    # per manager with investment discretion, so a single issuer legitimately appears
    # several times. Printing them separately reads as a duplication bug and understates
    # the position. Rows of different `putCall` are NOT merged: see Holding.key.
    merged: dict[str, tuple[int, int | None, str, str]] = {}
    order: list[tuple[str, str]] = []
    for block in re.findall(r"<(?:\w+:)?infoTable>(.*?)</(?:\w+:)?infoTable>", xml, re.S):
        issuer = field_of(block, "nameOfIssuer")
        raw_value = field_of(block, "value")
        if not issuer or raw_value is None:
            continue
        try:
            value = int(float(raw_value))
        except ValueError:
            continue
        cusip = (field_of(block, "cusip") or "").strip().upper()
        raw_shares = field_of(block, "sshPrnamt")
        try:
            shares = int(float(raw_shares)) if raw_shares else None
        except ValueError:
            shares = None
        # Filers write issuer names as XML-escaped text: "AT&amp;T", "S&amp;P".
        issuer = unescape(issuer).strip()
        # An absent putCall is an ordinary long position in the security itself. Filers
        # write the value as "Put"/"Call" (SALP) or "PUT"/"CALL"; normalise to upper.
        option_type = (field_of(block, "putCall") or "").strip().upper()
        # Fail closed on anything present but unreadable. An absent or blank tag is a
        # stock position and stays "", but mapping an unrecognised value to "" would
        # reintroduce the original defect quietly: an option counted as ownership, for
        # whichever filer writes "P" or a word the form does not define. Keeping the raw
        # value marks it as an option for the split and for the merge key, and leaves it
        # out of the stock total.
        if option_type not in {"PUT", "CALL"} and option_type:
            option_type = f"OTHER:{option_type}"
        # Merge on CUSIP where present: one issuer's share classes are one position.
        # Never across position types, so a put never lands on top of a long.
        key = f"{cusip or issuer.upper()}|{option_type}"
        if key in merged:
            prev_value, prev_shares, prev_cusip, _ = merged[key]
            total_shares = (
                (prev_shares or 0) + (shares or 0)
                if (prev_shares is not None or shares is not None)
                else None
            )
            merged[key] = (prev_value + value, total_shares, prev_cusip or cusip, option_type)
        else:
            merged[key] = (value, shares, cusip, option_type)
            order.append((key, issuer))
    return [
        Holding(
            issuer=name,
            value_usd=merged[k][0],
            shares=merged[k][1],
            cusip=merged[k][2],
            option_type=merged[k][3],
        )
        for k, name in order
    ]


def _13f_documents(
    cik: str, recent: dict, want: int = 2
) -> list[tuple[date, str, date | None, str]]:
    """(filing date, information-table URL, period end, accession) for the newest `want`
    13F-HR filings.

    Two by default, because one table says what a manager holds and two say what it
    did -- which names it opened, which it closed, and which it changed its mind about.
    A count of positions added tells a reader nothing they can act on; a name does.

    The accession is carried through rather than re-derived from the information-table
    URL, because the readable EDGAR index page (`filing_index_url`) needs the accession
    with its dashes intact, and the information-table URL only ever had the dashes
    stripped.
    """
    forms = recent.get("form", [])
    reports = recent.get("reportDate", [])
    out: list[tuple[date, str, date | None, str]] = []
    for i, form in enumerate(forms):
        if form != "13F-HR" or len(out) >= want:
            continue
        filed = _parse_date(recent["filingDate"][i])
        if not filed:
            continue
        # The period the snapshot describes, which is the date that actually matters and
        # is six to eight weeks earlier than the filing date the reader would otherwise
        # be shown.
        period = _parse_date(reports[i]) if i < len(reports) else None
        accession = recent["accessionNumber"][i]
        bare = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{bare}/"
        try:
            listing = json.loads(_get(base + "index.json").decode())
        except Exception:  # noqa: BLE001 - a missing prior quarter is not fatal
            continue
        for name in (item["name"] for item in listing["directory"]["item"]):
            if name.endswith(".xml") and name != "primary_doc.xml":
                out.append((filed, base + name, period, accession))
                break
    return out


def diff_holdings(
    current: list[Holding], prior: list[Holding], top: int = 4
) -> PositionChanges:
    """What changed between two quarters, by name.

    Opened and closed positions are exact set differences. "Added to" and "trimmed" are
    ranked by change in reported value rather than share count, because share counts are
    not comparable across a split and value is what the filer actually reported.
    """
    now = {h.key: h for h in current}
    was = {h.key: h for h in prior}
    opened = [h for h in current if h.key not in was]
    closed = [h for h in prior if h.key not in now]
    # Label with the option type where there is one. The keys already separate a put
    # from the stock, but projecting to the bare issuer name merges them back together
    # in the output: two entries both reading "NVIDIA", one of them an option.
    moved: list[tuple[str, int]] = [
        (
            f"{now[k].issuer} ({now[k].option_type})" if now[k].is_option else now[k].issuer,
            now[k].value_usd - was[k].value_usd,
        )
        for k in now.keys() & was.keys()
    ]
    increased = sorted([m for m in moved if m[1] > 0], key=lambda m: m[1], reverse=True)
    decreased = sorted([m for m in moved if m[1] < 0], key=lambda m: m[1])
    return PositionChanges(
        opened=sorted(opened, key=lambda h: h.value_usd, reverse=True)[:top],
        closed=sorted(closed, key=lambda h: h.value_usd, reverse=True)[:top],
        increased=increased[:top],
        decreased=decreased[:top],
    )


def _latest_13f_document(cik: str, recent: dict) -> tuple[date | None, str | None]:
    """Return (filing date, information-table URL) for the newest 13F-HR."""
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form != "13F-HR":
            continue
        accession = recent["accessionNumber"][i]
        filed = _parse_date(recent["filingDate"][i])
        bare = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{bare}/"
        listing = json.loads(_get(base + "index.json").decode())
        names = [item["name"] for item in listing["directory"]["item"]]
        # The information table is the XML that is not the cover page. Filers name it
        # freely, so it is identified by exclusion rather than by a fixed filename.
        for name in names:
            if name.endswith(".xml") and name != "primary_doc.xml":
                return filed, base + name
        return filed, None
    return None, None


def _latest_fast_filing(recent: dict) -> tuple[str | None, date | None]:
    forms = recent.get("form", [])
    best: tuple[str, date] | None = None
    for i, form in enumerate(forms):
        if form.upper() not in FAST_FORMS:
            continue
        filed = _parse_date(recent["filingDate"][i])
        if filed and (best is None or filed > best[1]):
            best = (form, filed)
    return best if best else (None, None)


def fast_filings_since(recent: dict, since: date, cik: str = "") -> list[FastFiling]:
    """Every fast-clock filing on or after `since`, newest first.

    This is the difference between reporting a portfolio and reporting news. The
    quarterly snapshot says what a manager held in March; these say what it did last
    week, and they are the filings people actually read in real time.
    """
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    out: list[FastFiling] = []
    for i, form in enumerate(forms):
        if form.upper() not in FAST_FORMS:
            continue
        filed = _parse_date(dates[i] if i < len(dates) else None)
        if not filed or filed < since:
            continue
        out.append(
            FastFiling(
                form=form,
                filed=filed,
                accession=accessions[i] if i < len(accessions) else "",
                _cik=str(int(cik)) if cik else "",
            )
        )
    out.sort(key=lambda f: f.filed, reverse=True)
    return out


def fetch_snapshot(
    name: str,
    cik: str,
    *,
    top_n: int = 5,
    api_key: str = "",
    since_date: date | None = None,
    store: Store | None = None,
) -> FundSnapshot:
    """One manager's latest 13F plus its most recent fast filing.

    Never raises. A manager that cannot be fetched is reported as an error line in the
    weekly rather than taking the whole section, and by extension the send, down with it.

    When `store` is given, every information table read is persisted to
    `thirteenf_holdings` (see `ingestion.position_history`), and the prior quarter is
    read from that history instead of EDGAR whenever it is already on file -- only the
    first time a manager's prior quarter is needed does this fetch it live. That is
    what lets "what did they hold last quarter" answer instantly on every run after the
    first, without doubling the network cost of this function forever.
    """
    try:
        submissions = json.loads(
            _get(f"https://data.sec.gov/submissions/CIK{cik}.json").decode()
        )
        recent = submissions.get("filings", {}).get("recent", {})
        fast_form, fast_date = _latest_fast_filing(recent)
        recent_filings = fast_filings_since(recent, since_date, cik) if since_date else []
        enriched: list[FastFiling] = []
        for f in recent_filings:
            if f.form.upper().startswith("SCHEDULE 13"):
                enriched.append(replace(f, detail=fetch_stake_detail(cik, f.accession)))
            elif f.form.strip() in {"3", "4", "5"}:
                enriched.append(replace(f, trades=fetch_form4_trades(cik, f.accession)))
            else:
                enriched.append(f)
        recent_filings = enriched
        documents = _13f_documents(cik, recent, want=2)
        filed = documents[0][0] if documents else None
        period_end = documents[0][2] if documents else None
        prior_period_end = documents[1][2] if len(documents) > 1 else None
        # The filing index page, not the raw XML and not the bare accession directory: a
        # reader clicking through wants the page EDGAR renders, and every figure in this
        # section is re-derivable from it.
        prior_13f_url = filing_index_url(cik, documents[1][3]) if len(documents) > 1 else ""
        latest_13f_url = filing_index_url(cik, documents[0][3]) if documents else ""

        holdings: list[Holding] = []
        if documents:
            holdings = parse_infotable(_get(documents[0][1]).decode("utf-8", errors="replace"))

        # The units test is against the whole table, options included, because every row
        # of one filing is reported in the same units.
        table_total = sum(h.value_usd for h in holdings)
        scaled = False
        if holdings and table_total < THIRTEEN_F_FLOOR_USD:
            # Reported in thousands. See THIRTEEN_F_FLOOR_USD.
            holdings = [replace(h, value_usd=h.value_usd * 1000) for h in holdings]
            scaled = True

        holdings.sort(key=lambda h: h.value_usd, reverse=True)
        stock = [h for h in holdings if not h.is_option]
        puts = [h for h in holdings if h.option_type == "PUT"]
        calls = [h for h in holdings if h.option_type == "CALL"]

        # The "what did they hold before" question .
        # Persisted so it never has to be answered by a live fetch twice: the prior
        # quarter is read out of `store` when a previous run already recorded it, and
        # only fetched from EDGAR the first time it is needed. Nothing here is fetched
        # or persisted when the caller passes no store -- existing callers (tests,
        # anything not opted into history) are unaffected.
        changes: PositionChanges | None = None
        if store is not None and period_end and documents and holdings:
            from whale_agent.ingestion.position_history import persist_snapshot_holdings

            prior_holdings: list[Holding] = []
            prior_accession = ""
            if len(documents) > 1 and prior_period_end:
                _pf, prior_doc_url, _pp, prior_accession = documents[1]
                if prior_period_end in store.report_periods_on_file(cik):
                    # Already recorded by an earlier run -- read it back rather than
                    # refetching EDGAR for a document we already have.
                    prior_holdings = [
                        Holding(
                            issuer=r["issuer"],
                            value_usd=r["value_usd"],
                            shares=r["shares"],
                            cusip=r["cusip"] or "",
                            option_type=r["option_type"] or "",
                        )
                        for r in store.holdings_for_period(cik, prior_period_end)
                    ]
                else:
                    try:
                        prior_holdings = parse_infotable(
                            _get(prior_doc_url).decode("utf-8", errors="replace")
                        )
                        prior_total = sum(h.value_usd for h in prior_holdings)
                        if prior_holdings and prior_total < THIRTEEN_F_FLOOR_USD:
                            prior_holdings = [
                                replace(h, value_usd=h.value_usd * 1000)
                                for h in prior_holdings
                            ]
                    except Exception:  # noqa: BLE001 - history is an enrichment, not a precondition
                        prior_holdings = []

            persist_snapshot_holdings(
                store,
                manager_cik=cik,
                current=(period_end, documents[0][3], latest_13f_url, filed, holdings),
                prior=(
                    (prior_period_end, prior_accession, prior_13f_url, None, prior_holdings)
                    if prior_holdings and prior_period_end
                    else None
                ),
            )
            if prior_holdings:
                changes = diff_holdings(holdings, prior_holdings)

        return FundSnapshot(
            name=name,
            cik=cik,
            latest_13f_filed=filed,
            total_value_usd=sum(h.value_usd for h in stock),
            position_count=len(stock),
            top_holdings=stock[:top_n],
            put_notional_usd=sum(h.value_usd for h in puts),
            call_notional_usd=sum(h.value_usd for h in calls),
            put_name_count=len(puts),
            call_name_count=len(calls),
            latest_fast_form=fast_form,
            latest_fast_date=fast_date,
            values_scaled_from_thousands=scaled,
            latest_13f_url=latest_13f_url,
            period_end=period_end,
            prior_period_end=prior_period_end,
            prior_13f_url=prior_13f_url,
            activity=fetch_activity(cik, api_key),
            recent_filings=recent_filings,
            changes=changes,
        )
    except Exception as exc:  # noqa: BLE001 - one bad manager must not cost the send
        return FundSnapshot(name=name, cik=cik, error=redact(exc, [api_key]))


# 13D/G filings anywhere in the market, not only from the eleven names on the watchlist.
# A watchlist answers "what did the funds I chose do"; it cannot answer "who took a big
# position this week", and the second question is the one a reader actually has. Whoever
# crosses 5% has to file, so this catches the fund nobody was watching.
STAKE_FORMS = ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A")

# The same forms as EDGAR's own index spells them. The vendor filing search says
# "SC 13D"; the daily index says "SCHEDULE 13D". Matching on a prefix covers the
# amendments ("SCHEDULE 13G/A") without listing every combination.
_INDEX_STAKE_PREFIX = "SCHEDULE 13"

_ACCESSION = re.compile(r"(\d{10}-\d{2}-\d{6})")


@dataclass(frozen=True)
class IndexedFiling:
    """One filing as EDGAR's daily index lists it: form, filer CIK, accession."""

    form: str
    cik: str
    accession: str


def parse_daily_index(text: str, forms: tuple[str, ...] | None = None) -> list[IndexedFiling]:
    """Stake filings out of one `form.YYYYMMDD.idx`, deduped by accession. Never raises.

    Why this exists rather than another page of the vendor's filing search: that search
    returns at most 100 rows per form type per request, and the sweep sorted newest
    first before capping. Measured over 2026-07-28 to 2026-08-04 it surfaced 96 filings
    where the index listed 2,246. A section headed "every 13D/G this week, ranked by
    size" cannot be built on a sample whose selection criterion is a page limit.

    EDGAR lists each stake filing twice, once under the subject issuer and once under
    the filer, so the raw row count is roughly double the filing count. The accession
    number is the filing's identity and is what the two rows share.
    """
    # `forms` selects by exact form name; without it the stake-form prefix applies, which
    # is what the 13D/G caller wants (it needs SCHEDULE 13D, 13G and both amendments).
    # Exact matching matters for the registration caller: a prefix on "424B" would pull
    # in ~1,000 424B2 shelf takedowns a day, each costing a full document fetch.
    wanted = set(forms) if forms else None
    out: list[IndexedFiling] = []
    seen: set[str] = set()
    for line in text.splitlines():
        # The form column is fixed width but wider than the shortest form name, so a
        # fixed slice truncates "SCHEDULE 13G/A" into "SCHEDULE 13G" and silently
        # relabels every amendment as an original. Split on the column gap instead.
        form = re.split(r"\s{2,}", line.strip())[0].strip()
        if wanted is not None:
            if form not in wanted:
                continue
        elif not form.startswith(_INDEX_STAKE_PREFIX):
            continue
        m = _ACCESSION.search(line)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        cik = ""
        parts = line.split()
        for token in parts:
            if token.isdigit() and 4 <= len(token) <= 10:
                cik = token
                break
        out.append(IndexedFiling(form=form, cik=cik, accession=m.group(1)))
    return out


@dataclass(frozen=True)
class MarketStake:
    """One stake disclosure from anywhere in the market."""

    filed: date
    form: str
    holders: list[str]
    detail: StakeDetail
    url: str = ""
    accession: str = ""  # bare (dashless) form, same as PooledTrade/PooledStake


def daily_index_url(day: date) -> str:
    quarter = (day.month - 1) // 3 + 1
    return (
        f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{quarter}/"
        f"form.{day.strftime('%Y%m%d')}.idx"
    )


def fetch_index_candidates(
    since: date,
    until: date,
    *,
    pause: float = _SEC_PAUSE,
    forms: tuple[str, ...] | None = None,
) -> list[tuple[date, str, str, str]]:
    """Every stake filing EDGAR indexed between the two dates, one row per filing.

    Walks the daily index day by day. A missing day is normal and not an error: there
    is no index on weekends or holidays, and the current day's index is published after
    the fact, so a Monday morning run finds nothing for Monday. Those days fall through
    to the vendor search, which is live but capped.
    """
    out: list[tuple[date, str, str, str]] = []
    day = since
    while day <= until:
        try:
            text = _get(daily_index_url(day)).decode("latin-1")
        except Exception:  # noqa: BLE001 - no index for this day is expected, not fatal
            day += timedelta(days=1)
            continue
        for row in parse_daily_index(text, forms):
            out.append((day, row.form, row.cik, row.accession))
        time.sleep(pause)
        day += timedelta(days=1)
    return out


def fetch_market_stakes(
    api_key: str,
    since: date,
    until: date,
    *,
    forms: tuple[str, ...] = STAKE_FORMS,
    max_details: int = 1500,
    budget_seconds: float = 600.0,
    with_count: bool = False,
):
    """Stake filings market-wide in the window, richest first.

    Discovery comes from EDGAR's daily index, which is complete, and falls back to the
    vendor filing search for days the index has not published yet. The figures then come
    from each filing's own primary document, because neither index carries stake size.

    The old path used the vendor search alone and kept the newest 25 rows. Over
    2026-07-28 to 2026-08-04 that examined 25 filings out of 1,123, reaching back only
    two days, while the section above it claimed to rank every 13D/G of the week. The
    cap is now high enough to cover a normal week outright, with a wall-clock budget so
    an unusually heavy week degrades to a partial ranking instead of hanging the job.
    """
    # The two sources spell the accession differently: the index dashes it
    # (0001213900-26-084889), the vendor's URL does not. Deduping on the raw string
    # would let every index filing back in a second time through the vendor path.
    seen: set[str] = set()
    candidates: list[tuple[date, str, str, str]] = []
    for filed, form, cik, accession in fetch_index_candidates(since, until):
        bare = accession.replace("-", "")
        if bare in seen:
            continue
        seen.add(bare)
        candidates.append((filed, form, cik, bare))

    for form in forms:
        if not api_key:
            break
        url = (
            "https://financialmodelingprep.com/stable/sec-filings-search/form-type"
            f"?formType={urllib.parse.quote(form)}&from={since.isoformat()}"
            f"&to={until.isoformat()}&apikey={api_key}"
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
                candidates.append((filed, row.get("formType") or form, m.group(1), m.group(2)))

    candidates.sort(reverse=True)
    out: list[MarketStake] = []
    started = time.monotonic()
    for filed, form, cik, accession in candidates[:max_details]:
        # A partial ranking is worth more than a missed send. Whatever has been read by
        # the time the budget runs out is still every filing that was read, ranked
        # honestly; the caller reports how much of the week it covers.
        if time.monotonic() - started > budget_seconds:
            break
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/"
        try:
            detail = parse_stake_filing(
                _get(base + "primary_doc.xml").decode("utf-8", "replace")
            )
        except Exception:  # noqa: BLE001 - an unreadable filing is skipped, not fatal
            continue
        if not detail.issuer or detail.percent is None:
            continue
        out.append(
            MarketStake(
                filed=filed,
                form=form,
                holders=detail.holders,
                detail=detail,
                url=base,
                accession=accession,
            )
        )
    out.sort(key=lambda s: s.detail.percent or 0, reverse=True)
    # The number discovered, not the number parsed: the header quotes it to say how much
    # of the week the ranking actually covers, and a filing whose document would not
    # parse was still filed.
    return (out, len(candidates)) if with_count else out


def filings_from_fmp_rows(rows: list[dict], since: date, until: date) -> list[FastFiling]:
    """Turn FMP filing-search rows into FastFilings. Pure, so it is testable offline.

    Every form type is kept. The previous path filtered to FAST_FORMS and threw away
    13F-HR/A amendments, Form 5, and anything else a fund files -- a filter chosen when
    the report only had two sections to fill.
    """
    seen: set[str] = set()
    out: list[FastFiling] = []
    for row in rows:
        link = row.get("finalLink") or row.get("link") or ""
        m = re.search(r"/data/(\d+)/(\d+)/", link)
        if not m or m.group(2) in seen:
            continue
        filed = _parse_date((row.get("filingDate") or "")[:10])
        if not filed or filed < since or filed > until:
            continue
        seen.add(m.group(2))
        out.append(
            FastFiling(
                form=(row.get("formType") or "").strip(),
                filed=filed,
                accession=m.group(2),
                _cik=m.group(1),
            )
        )
    out.sort(key=lambda f: f.filed, reverse=True)
    return out


def fetch_all_filings(api_key: str, cik: str, since: date, until: date) -> list[FastFiling]:
    """Every filing by one registrant in the window. Never raises."""
    if not api_key:
        return []
    url = (
        "https://financialmodelingprep.com/stable/sec-filings-search/cik"
        f"?cik={cik}&from={since.isoformat()}&to={until.isoformat()}"
        f"&limit=100&apikey={api_key}"
    )
    try:
        return filings_from_fmp_rows(json.loads(_get(url).decode()), since, until)
    except Exception:  # noqa: BLE001 - one registrant failing is not fatal
        return []


def fetch_watchlist(
    watchlist: dict[str, str] | None = None,
    *,
    top_n: int = 5,
    api_key: str = "",
    since_date: date | None = None,
    store: Store | None = None,
) -> list[FundSnapshot]:
    """Snapshots for every tracked manager, largest reported portfolio first.

    `store`, when given, is threaded into `fetch_snapshot` for every manager so each
    one's 13F history accrues and the prior-quarter question (`ingestion.position_history
    .prior_position`) can be answered without a live fetch on every subsequent run.
    """
    entries = watchlist if watchlist is not None else WATCHLIST
    snapshots = [
        fetch_snapshot(n, c, top_n=top_n, api_key=api_key, since_date=since_date, store=store)
        for n, c in entries.items()
    ]
    snapshots.sort(key=lambda s: s.total_value_usd, reverse=True)
    return dedupe_joint_filings(snapshots)


def dedupe_joint_filings(snapshots: list[FundSnapshot]) -> list[FundSnapshot]:
    """Collapse related entities that filed one joint 13F.

    Affiliated managers routinely file a single table covering all of them, so two CIKs
    return byte-identical holdings. Printing both doubles the apparent money in the
    section, which is the kind of error a reader would catch and never trust again.
    """
    seen: set[tuple] = set()
    out: list[FundSnapshot] = []
    for snap in snapshots:
        if snap.error is not None or not snap.has_13f:
            out.append(snap)
            continue
        # The option totals are part of the fingerprint: two affiliates whose stock
        # books match but whose option rows do not did not file the same table.
        key = (
            snap.latest_13f_filed,
            snap.total_value_usd,
            snap.position_count,
            snap.put_notional_usd,
            snap.call_notional_usd,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(snap)
    return out


@dataclass(frozen=True)
class PooledTrade:
    """One Form 4 open-market trade, pooled across every tracked manager.

    Only P (bought in the open market) and S (sold in the open market) transactions
    are pooled; grants, tax withholding and other non-discretionary codes carry no
    market decision to rank and are excluded rather than mislabelled as one.
    """

    manager: str
    manager_cik: str
    direction: str  # "bought" or "sold"
    issuer: str
    symbol: str
    shares: int
    price: float
    dollar_value: float
    filed: date
    form: str
    accession: str
    url: str
    # Trade size relative to the manager's disclosed 13F portfolio value. None when
    # the manager has no 13F on file or its value is zero/unknown -- never a
    # division by zero, and never a made-up percentage.
    portfolio_value_usd: int | None = None
    percent_of_portfolio: float | None = None


@dataclass(frozen=True)
class PooledStake:
    """One SC 13D/13G stake disclosure, pooled across every tracked manager.

    Ranked by percent of class, the only size figure a 13D/G discloses; no dollar
    value is invented for it -- these filings carry no price. `is_new` is read off the
    form string itself: an initial filing (no "/A") is a new position being disclosed,
    while an amendment does not, on its own, say whether the stake grew or shrank.
    """

    manager: str
    manager_cik: str
    issuer: str
    percent: float
    shares: int | None
    filed: date
    form: str
    is_new: bool
    accession: str
    url: str


def pool_top_trades(snapshots: list[FundSnapshot], top: int = 5) -> list[PooledTrade]:
    """The largest open-market trades across every tracked manager, ranked by trade
    size relative to the manager's own disclosed 13F portfolio value -- not by raw
    dollars, which just re-ranks by whoever runs the biggest fund.

    Three honesty rules, in order:
      1. One filing, one row. Several transaction lines sharing one accession number
         are the same Form 4, so they are summed into a single row (shares added,
         price averaged share-weighted) rather than printed as separate trades.
      2. One row per manager in the ranked list, so the top-N names five different
         managers instead of one manager's five biggest filings.
      3. Ranked by trade_value / portfolio_value. A manager with no 13F on file, or
         whose portfolio value is zero or unknown, is never divided by zero and never
         silently dropped -- its filing lands in the second, dollar-ranked list
         instead, with `percent_of_portfolio` left as None.

    Dollar value itself is still shares * price, both copied from the Form 4. A
    trade missing either field is left out rather than guessed at.
    """
    # Step 1: one row per (manager, accession) filing.
    by_key: dict[tuple[str, str], list[tuple[Trade, FastFiling]]] = {}
    for snap in snapshots:
        if snap.error is not None:
            continue
        for f in snap.recent_filings:
            for tr in f.trades:
                if not tr.is_open_market:
                    continue
                if tr.dollar_value is None:
                    continue
                key = (snap.name, f.accession or f"{snap.name}:{f.filed.isoformat()}")
                by_key.setdefault(key, []).append((tr, f))

    filings: list[PooledTrade] = []
    for (manager, _accession), pairs in by_key.items():
        snap = next(s for s in snapshots if s.name == manager)
        total_shares = sum(tr.shares or 0 for tr, _ in pairs)
        total_value = sum(tr.dollar_value or 0.0 for tr, _ in pairs)
        avg_price = (total_value / total_shares) if total_shares else 0.0
        # Direction of the largest single leg -- a filing overwhelmingly on one side
        # is fairly described by that side even when a small opposite leg is present.
        biggest = max(pairs, key=lambda pair: pair[0].dollar_value or 0.0)
        tr0, f0 = biggest
        portfolio_value = (
            snap.total_value_usd if snap.has_13f and snap.total_value_usd else None
        )
        percent = (100.0 * total_value / portfolio_value) if portfolio_value else None
        filings.append(
            PooledTrade(
                manager=manager,
                manager_cik=snap.cik,
                direction="bought" if tr0.acquired else "sold",
                issuer=tr0.issuer,
                symbol=tr0.symbol,
                shares=total_shares,
                price=avg_price,
                dollar_value=total_value,
                filed=f0.filed,
                form=f0.form,
                accession=f0.accession,
                url=f0.url,
                portfolio_value_usd=portfolio_value,
                percent_of_portfolio=percent,
            )
        )

    # Step 2: one row per manager -- keep each manager's single best candidate
    # (best by percent when rankable, otherwise by raw dollars).
    best_per_manager: dict[str, PooledTrade] = {}
    for row in filings:
        current = best_per_manager.get(row.manager)
        if current is None:
            best_per_manager[row.manager] = row
            continue
        row_key = (
            row.percent_of_portfolio
            if row.percent_of_portfolio is not None
            else row.dollar_value
        )
        cur_key = (
            current.percent_of_portfolio
            if current.percent_of_portfolio is not None
            else current.dollar_value
        )
        # Prefer a rankable trade over an unrankable one regardless of size; between
        # two of the same kind, prefer the bigger one.
        row_rankable = row.percent_of_portfolio is not None
        cur_rankable = current.percent_of_portfolio is not None
        if (
            row_rankable
            and not cur_rankable
            or row_rankable == cur_rankable
            and row_key > cur_key
        ):
            best_per_manager[row.manager] = row

    ranked = sorted(
        (r for r in best_per_manager.values() if r.percent_of_portfolio is not None),
        key=lambda t: t.percent_of_portfolio,
        reverse=True,
    )[:top]
    unrankable = sorted(
        (r for r in best_per_manager.values() if r.percent_of_portfolio is None),
        key=lambda t: t.dollar_value,
        reverse=True,
    )[:top]
    return ranked + unrankable


def pool_top_stakes(snapshots: list[FundSnapshot], top: int = 5) -> list[PooledStake]:
    """The largest new or amended stakes across every tracked manager, by percent of
    class. No dollar figure: a 13D/G does not carry a price."""
    out: list[PooledStake] = []
    for snap in snapshots:
        if snap.error is not None:
            continue
        for f in snap.recent_filings:
            if not f.form.upper().startswith("SCHEDULE 13"):
                continue
            if f.detail is None or f.detail.percent is None:
                continue
            out.append(
                PooledStake(
                    manager=snap.name,
                    manager_cik=snap.cik,
                    issuer=f.detail.issuer,
                    percent=f.detail.percent,
                    shares=f.detail.shares,
                    filed=f.filed,
                    form=f.form,
                    is_new="/A" not in f.form.upper(),
                    accession=f.accession,
                    url=f.url,
                )
            )
    out.sort(key=lambda s: s.percent, reverse=True)
    return out[:top]
