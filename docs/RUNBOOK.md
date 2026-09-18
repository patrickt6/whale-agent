# Runbook

How to operate it day to day. For first-time configuration see [`SETUP.md`](SETUP.md).

**Run everything through `./whale`.** Plain `python -m whale_agent…` does not import on
this machine, and the launcher also `cd`s to the repo root so `whale.db` and `.env`
resolve identically wherever you typed the command.

## Commands

```bash
./whale digest --show-config   # what is switched on, and what would switch on the rest
./whale digest --demo          # offline fixtures, no network, no email, no database write
./whale digest --dry-run       # real data, rendered to stdout, nothing sent
./whale digest                 # the real thing
./whale weekly --dry-run
./whale watchdog --check       # report staleness, do not alert
./whale test                   # 377 tests, under a second
./whale python -m whale_agent.jobs.backfill --days 28 --dry-run
./whale python -m whale_agent.jobs.verify_sources
```

### `digest` — `jobs/digest_daily.py`

| Flag | Default | Notes |
|---|---|---|
| `--limit N` | 50 | max filings per SEC/vendor pull |
| `--demo` | off | offline fixture demo. Never touches the real database — it would pollute first-time-filer state |
| `--dry-run` | off | render but do not send |
| `--test-email` | off | send even in `--demo`, to verify mail delivery only |
| `--no-llm` | off | skip the prose layer, use the deterministic render |
| `--sources a,b` | all | comma-separated source names; see `--show-config` for the names |
| `--show-config` | — | print config and exit |
| `--date YYYY-MM-DD` | today | |
| `--verbose` / `-v` | off | |

### `weekly` — `jobs/digest_weekly.py`

`--dry-run`, `--date YYYY-MM-DD` (report end date), `--articles-dir DIR` (default
`articles`), `--article-base-url URL` — the public URL the article directory is served
from, defaulting to `Settings.article_base_url` (the live Cloudflare site) when unset.

**Manual/local sends: run `--publish`, not a bare send.** Rendering and sending are one
step in this job, but *publishing* the rendered pages to Cloudflare is a separate thing
that has to happen first — the render step only writes pages under `--articles-dir`. A
bare `./whale weekly --articles-dir deploy/public` sends immediately after rendering and
leaves publishing as a step a human has to remember to do afterward. The reader has
received a weekly with dead "our report" links twice this way: the pages existed minutes
later, but not when the email went out.

The safe single command:

```bash
./whale weekly --articles-dir deploy/public --publish
```

`--publish` deploys `deploy/public` to Cloudflare (`cd deploy && npx wrangler@latest
deploy`, off the saved wrangler OAuth login on this machine — no token needed) *before*
sending, mirroring the two ordered steps the CI workflow already runs ("Deploy the pages
to Cloudflare" then "Send the weekly"). It requires `--articles-dir` to point at
Cloudflare's asset directory (`deploy/public`, the default) — anywhere else and it
refuses rather than deploying the wrong thing.

Independent of `--publish`, the send itself refuses to go out if the report links it is
about to carry do not actually resolve yet — it fetches each one (and, where a link
targets an anchor, confirms the anchor is present in the fetched page) and blocks with a
`link-check` failure explaining which link is dead rather than delivering it. This is
what makes `--publish` a convenience rather than the only thing standing between a
human and dead links: forgetting it, or running the deploy by hand and mistiming it,
gets caught here instead of by a reader. See `monitoring/link_check.py`.

`--dry-run` never publishes and never sends, regardless of `--publish`.

### `backfill` — `jobs/backfill.py`

`--days N` (28), `--max-pages N` (400), `--dry-run`, `--revalue`, `--verbose`.

Never sends anything, by design. Safe to re-run — events key on a stable hash, so a run
that dies halfway can just be repeated.

`--revalue` re-runs valuation over rows already in the store. Use it when the store is
full of events with share counts and no dollar figure.

### `watchdog` — `jobs/watchdog.py`

`--check` reports and does not alert. Without it, problems are emailed to
`OPERATOR_ALERT_EMAIL` (falling back to `WHALE_EMAIL_TO`) via `monitoring/alerting.py`.

Two checks (`monitoring/health.py`): `stale_sources` flags a source whose last success is
older than its max age, and `digest_overdue` fires if no digest has gone out by
`DIGEST_DEADLINE_HOUR_UTC` (default 13). The second is the dead man's switch — it is what
tells you the cron itself died, which no amount of in-process error handling can.

### `verify_sources` — `jobs/verify_sources.py`

`--fmp`, `--quiver`, `--links`, `--ticker SYM` (default AAPL). Exits non-zero on failure,
so it is cron- and CI-safe.

It probes live endpoints for the field names the parsers expect — the one thing
golden-file tests structurally cannot catch. It checks FMP price/profile/insider/senate/
house/institutional and Quiver congressional, and `--links` samples filing URLs and
confirms they resolve.

**Its coverage is FMP and Quiver only, and only when their keys are set.** SEC, Taiwan,
Japan, Dataroma, Arkham, Whale Alert and the gov sources are probed by nothing. Schema
drift there shows up as a quiet source, not an error.

## Daily operation

1. `./whale digest` on a cron before `DIGEST_DEADLINE_HOUR_UTC`.
2. `./whale watchdog` after the deadline hour.
3. `./whale weekly` once a week. The weekly cadence is a product decision, not a
   scheduling convenience: one day of filings yielded 0 patterns, sixteen days yielded 2.
   Running it daily produces nothing and trains you to ignore it.
4. `./whale python -m whale_agent.jobs.verify_sources` weekly, before the weekly report.

There is currently **no way to run instant alerts** — `jobs/instant_alerts.py` has no
entry point.

## Reading the output

**Coverage notes.** A source that is off or broken appears as a note on the digest rather
than an exception. If a jurisdiction is silent, check the note before checking the code.

**Rejected-event count.** `enrichment/plausibility.py` marks and excludes implausible
events rather than deleting them. That count is the early-warning signal for a vendor
schema change — a jump means a field moved, not that filers went mad. Treat it as a
metric, not noise.

**`usd_value_is_estimate`.** Any congressional trade, any crypto transfer, and any equity
event whose price we looked up rather than read off the filing. Estimates are still
gated at $5M, but they are weaker evidence and the digest says so.

**Deterministic prose.** If the digest reads wooden, the LLM layer fell back. That is the
designed behaviour on any failure — no key, HTTP error, malformed JSON, or a figure the
provenance gate refused. Check the logs for which. The digest still went out; that is the
point.

## When something is wrong

| Symptom | Look at |
|---|---|
| Nothing sends | `.env` delivery lines. `./whale digest --show-config`. `delivery/email.py` never raises — it returns `DeliveryResult(ok=False)`, so check the log, not for a traceback |
| Confusing SMTP rejection | The App Password pasted into the wrong env var. Caught explicitly at `jobs/digest_daily.py:46` |
| A jurisdiction is empty | Coverage note first. Then: Taiwan needs FMP prices to clear the gate at all; Japan needs `JAPAN_EDINET_API_KEY`; SEC events need `enrichment/tickers.py` to have resolved CIK→ticker |
| A figure is absurd | `plausibility.py` should have caught it. If it did not, the market-cap enrichment probably failed, which is the decisive check |
| Store is full but nothing clears $5M | Unvalued backfill rows. `--revalue` |
| One source erroring | `WHALE_SOURCES=that_source ./whale digest --dry-run` to isolate |
| Suspected vendor schema change | `verify_sources`, then the field tables in [`DATA_SOURCES.md`](DATA_SOURCES.md) |
| 401 from a vendor | The key, not the network. `_http.py` does not retry 401 on purpose |

## Standing hazards

- **Rotate the FMP key.** Rotate it on any suspected exposure.
- **~90 files are uncommitted.** One `git clean` ends the project. This is the cheapest
  high-value action available and needs no design judgement.
- **Never run with `httpx` logging at INFO.** FMP and Whale Alert authenticate by query
  string; `_http.py` pins those loggers to WARNING at import, and unpinning them prints
  live keys.
- **Dataroma is scraped, not licensed.** Personal ingestion only. If output is ever shown
  to anyone beyond a private audience, set `WHALE_ENABLE_DATAROMA=0` first.
- **Do not "modernise" `lda.senate.gov` to `lda.gov`.** It 403s programmatic clients.

### The corpus release

`whale.db` lives as an asset on the private release tagged `corpus`, because it is 40MB
and gitignored. `scripts/corpus.sh {init|download|upload}` is the only thing that touches
it. `init` is idempotent and is run once, by hand, after the repo first has a remote.

`download` deliberately has no fallback. If the asset cannot be fetched the run aborts
rather than starting from an empty database: an empty corpus yields a confident report
about nothing, which is worse than no report.

Upload is always the last step of a workflow, so a crashed run leaves the previous good
corpus in place rather than replacing it with a half-written one.
