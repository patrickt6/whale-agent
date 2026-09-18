# whale-agent documentation

Source of truth for what this is, what it does, and what it does not do yet. Written so a new
contributor does not have to reconstruct any of it from the code.

**Read in this order.** Each file assumes the ones above it.

| File | What it answers |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How it works, and the invariants that must not be broken. |
| [`DATA_SOURCES.md`](DATA_SOURCES.md) | Every source, its auth, and its verified field names. |
| [`DECISIONS.md`](DECISIONS.md) | Why things are the way they are, and what was rejected. |
| [`RUNBOOK.md`](RUNBOOK.md) | How to operate it day to day. |
| [`SETUP.md`](SETUP.md) | First-time configuration (predates these docs; still accurate). |

## The one-paragraph version

A daily and weekly digest of large disclosed investment positions, assembled from
mandatory public filings. Code decides what is worth reporting; a language model only
writes sentences about decisions already made. Every figure traces to a filing field or
to arithmetic over filing fields, and any sentence containing a figure that does not
trace is deleted before sending. The product's value is not comprehensiveness — the
reader already has Bloomberg — it is surfacing the disclosures that the wires do not
cover.

## The five rules that must survive any refactor

1. **Code decides what matters. The model only decides how to say it.** Ranking,
   thresholds, and pattern detection are deterministic. The model never sees a score and
   never sees a number it could do arithmetic on.
2. **No invented figures.** `summarization/provenance.py` runs over rendered output; an
   unsourced number means the prose is dropped, not that the digest is.
3. **Traceable is not the same as true.** `enrichment/plausibility.py` exists because a
   vendor's bad field produced a $1.6 quadrillion position that traced perfectly.
4. **A failing source never fails the run.** Unconfigured or broken sources become a
   coverage note on the digest, never an exception.
5. **`None` means "not learned", never zero.** Every enrichment field is optional, and
   scoring treats unknown as neutral rather than as a penalty.

## Fastest orientation

```bash
./whale digest --show-config      # what is on, and what would switch on the rest
./whale test                      # 377 tests, under a second
./whale python -m whale_agent.jobs.verify_sources   # are the live endpoints still real
```

Run everything through `./whale`. Plain `python -m whale_agent…` does not import.
