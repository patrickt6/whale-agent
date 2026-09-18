# Architecture

How the pipeline fits together, and the invariants that must survive any change.

## The pipeline

```
registry          which sources run today, and why the others did not
   ↓
ingestion         Adapter ABC: fetch → parse → normalize      → NormalizedEvent
   ↓
valuation         native amount / shares×price / %×cap        → usd_value
   ↓
enrichment        market context, filer history, ticker        → EventContext
   ↓
plausibility      reject figures that are traceable but false
   ↓
scoring           composite score, $5M gate, tiering
   ↓
patterns          deterministic detection over the window      → Pattern
   ↓
thesis            model writes language about a Pattern        → Thesis
   ↓
render            markdown / HTML email / article page
   ↓
provenance gate   every figure must trace, or prose is dropped
   ↓
delivery          SMTP or Resend; Telegram/SMS for Tier-1
```

`build_ranked()` and `run_digest()` in `jobs/pipeline.py` are pure — events in, output
out — and testable with no network. `run_daily_digest()` is the orchestration on top.

## The funnel, and why the model is not overwhelmed

This is the answer to "won't all these APIs overwhelm the agent". Measured on live data:

```
249   events collected from all sources
  9   clear the $5M gate                    ← code
~15   ranked and sent to the model          ← code
0-3   patterns detected per week            ← code
      ↓
MODEL SEES ~3,000 tokens: pre-formatted rows + patterns
```

The model never sees the corpus, never sees raw JSON, never sees a score, and never sees
a number it could do arithmetic on — only pre-formatted strings like `"$12.4M"` that it
is instructed to copy verbatim. **Adding data sources widens the top of the funnel and
changes nothing about what reaches the model.**

## Invariants

### 1. Code decides what matters; the model decides how to say it

Ranking, gating and pattern detection are deterministic and testable. The model's only
job is language. This is what makes the output defensible: a reader who disagrees with
the ordering can be shown the terms that produced it.

### 2. No invented figures — `summarization/provenance.py`

Every material numeric token in rendered output must be derivable from the events.
`find_unsourced_numbers(text, events)` returns anything that is not.

- On failure, **prose is dropped and the deterministic render ships**. Losing the
  language is always better than shipping a number that traces to nothing.
- For a thesis, run the gate against **`pattern.events`, not the whole window** — a
  subset sum is not in the full window's allowed token set.
- Run it against `html_to_text(page)`, not raw markup: hex colours tokenize as
  magnitudes (`#0C6B3F` → `6B`).

### 3. Traceable ≠ true — `enrichment/plausibility.py`

A separate, independent check. The provenance gate proves a number came from a field; it
cannot prove the field held what the vendor claimed. FMP's price field sometimes carries
a transaction total, which produced a **$1.6 quadrillion** position that passed every
provenance check. The strongest test is the cheapest: a position cannot exceed the
company's market cap.

Rejected events are **marked and excluded, not deleted** — a silent drop is
indistinguishable from a source outage, and the rejection count is itself a signal.

### 4. Thresholds belong on the right object

The $5M gate filters **per filing**. A cluster is a **count** phenomenon. Gating first
left zero clusters in sixteen days, because four insiders buying $2M each is invisible to
a per-filing dollar filter — while being stronger evidence than one buying $6M.

So: **pattern detection runs on ungated events, and the group must clear $5M in
aggregate** (`MIN_PATTERN_AGGREGATE_USD`). This keeps real clusters and rejects "31 filers
bought TSM totalling $423K".

### 5. Graceful degradation everywhere

- A source that is unconfigured or broken becomes a **coverage note**, never an exception.
  An empty section reads as "this source is off", never as "nothing happened".
- A failing LLM costs the prose, never the digest.
- A failing mailbox returns a `DeliveryResult`, never raises.

### 6. `None` means "not learned", never zero

Every `EventContext` field is optional. Scoring terms return exactly `1.0` when their
input is `None`, so an unenriched Taiwan filing is neutral rather than penalised. A zero
would read as "we checked and it was nothing", which is a different and false claim.

## Key contracts

| Type | File | Role |
|---|---|---|
| `NormalizedEvent` | `models/event.py` | One disclosed move. Every adapter produces these. |
| `EventContext` | `models/context.py` | Market, filer and cohort context. All optional. |
| `ContextAnnotation` | `models/annotation.py` | Company-level facts (lobbying, contracts) that are **not** events and must not hit the $5M gate. |
| `Pattern` | `analysis/patterns.py` | A countably-present grouping, with its own falsifier. |
| `Thesis` | `models/thesis.py` | One article. `is_publishable()` requires a falsifier and a citation. |

## Ordering that matters

In `build_ranked`, the sequence is load-bearing:

1. **Market context before scoring** — size and float are scoring inputs.
2. **Filer history before persisting** — otherwise each event lands in its own history and
   a debut stops looking like one.
3. **Persist before scoring** — a crash in scoring still leaves the raw record.
4. **Plausibility after enrichment** — the market-cap check needs the cap.

## Scoring model

Multiplicative terms, each neutral (1.0) when its input is unknown:

| Term | Range | Rationale |
|---|---|---|
| base type weight | 0.1–1.0 | a buy is not a grant; a 13D is not a 13G |
| dollar conviction (log10) | 0.2–2.8 | bounds $200M vs $5M to ~2.3x, not 40x |
| company size | 0.85–1.5 | signal concentrates in small caps (Cohen-Malloy-Pomorski 2012) |
| percent of float | 1.0–2.0 | the honest denominator for conviction |
| unfamiliarity | 0.85–1.35 | measured from our own store, not a hardcoded list |
| unusual-for-filer | 0.95–1.6 | gap since last filing × size vs their own median |
| new position | 0.95–1.15 | opening states a view; adding repeats one |
| reputation | 0.45–1.25 | activists up slightly; **passive managers 0.9 with an extra 0.5 discount on passive forms** |

`passes_gate` and `assign_tier` are product invariants — the $5M threshold does not move.
