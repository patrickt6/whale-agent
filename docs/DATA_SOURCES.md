# Data sources

Every source, its auth, and the exact vendor field names the parser reads. The field
lists are the point of this document: golden-file tests cannot notice a vendor renaming a
key, so when a source goes quiet the first question is always "did the field move?"

Verify against live endpoints with:

```bash
./whale python -m whale_agent.jobs.verify_sources
```

Note what that job actually covers: FMP and Quiver only, and only when their keys are
configured. **SEC, Taiwan, Japan, Dataroma, Arkham, Whale Alert and the gov sources are
not probed by anything.** A schema change there surfaces as a quiet source.

## How a source gets to run

`ingestion/registry.py:53` `build_sources()` constructs a `SourceSpec` per adapter. Each
spec's `enabled` is computed from the matching `Settings.*_enabled` property, which is
almost always *credential present* **and** *flag not turned off*. A disabled spec carries
a `disabled_reason` string that becomes a coverage note on the digest.

`WHALE_SOURCES` is an allowlist applied last (`registry.py:147`): if it is non-empty,
every source not named in it is force-disabled with reason `"not in WHALE_SOURCES"`.
Blank means "everything configured runs". This is the switch to use when debugging one
adapter.

A source failing is never a run failing. See rule 4 in [`README.md`](README.md).

## Event sources (produce `NormalizedEvent`, feed the daily digest)

### SEC Form 4 — `UsSecForm4Adapter`
`ingestion/us_sec.py:96` · key `sec_form4` · US · free, no key · **on by default,
unconditionally**

Fetched through the `edgartools` library (`edgar.get_filings(form="4")`), not raw HTTP.
`WHALE_SEC_USER_AGENT` is passed to `edgar.set_identity()` — the SEC requires a real
contact string and will block you without one.

Event types come from `FORM4_CODE_MAP` (`us_sec.py:230`): open-market buy/sell, grant,
option exercise, scheduled 10b5-1 sale.

XML fields read: `documentType`, `issuer/issuerName`, `issuer/issuerCik`,
`reportingOwner/reportingOwnerId/rptOwnerName`, `reportingOwnerId/rptOwnerCik`,
`nonDerivativeTable/nonDerivativeTransaction`, `transactionCoding/transactionCode`,
`transactionCoding/equitySwapInvolved`, `transactionCoding/transactionTimeliness`,
`transactionAmounts/transactionShares`, `transactionAmounts/transactionPricePerShare`,
`transactionAmounts/transactionAcquiredDisposedCode`, `transactionDate`.

Ownership XML carries a CIK and no ticker. `enrichment/tickers.py` translates; without it
these events silently skip every ticker-keyed enrichment while vendor events keep theirs.

### SEC SC 13D/G — `UsSec13DGAdapter`
`ingestion/us_sec_13d.py:32` · key `sec_13dg` · US · free · **on by default**

`edgar.get_filings(form="SC 13D")`. Produces `ACTIVIST_13D` / `PASSIVE_13G`.

Cover-page attributes read: `issuer_info.cik`, `subject_company_cik`, `filing.cik`,
`obj.cusip` / `issuer_info.cusip`, `obj.subject_company`, `issuer_info.name`,
`filing.company`, `obj.filer_name`, `obj.percent_of_class`, `obj.aggregate_amount`.

Keyed on the **subject company's** CIK, not the filer's. The fallback chain is at
`us_sec_13d.py:58-67` and exists because the field is not always populated.

### FMP insider trading — `FmpInsiderAdapter`
`ingestion/vendors/fmp.py:107` · key `fmp_insider` · US · **paid** · needs `FMP_API_KEY`
and `WHALE_ENABLE_FMP`

`GET https://financialmodelingprep.com/stable/insider-trading/search?apikey=…`

Fields: `transactionDate`, `filingDate`, `transactionType`, `price`, `url`/`link`,
`companyName`, `symbol`, `companyCik`, `reportingName`, `reportingCik`,
`securitiesTransacted`.

Two live hazards here: `price` sometimes
holds a transaction total rather than a per-share price, and `companyName` comes back
null often enough that issuer names fall through to the bare ticker.

### FMP congressional — `FmpCongressAdapter`
`ingestion/vendors/fmp.py:161` · key `fmp_congress` · run twice, senate and house, results
concatenated (`registry.py:91`)

`GET /stable/senate-latest` and `/stable/house-latest`

Fields: `transactionDate`, `disclosureDate`, `type`, `firstName`, `lastName`,
`representative`, `office`, `link`/`url`, `assetDescription`, `symbol`, `amount`.

STOCK Act amounts are ranges. `range_lower_bound()` (`fmp.py:63`) takes the **lower
bound, never the midpoint**, and every event is flagged `usd_value_is_estimate=True`.
Almost none clear the $5M gate — the adapter's own comment calls that "the correct
outcome, not a bug."

### Quiver congressional — `QuiverCongressAdapter`
`ingestion/vendors/quiver.py:82` · key `quiver_congress` · **paid** (~$30-75/mo) · needs
`QUIVER_QUANT_API_KEY` and `WHALE_ENABLE_QUIVER`

`GET https://api.quiverquant.com/beta/live/congresstrading`, header
`Authorization: Token {key}`.

Fields: `TransactionDate`, `ReportDate`, `Transaction`, `Amount`, `Range`,
`House`/`Chamber`, `Representative`/`Senator`, `Company`, `Ticker`.

Same lower-bound rule as FMP, using the same shared helper.

### Taiwan MOPS / TWSE — `TaiwanMopsAdapter`
`ingestion/taiwan_mops.py:108` · key `taiwan_mops` · **free, keyless, on by default**

`GET https://openapi.twse.com.tw/v1/opendata/{dataset}`, where the registry default is
`t187ap08_L` (insider holdings). Also available: `t187ap03_L` (director holdings),
`t187ap04_L` (material announcements).

`TAIWAN_MOPS_USER_AGENT` must be a real contact string.

Fields are tried in order against both Chinese and English variants (`taiwan_mops.py:53`):

| Meaning | Keys tried |
|---|---|
| company code | `公司代號`, `CompanyCode`, `Code`, `公司代号` |
| company name | `公司名稱`, `CompanyName`, `Name`, `公司简称` |
| person | `姓名`, `Name`, `PersonName`, `股東名稱` |
| title | `職稱`, `Title`, `JobTitle` |
| shares | `目前持有股數`, `現持股數`, `持股數`, `Shares`, `CurrentShares`, `選任時持股數` |
| percent | `持股比率`, `持股比例`, `HoldingPercentage`, `Percentage` |
| date | `出表日期`, `資料日期`, `Date`, `ReportDate`, `公告日期` |

Emitted as `PASSIVE_13G` because these are *holding balances*, not transactions. Amounts
are share counts in TWD terms, so an event only clears $5M once the FMP price provider
resolves the `.TW` symbol. Taiwan without FMP is close to invisible.

A renamed field degrades to fewer Taiwan rows, not a crash.

### Japan EDINET — `JapanEdinetAdapter`
`ingestion/japan_edinet.py:102` · key `japan_edinet` · free but **requires a registered
key** (`JAPAN_EDINET_API_KEY`)

Two calls: `GET {base}/documents.json?date=…&type=2&Subscription-Key=…` to list, then
`GET {base}/documents/{docID}?type=5&Subscription-Key=…` for the body. Base
`https://api.edinet-fsa.go.jp/api/v2`. The key travels as a **query parameter**.

**The body is a ZIP, not text.** `Content-Type: application/octet-stream`, magic
`PK\x03\x04`, containing one `XBRL_TO_CSV/*.csv` that is tab separated and **UTF-16 with
a BOM**. `decode_edinet_document` unpacks it. Reading the response as text was why this
source produced zero rows for its whole life despite returning HTTP 200.

Doc type `350` covers **both** `大量保有報告書` (initial) and `変更報告書` (change report);
only `docDescription` separates them. `360` is `訂正報告書`, a correction, and is what
sets `is_amendment=True`.

`documents.json` **does not identify the issuer** on these filings: `secCode`,
`issuerName` and `subjectEdinetCode` were null on all 34 rows returned for 2026-07-24.
Issuer name, securities code, share count and ratio all come from the CSV. Fields read
from the JSON are only `docTypeCode`, `docID`, `docDescription`, `submitDateTime`,
`filerName` and `edinetCode`.

CSV rows are matched by **exact** XBRL local element name, with the Japanese label as a
fallback (`japan_edinet.py:79`):

| field | element | label |
| --- | --- | --- |
| ratio | `HoldingRatioOfShareCertificatesEtc` | `株券等保有割合` |
| prior ratio | `HoldingRatioOfShareCertificatesEtcPerLastReport` | `直前の報告書に記載された株券等保有割合` |
| shares | `TotalNumberOfStocksEtcHeld` | `保有株券等の数` |
| shares outstanding | `TotalNumberOfOutstandingStocksEtc` | `発行済株式等総数` |
| issuer name | `NameOfIssuer` | `発行者の名称` |
| issuer code | `SecurityCodeOfIssuer` | `発行者の証券コード` |
| obligation date | `DateWhenFilingRequirementAroseCoverPage`  | none |
| change reason | `ReasonForFilingChangeReportCoverPage`  | none |

Matching is exact because `HoldingRatioOfShareCertificatesEtc` is a *prefix* of the
prior-ratio element; fragment matching is how the previous holding used to be read as
the current one.

**Ratios are XBRL `pure` fractions.** `0.1052` means 10.52 percent. The parser multiplies
by 100 so `percent_of_company` means the same thing here as everywhere else.

**Per-holder vs group total.** Facts repeat once per joint holder in a
`...FilerLargeVolumeHolderNMember` context and, when there are two or more holders, once
more in the bare `FilingDateInstant` context holding the group total. The group total
wins; a single-holder filing has no aggregate row and its lone member context *is* the
total. Taking the first match returned one co-holder's slice (0.42% instead of 10.52% on
S100YS2X).

`transaction_date` is the obligation date, not the submission date, which can be five
business days later. `shares_outstanding` is taken from the filing rather than from a
price vendor, so the denominator of every percent-of-company valuation is the issuer's
own dated figure.

`fetch()` walks back `DEFAULT_LOOKBACK_DAYS` (7) days: EDINET publishes nothing on
weekends or Japanese holidays, so a one-day window is empty two days in seven.

Valuation works end to end: FMP resolves the `NNNN.T` symbol (verified live on `6951.T`,
`2499.T` and six others), `JPY` is in the FX table, and real filings clear the $5M gate
by wide margins. A filing whose ticker FMP cannot price keeps `usd_value=None` and
renders as "not disclosed".

Golden fixtures (`tests/fixtures/edinet_*`) are byte-for-byte captures of live
2026-07-24 responses, including the raw ZIP, so the tests pin the real vendor shape.

### Dataroma — `DataromaInsiderBuysAdapter`
`ingestion/dataroma.py:70` · key `dataroma` · free, keyless, **on by default** ·
**legally the weakest source here**

`GET https://www.dataroma.com/m/ins/ins.php` — scraped HTML, no official API, site owned
by Morningstar, and its terms of use may not permit this. The docstring's instruction is
explicit: personal ingestion only, never redistribute, a handful of requests per day.
`WHALE_ENABLE_DATAROMA=0` turns it off. If this product is ever shown to anyone outside
a private audience, turn it off.

Produces `OPEN_MARKET_BUY` only — the page lists purchases.

Cells are keyed by **normalized header text**, not column position: `stock`/`ticker`,
`date`, `price`, `value`/`amount`, `insider`/`reporter`, `title`, `shares`, plus `_href`
from the anchor. The column order has changed before; header-keying means a rename drops
one field instead of shifting every value one place left.

### Arkham — `ArkhamAdapter`
`ingestion/crypto_arkham.py:62` · key `arkham` · needs `ARKHAM_API_KEY` (free tier still
issues one), header `API-Key`

`GET https://api.arkm.com/transfers?usdGte=…&limit=…[&chains=…]`

Fields: `transfers` (or a bare array), `fromAddress`/`toAddress` each with
`arkhamEntity`/`entity` → `name`, `type`; `blockTimestamp`/`timestamp`;
`tokenSymbol`/`symbol`; `chain`; `historicalUSD`/`unitValueUSD`/`usd`; `txURL`/`url`;
`transactionHash`/`hash`.

Transfers unattributed on **both** sides are dropped (`crypto_arkham.py:92`). USD is
always `usd_value_is_estimate=True` — an on-chain movement is not a disclosed position and
by design scores below any filing.

### Whale Alert — `WhaleAlertAdapter`
`ingestion/crypto_whale_alert.py:37` · key `whale_alert` · needs `WHALE_ALERT_API_KEY`
(query param `api_key`)

`GET https://api.whale-alert.io/v1/transactions?api_key=…&min_value=…`

Fields: `transactions`, `from`/`to` → `owner`, `owner_type`; `timestamp`; `symbol`;
`blockchain`; `transaction_url`; `hash`; `amount_usd`. `WHALE_ALERT_MIN_USD` defaults to
5,000,000.

Both-sides-unlabelled transfers are dropped. Its own docstring calls it the
lowest-signal transaction type in the system, by design.

## Context sources (produce `ContextAnnotation`, **not currently wired to anything**)

These are company-level quarterly facts with no filer taking a position. They deliberately
do **not** go through `NormalizedEvent` or the $5M gate — see `models/annotation.py`.
Their `normalize()` raises `NotImplementedError` on purpose; the entry points are
`.annotations()` and friends. **No job calls them.**

### Senate LDA lobbying — `SenateLdaAdapter`
`ingestion/gov/lobbying.py:115` · keyless, public domain

`GET https://lda.senate.gov/api/v1/filings/`, paginated by the DRF `next` link, with a
`SENATE_LDA_PAGE_DELAY` (default 1s) sleep between pages. **The host must stay
`lda.senate.gov` — `lda.gov` returns 403 to programmatic clients.** The file says "do not
modernise the base URL"; believe it.

Fields: `filing_year`, `filing_period`, `client.name`, `registrant.name`, `income`,
`expenses`, `expenses_method`, `filing_type_display`, `dt_posted`,
`filing_document_url`/`url`.

`income` and `expenses` are **alternative reporting methods, never both** — outside firms
report income, companies lobbying for themselves report expenses. Coalescing null to zero
would report Boeing as spending nothing. Registrations carry neither, so `amount_usd`
stays `None` rather than a fabricated 0.

This is the primary source that Quiver's paid lobbying product is derived from.

### USASpending — `UsaSpendingAdapter`
`ingestion/gov/usaspending.py:111` · keyless, no quota

`POST https://api.usaspending.gov/api/v2/search/spending_by_award/`

Fields requested and echoed: `Award ID`, `Recipient Name`, `Award Amount`,
`Awarding Agency`, `Awarding Sub Agency`, `Start Date`, `End Date`, `Description`,
`recipient_id`, plus `generated_internal_id` for the permalink.

Matched by `recipient_search_text` against a **hand-maintained ticker→SAM legal-name map
of 22 entries** (`usaspending.py:60`), not by ticker. Coverage is exactly those 22 names.

Uses `date_type: "new_awards_only"`. The default filter is by transaction date and
returned a 1993 DOE contract for a 2026 window.

### Quiver lobbying / gov contracts
`vendors/quiver.py:127,174` · `/beta/live/lobbying`, `/beta/historical/lobbying/{ticker}`,
`/beta/live/govcontractsall` · same Quiver auth. Paid restatements of the two free sources
above.

### FMP institutional (13F)
`vendors/fmp.py:212,282` · not registered. The endpoint shape is per-manager or
per-symbol; there is no "all filers today" call, so it needs a watchlist before it can be
a feed. Referenced only as a live-schema probe in `verify_sources.py:152`.

## HTTP behaviour common to all adapters

`ingestion/_http.py`. Retries only on `{408, 425, 429, 500, 502, 503, 504}` with
exponential backoff (`WHALE_HTTP_BACKOFF * 2**attempt`, `WHALE_HTTP_MAX_RETRIES`
attempts). A `401` or `404` fails immediately — retrying a bad key only burns quota.

`polite_headers()` (`_http.py:115`) attaches the configured contact string. SEC and TWSE
require it; several sources block generic library user-agents.

The module forces `httpx` and `httpcore` loggers to WARNING at import. FMP and Whale Alert
authenticate by query parameter, and httpx logs full request URLs at INFO — one verbose
run would print live API keys into a terminal that gets pasted somewhere.

## Environment variables

Full annotated list is in [`SETUP.md`](SETUP.md) and `.env.example`. The short version of
what gates what:

| Source | Required | Default state |
|---|---|---|
| SEC Form 4 / 13D-G | `WHALE_SEC_USER_AGENT` (functionally) | always on |
| Taiwan MOPS | `TAIWAN_MOPS_USER_AGENT` | on, keyless |
| Dataroma | — | on, keyless |
| Japan EDINET | `JAPAN_EDINET_API_KEY` | on if key |
| FMP (insider, congress, prices, market cap) | `FMP_API_KEY` | on if key |
| Quiver | `QUIVER_QUANT_API_KEY` | on if key |
| Arkham | `ARKHAM_API_KEY` | on if key |
| Whale Alert | `WHALE_ALERT_API_KEY` | on if key |
| Senate LDA / USASpending | — | flag on, **but unwired** |
| Treasury / FRED | `FRED_API_KEY` | flag on, **no adapter exists** |

Every source also has a `WHALE_ENABLE_*` flag that forces it off regardless of key.

`./whale digest --show-config` prints what is on and what would switch on the rest.
