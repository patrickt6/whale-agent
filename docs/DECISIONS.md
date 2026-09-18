# Decisions

## STE100 for all reader-facing prose

**Chosen:** every piece of prose a human reader sees -- email body, report-page prose,
headlines, research notes -- follows ASD-STE100 Simplified Technical English, the
aerospace controlled-language standard. One idea per sentence, sentences under about 25
words, simple present/past tense, active voice, no vague qualifiers ("quite", "fairly",
"very", "a lot of", "somewhat", "rather"), no noun-cluster stacking. Internal logs and
docs (this file included) are not in scope; this is a rule about what the reader reads.

**Enforced by:** `tests/test_ste100_house_style.py`, the same mechanism as the existing
em-dash/curly-quote check (`test_no_em_dash_en_dash_or_curly_quotes_anywhere_on_the_page`
in `test_tracked_filings_prose.py`) -- render the full report page and scan the text
inside every `<p>` block. It catches three mechanically-detectable violation classes:
sentences over 25 words, "was/were/is/are ... by" passive constructions, and the six
banned qualifiers above.

**Rejected:** a full grammar-based STE100 compliance engine. Real STE100 also forbids
non-simple tenses, "-ing" words used as nouns or adjectives, and noun-cluster stacking,
none of which a regex can catch without a parser. This is a conservative, high-confidence
mechanical check, not full compliance -- a human editor still has to catch the rest.

**Known gap:** the check is wired to the tracked-filings report page only (the same
fixture the em-dash test builds). `_quarterly_filing_prose` and `_overview_row_prose` /
`_overview_section_prose` in `publishing/tracked_filings_html.py`, and the weekly email
body in `summarization/render_weekly_html.py`, are not yet exercised by this test and
have not been audited against STE100. Extending the fixture to cover them is the natural
next pass.

Why things are the way they are, and what was rejected. [`ARCHITECTURE.md`](ARCHITECTURE.md)
covers the pipeline invariants; this page
is the record of choices where an alternative was seriously considered, so that nobody
re-litigates them from scratch or, worse, silently reverses one.

The primary source for the code decisions is the module docstrings, which are unusually load-bearing
in this repo — several of them are the only record of a decision.

## Reversed: fame is not a ranking axis

**Originally chosen:** a notability multiplier of 1.0-2.0× (PRD §3).

**Now:** removed. Reputation survives as a 0.45-1.25 term in which **passive managers score
0.9 with a further 0.5 discount on passive forms.**

**Why:** the first version "produced a digest of names Patrick could have listed himself,
and — worse — promoted Vanguard and BlackRock" (`scoring/score.py:1-32`). An index fund
crossing 5% is a mechanical consequence of inflows, not a view. The reader's axis is
**obviousness**, not size or fame: he already reads CNBC, Bloomberg and the FT, so a
famous name is evidence the item is *not* worth sending.

**Not deleted, demoted.** Index-fund filings still land in the store at 0.9 so a future
reader profile can restore them. This is the clearest reversed decision in the project;
both the original spec and the rejection rationale are on record.

## The model never sees a number it could compute with

**Chosen:** the LLM receives pre-formatted strings (`"$12.4M"`), never raw values, never
scores, never the ranking (`summarization/prompts.py:1-12`). Its system prompt says "use
ONLY the numbers present in the JSON" and "if null, write 'not disclosed', never
estimate."

**Rejected:** handing the model the event rows and letting it summarize. A model given
3,000 rows "will produce a story either way" (`analysis/patterns.py:1-36`).

**Why:** the product's entire claim is that figures trace to filings. A model that can do
arithmetic can produce a figure no filing contains, and there is no way to tell those
apart after the fact except by the provenance gate — which is a backstop, not a design.

## The deterministic prose is the normal case, not the degraded one

**Chosen:** every LLM failure — no key, HTTP error, malformed JSON, a hallucinated figure
caught by the gate — falls back to the template render. `summarization/llm.py:295`: "Never
raises… prose is strictly an upgrade… must not cost delivery."

**Why:** `summarization/thesis.py:1-26` is explicit that the fallback thesis is "the only
honest arrangement when the product's claim is that the figures, not the sentences, are
the thing being sold."

**The known cost:** the deterministic prose is wooden, and the fallback claim sentence
("concentrated enough that it is worth checking whether it repeats") says almost nothing.
That is the thing a model genuinely improves, and it is the outstanding quality gap.

## Gemini is the default LLM, not Anthropic

**Chosen:** Gemini, "because its free tier makes the digest cost nothing to run"
(`summarization/llm.py:1-12`). Anthropic is a second implementation of the same
provider-agnostic `LLMProvider` protocol.

**Note:** `ANTHROPIC_MODEL` is pinned in `.env.example` with a comment warning never to
default to a "latest" alias. A silently-upgraded model is an unannounced change to the
product's voice.

## A thesis must state what would refute it

**Chosen:** `models/thesis.py` is a structured schema, not free prose. `is_publishable()`
requires a falsifier and a citation. `Citation.detail` must let a reader re-derive the
figure independently — accession number, endpoint plus params, series ID. "A citation
without url or detail is not a citation."

The `Pattern.falsifier` is copied verbatim into the thesis and **the model may only append
to it, never rewrite it**, "because a layer arguing a case must not also hold the pen on
what would refute it" (`summarization/thesis.py`).

**Rejected:** free-form analyst prose. "A research note that states a claim without stating
what would disprove it is a press release."

A thesis that fails `is_publishable()` returns `None`. A quiet week publishes nothing —
that is a feature.

## Context facts are not events

**Chosen:** `ContextAnnotation` (`models/annotation.py`) as a separate type from
`NormalizedEvent`.

**Rejected:** routing lobbying spend and federal contract awards through the event model.

**Why:** they are quarterly, company-level, and have no filer taking a position. Through
the event model they would either flood the daily digest or be dropped by the $5M gate
entirely. They are never Tier-1 alerts.

## Congressional ranges: lower bound, never midpoint

**Chosen:** `range_lower_bound()` (`vendors/fmp.py:63`), shared by the Quiver adapter,
with `usd_value_is_estimate=True`.

**Rejected:** the midpoint, which is the conventional treatment.

**Why:** "a midpoint would be a number nobody disclosed, and the provenance gate exists
precisely to stop that kind of invention." The consequence — almost no congressional trade
clears $5M — is accepted as correct.

## Hand-written SVG charts, not a plotting library

**Chosen:** inline SVG generated in `publishing/charts.py`.

**Rejected:** matplotlib.

**Why, two reasons** (`publishing/charts.py:1-23`): adding a plotting stack and its numeric
dependencies "to draw a dozen rectangles is a poor trade for a project whose deployment
story is a cron job"; and inline SVG is markup rather than an image asset, so it survives
email clients that block remote images.

**Also rejected:** stock photography and AI-generated header art. For a product whose
claim is that every figure traces to a source, decoration "quietly says the opposite." The
header image must be evidence — a chart of the article's own data.

## Cloudflare Access, not Substack

**Chosen:** static pages behind Cloudflare Access with a single-email allow policy.

**Rejected:** Substack — no public write API, and no genuine single-reader privacy mode.

**Why:** the publication must be private to a private audience. An unguessable URL is not a
privacy model; Access is a real identity check, and it is free.

Separately, and not the same decision: Substack *as an ingestion source* is usable via
per-publication RSS (PRD line 112).

## Instagram is not a dependency

**Rejected as a source** despite being a common discovery channel for readers and the origin of
the whole project: "no usable public API… can only be monitored manually or via brittle,
ToS-risky scraping. Do not build a hard dependency on it" (PRD line 113).

## Vendor selection

| Slot | Chosen | Rejected / deferred | Why |
|---|---|---|---|
| SEC access | `edgartools` | `sec-api.io` ($49/mo personal; enterprise tier needed to redistribute) | Free, open-source, no key, widely used. The cheap-to-operate default. |
| Global insider data | 2iQ Research, **deferred** | Smart Insider (FactSet-only, no public price) | 60,000+ stocks across 50 countries, but not bought until the free tiers prove the workflow. Fallback if the quote is unpalatable: free-feed stitch plus Fintel. |
| Congressional | Quiver + FMP | WhaleWisdom, TipRanks, GuruFocus | Unusual Whales was named a top-3 purchase specifically for being AI-native (OpenAPI + MCP + `skill.md`) but is not implemented. |
| Superinvestor buys | Dataroma | — | Cheapest possible coverage, on the weakest legal footing of any source. |
| Lobbying / contracts | `lda.senate.gov`, `api.usaspending.gov` | Quiver's paid equivalents | These are the primary sources Quiver's product is derived from, and they are free and keyless. USASpending is "the closest thing the free tier has to a causal link" — it can support "the CFO bought in March, the Army awarded $4.8bn in April." |
| Scheduler | cron + idempotent jobs | Temporal, Inngest | Overkill for a single-user agent. Idempotency is what makes cron safe, and it is already enforced at the storage layer. |
| Database | SQLite, Postgres planned | DuckDB | DuckDB is good for backfill analytics, wrong for transactional writes. **The Postgres migration was specified for Phase 1 and has not happened** — `DATABASE_URL` exists in config and nothing reads it. |

## Storage shape

`seen_filers` is its own narrow table rather than being derived by JSON-parsing the full
event table on each digest run (`storage/db.py:1-35`). Median and frequency lookups run
per event; deriving them each time does not scale past a small store. Amendment history is
retained via an `amended=1` flag rather than deletion.

## Delivery

- `delivery/email.py` **never raises.** An unconfigured or failing mailbox returns
  `DeliveryResult(ok=False)` — "losing the email is recoverable; losing the digest is not."
- Resend vs SMTP: Resend for deliverability and its free tier, at the cost of domain
  verification; SMTP retained as the zero-signup path via a Gmail App Password.
- `delivery/base.py` hand-rolls the digest→HTML conversion rather than adding a markdown or
  templating dependency, because the digest's line structure is fixed and known. Plain text
  is always sent as a multipart alternative.
- SMS is capped at `SMS_MAX_CHARS = 320`, two segments: "instant alerts should be a glance,
  not a read."

## The academic basis, and what each citation actually licenses

The evidence block in the weekly report is not decoration; these papers are what the
scoring model is allowed to assume.

| Work | Finding | What it justifies here |
|---|---|---|
| Lakonishok & Lee 2001, *RFS* 14(1) | Insider **buys** informative, sells not; effect concentrated in small firms | Sells scored well below buys; the company-size term exists at all |
| Seyhun 1986 | Purchases more informative than sales (litigation risk deters honest selling) | Same direction, independent confirmation |
| Cohen, Malloy & Pomorski 2012, *JF* 67(3) | **Opportunistic** trades earn ~82bps/month value-weighted; routine trades ~0 | Both the 0.85-1.5 company-size term **and** `enrichment/routine_classifier.py` |
| Chiang et al. 2004 | **No** reliable abnormal returns for Taiwan Stock Exchange insider trades | Taiwan is covered because it is the reader's market of interest, explicitly *not* because the evidence supports trading it |
| Eckbo & Smith (Oslo) | Same null-result category | Reinforces the above |
| Schroeder 2024 (SSRN) | 13F cloning, 24.3% annualized — caveated by 45-135 day lag, long-only bias, alpha decay | Why 13F staleness is stated in every weekly report |

**The live inconsistency:** the report claims the signal lives only in opportunistic
trades, and `routine_classifier.py` implements exactly that test — but nothing calls it
(see [`STATUS.md`](STATUS.md)). Wiring it is the highest-value scoring change available.

## Two apparent contradictions, reconciled

**"The $5M threshold does not move" vs the $4.5-5M buffer band.** The buffer (PRD §2) is a
*display* flag marking near-misses so FX and price-estimation noise do not silently drop
borderline items. It does not change the gate boundary. Both statements are true.

**Per-filing $5M gate vs cluster gating.** Clusters are gated on count and aggregate
(`MIN_PATTERN_AGGREGATE_USD`), not per filing, because per-filing gating hid real
clusters: four $2M buys into one issuer are stronger evidence than one $6M buy, and the
per-filing gate made all four invisible. See [`ARCHITECTURE.md`](ARCHITECTURE.md).
