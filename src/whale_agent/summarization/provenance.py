"""Numeric-provenance validation gate.

The product's trust model: no hallucinated numbers. Every numeric token appearing in
rendered digest text must be derivable from the structured event data. In Phase 0 the
render is fully deterministic so this always passes, but the harness is written now so
it is ready to guard the LLM prose layer the moment it lands.

Naming convention for this module and its siblings in `summarization/` and
`enrichment/plausibility.py`: a `check_*` function returns findings and never raises or
blocks; an `assert_*` function raises and blocks the output. Reading either name alone
should tell you which one it is without opening the body.

What is actually load-bearing here:

* `assert_no_hallucinated_numbers` is the hard gate. It is called once per rendered
  artifact (text digest, HTML email) immediately before delivery, and a call site that
  reaches it and does not catch `ValueError` has, by construction, refused to send. This
  is the only function in the file that can stop a digest.
* `check_unsourced_numbers` (formerly `find_unsourced_numbers`, kept as an alias below
  for existing callers) is the check the gate is built on. It never raises; it returns
  the list of tokens it could not source, empty meaning clean. Everything else in the
  file -- `allowed_numeric_tokens`, the tokenizer, `_is_material` -- exists only to make
  that one list correct.

The layer-2 model verifier in `verifier.py` is deliberately not in this file and does not
gate anything: see that module's docstring for why a model reading the finished digest is
advisory-only and cannot substitute for the checks below.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from whale_agent.enrichment.history_context import HistoryContext, history_figures
from whale_agent.models.event import NormalizedEvent
from whale_agent.summarization.render import format_usd

# Numeric tokens: money, percentages, plain integers/decimals, K/M/B abbreviations.
#
# The trailing `(?![A-Za-z])` matters more than it looks. Without it, a scale suffix can
# be claimed from the first letter of an adjacent word: "01 Buffett" tokenized as "01 B"
# and was then read as an unsourced *one billion*, failing the gate on a perfectly
# correct digest. Requiring the suffix to end the word makes the token boundary real.
# The decimal part likewise requires digits after the point, so the rank in "2. Berkshire"
# is the number 2 rather than "2." trailing into the name.
_NUM_TOKEN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?[KMB%]?(?![A-Za-z])", re.IGNORECASE)

# Stripped before scanning: identifiers inside a link are not numeric claims.
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def _normalize_token(tok: str) -> str:
    return tok.replace(",", "").replace(" ", "").lower().strip().rstrip(".")


def _is_material(tok: str) -> bool:
    """Only magnitude-bearing tokens carry hallucination risk.

    A token is material if it has a currency/percent/scale marker ($, %, K, M, B) or
    is a plain number >= 1000. Ranks, filing-lag days, cluster sizes, and date
    fragments are small bare integers and are never scrutinized.
    """
    if any(m in tok for m in ("$", "%", "k", "m", "b")):
        return True
    digits = tok.rstrip("%").replace(".", "")
    if digits.isdigit():
        n = float(tok.rstrip("%"))
        if 1900 <= n <= 2100 and "." not in tok:
            return False  # a bare 4-digit year is a date fragment, not a magnitude
        return n >= 1000
    return False


def allowed_numeric_tokens(
    events: list[NormalizedEvent], history: Mapping[str, HistoryContext] | None = None
) -> set[str]:
    """Every numeric string legitimately derivable from the structured events.

    `history` adds the stored figures a per-row history line prints (an earlier
    filing's share count or value, a 13F row's shares or value, their dates).
    """
    allowed: set[str] = set()
    for c in history_figures(history):
        allowed.add(_normalize_token(c))
        for m in _NUM_TOKEN.findall(c):
            allowed.add(_normalize_token(m))
    for ev in events:
        candidates: list[str | None] = [
            format_usd(ev.effective_usd),
            format_usd(ev.usd_value),
            format_usd(ev.implied_usd_value),
            None if ev.percent_of_company is None else f"{ev.percent_of_company:.1f}%",
            None if ev.effective_usd is None else f"{ev.effective_usd:,.0f}",
            str((ev.disclosure_date - ev.transaction_date).days),
            str(ev.cluster_size),
            ev.disclosure_date.isoformat(),
            ev.transaction_date.isoformat(),
            # Numeric tickers are real: Taiwan lists 2330.TW, Japan 7203.T, Hong Kong
            # 0700.HK. Without this the HTML email fails the gate the moment it prints a
            # non-US symbol -- the symbol is structured data, not a magnitude.
            ev.ticker,
            # CIKs and CUSIPs are identifiers too, and long enough to look like money.
            ev.issuer_id,
            ev.filer_id,
            # Enrichment figures. These are fetched from a vendor rather than read off
            # the filing, so they are exactly as traceable as the filing's own fields --
            # and the digest now cites them as reasons ("2.1% of float", "small company
            # at $210.0M"), which means the gate has to recognise them or it rejects a
            # correct digest.
            None
            if ev.context.percent_of_float is None
            else f"{ev.context.percent_of_float:.1f}%",
            format_usd(ev.context.market_cap_usd),
            format_usd(ev.context.filer_typical_usd),
        ]
        for cand in candidates:
            if cand is None:
                continue
            # Store both the whole string and its numeric sub-tokens.
            allowed.add(_normalize_token(cand))
            for m in _NUM_TOKEN.findall(cand):
                allowed.add(_normalize_token(m))
    # Aggregates over the same events. A digest header that says "14 filings,
    # $2.1B total" is stating something computed in code from the rows below it, so it
    # is as traceable as any single row -- but it is not any one event's field, and
    # without this the summary bar fails the gate.
    values = [ev.effective_usd for ev in events if ev.effective_usd is not None]
    if values:
        total = sum(values)
        allowed.add(_normalize_token(format_usd(total)))
        allowed.add(_normalize_token(f"{total:,.0f}"))
        allowed.add(_normalize_token(format_usd(max(values))))
    allowed.add(str(len(events)))

    # Structural constants that legitimately appear (rank numbers, thresholds).
    allowed.update({"5", "5m", "$5m", _normalize_token("$5.0M")})
    return allowed


def check_unsourced_numbers(
    text: str,
    events: list[NormalizedEvent],
    history: Mapping[str, HistoryContext] | None = None,
) -> list[str]:
    """Return numeric tokens in `text` not traceable to the events. Empty == clean.

    Never raises: this is the `check_*` half of the gate, meant to be called freely
    (per-sentence during generation, again over the whole render, again over the HTML)
    without any of those calls being able to block anything on their own. Only
    `assert_no_hallucinated_numbers` turns a non-empty result into a refusal.

    Date fragments split by the tokenizer (year/month/day integers) are tolerated by
    also allowing the components of any ISO date already in the allowed set.

    URLs are removed before scanning. A source link is an opaque identifier, not a
    claim about magnitude: an SEC archive URL embeds the filer's CIK and the filing's
    accession number, and those digits are long enough to read as dollar amounts. The
    first live run against a vendor feed failed on exactly that
    (`000186332826000002`), which would have blocked every digest carrying a link back
    to the filing -- the one thing that makes a figure checkable.
    """
    allowed = allowed_numeric_tokens(events, history)
    unsourced: list[str] = []
    for raw in _NUM_TOKEN.findall(_URL.sub(" ", text)):
        tok = _normalize_token(raw)
        if not tok or tok in {"$", "%"}:
            continue
        if not _is_material(tok):
            continue  # ranks, lag days, cluster sizes, date fragments: not a risk
        if tok in allowed:
            continue
        unsourced.append(raw.strip())
    return unsourced


# Retained for callers outside this task's scope that still import the pre-rename name.
# `check_unsourced_numbers` is the name to use in new code; this is not deprecated so
# much as unwritten -- the rename didn't reach every caller in one pass.
find_unsourced_numbers = check_unsourced_numbers


def assert_no_hallucinated_numbers(
    text: str,
    events: list[NormalizedEvent],
    history: Mapping[str, HistoryContext] | None = None,
) -> None:
    """The hard gate. Raises `ValueError` and blocks the caller from sending on any hit."""
    bad = check_unsourced_numbers(text, events, history)
    if bad:
        raise ValueError(f"Unsourced numeric tokens in digest output: {bad}")
