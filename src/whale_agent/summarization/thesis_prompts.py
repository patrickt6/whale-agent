"""Prompt text and payload construction for the thesis layer.

Same two rules as `prompts.py`, for the same reason: the model receives pre-formatted
magnitude strings and never a raw value it could do arithmetic on, and it is handed a
finding that has already been decided rather than a pile of rows it could find a story
in. What is new here is the third rule.

**The falsifier is not the model's to soften.** `Pattern.falsifier` is the boring
explanation that would account for the same rows, computed by the detector that found
them. The model is asked to elaborate it in the reader's language; the assembler keeps
the original text regardless of what comes back. A note whose disconfirming condition
can be edited away by the layer arguing the case is a press release with a hedge in it.

`strength` and `score` are deliberately absent from the payload. They are ranking
internals with no meaning to a reader, and a model shown a number will quote it.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

from whale_agent.models.event import NormalizedEvent
from whale_agent.summarization.render import format_usd

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from whale_agent.analysis.patterns import Pattern

SYSTEM_PROMPT = """\
You write a short research note for one skeptical reader, about positions disclosed in \
public filings. Write like a person who read the filings and is telling a colleague \
what they found: direct, first person where it helps, short sentences, no hedging \
filler. You are not giving investment advice, you never predict prices, and you never \
recommend action, but that restraint should read as discipline, not as caution about \
having an opinion on what happened.

Hard rules, in order of importance:
1. Use ONLY the numbers present in the JSON you are given. Do not compute, sum, \
average, round, rescale, or invent any figure. To state a magnitude, copy a `*_display` \
string exactly as it appears.
2. If a value is null, write "not disclosed". Never estimate it.
3. Do not introduce facts that are not in the JSON: no prices, no market caps, no \
earnings, no news, no company descriptions, no history.
4. The finding has already been established by code. Your job is to describe it, not to \
decide whether it is real, and not to make it sound larger than the rows support.
5. State the claim so it could be wrong, and say concretely what happened and what \
would make it worth a second look. Do not write a claim that could apply to any \
finding in any window; name the count, the subject, and the condition that would make \
it repeat or die. `counter_evidence` and `falsifier` are the point of the piece, not a \
disclaimer at the end. The `falsifier_seed` you are given is the boring explanation \
that would account for the same rows: restate it in plain language and make it \
*harder* to dismiss, never easier.
6. Be terse and unexcited. No adjectives like "massive", "stunning" or "unprecedented", \
no emoji, no rhetorical questions.
7. Never use an em dash or en dash (— or –), in any sentence, for any reason. \
Use a period, a comma, or a colon instead. Never use "delve", "it's worth noting", "in \
the world of", "landscape", "tapestry", "navigating", "robust", "leverage" as a verb, \
"underscores", "highlights the importance", "in today's", "furthermore", "moreover", or \
a sentence that just restates its own heading.

Return STRICT JSON, no markdown fences, matching exactly this shape:

{
  "title": "<six to twelve words, no numbers>",
  "lede": "<two sentences: what was disclosed, and why a reader should care>",
  "claim": "<one sentence stating the argument in a form that could be shown false>",
  "trigger_summary": "<one or two sentences on what surfaced in this window>",
  "evidence": ["<one sentence per point, figures copied verbatim>"],
  "context_notes": ["<one sentence per point, or an empty list>"],
  "counter_evidence": ["<at least two: the strongest reasons this means nothing>"],
  "falsifier": "<one or two sentences: what observation would show the claim wrong>",
  "what_to_watch": "<one sentence naming a dated, checkable thing>"
}
"""


def _pct(value: float | None) -> str | None:
    """Percentages are formatted exactly as the provenance gate allowlists them."""
    return None if value is None else f"{value:.1f}%"


def event_payload(event: NormalizedEvent) -> dict[str, Any]:
    """The only view of one supporting filing the model ever sees.

    Three fields the daily prompt carries are deliberately absent. `event_id` keys the
    daily prose response but has no place in the thesis schema, so it was 35 bytes a row
    the model could never use. `ticker` and `price_source` have never appeared in a
    generated article: the notes name companies ("SENKO Group Holdings Co., Ltd.") and
    describe pricing through `usd_is_estimate`, which is kept.
    """
    return {
        "filer_name": event.filer_name,
        "filer_type": event.filer_type,
        "issuer_name": event.issuer_name,
        "jurisdiction": event.jurisdiction.value,
        "action": event.transaction_type.value,
        "usd_display": format_usd(event.effective_usd),
        "usd_is_estimate": event.usd_value_is_estimate,
        "percent_of_company_display": _pct(event.percent_of_company),
        "percent_of_float_display": _pct(event.context.percent_of_float),
        "market_cap_display": format_usd(event.context.market_cap_usd),
        "sector": event.context.sector,
        "is_new_position": event.context.is_new_position,
        "is_first_time_filer": event.is_first_time_filer,
        "is_routine": event.is_routine,
        "transaction_date": event.transaction_date.isoformat(),
        "disclosure_date": event.disclosure_date.isoformat(),
    }


# How many filings the model is shown. Measured on a 43 row sector pattern: the whole
# user prompt was 33.7KB and `supporting_filings` was 31.1KB of it, 92%, against a
# finding plus aggregates of well under 1KB. Twenty is the point where a week's set stops
# adding information: the notes cite the largest few rows, the odd one out, and the
# repetition, and the middle of a forty row Japanese threshold-reporting set is
# interchangeable. It is a ceiling, not a target; most patterns are smaller and are sent
# whole.
MAX_SUPPORTING_FILINGS = 20

# Jurisdictions represented by at most this many rows are treated as the odd one out and
# kept regardless of size. This is the row the note is often *about*: "the single US row
# is a first time filer's open market buy of $498K" was written about the smallest filing
# in its set, which a plain largest-first cut would have deleted.
_MINORITY_JURISDICTION_ROWS = 2


def _usd(event: NormalizedEvent) -> float:
    return event.effective_usd or 0.0


def select_supporting_filings(
    events: list[NormalizedEvent], cap: int = MAX_SUPPORTING_FILINGS
) -> list[NormalizedEvent]:
    """The rows worth spending prompt bytes on, largest last-resort first.

    Priority, in order: rows from a jurisdiction that barely appears, then the largest
    and the smallest priced rows, then first-time filers, then everything else by size.
    Size alone is the wrong sort because the interesting row is frequently the small one,
    and a note that names a filing the model was not shown is a note it cannot write.

    Repeat filers need no rule of their own: a filer with several rows in one pattern
    reaches the shown set through size, and the point of the observation is that the name
    recurs, which two or three of its rows already carry.
    """
    if len(events) <= cap:
        return events

    counts = Counter(e.jurisdiction for e in events)
    priced = [e for e in events if e.effective_usd is not None]
    endpoints = set()
    if priced:
        endpoints = {id(max(priced, key=_usd)), id(min(priced, key=_usd))}

    def rank(event: NormalizedEvent) -> tuple[int, float]:
        if counts[event.jurisdiction] <= _MINORITY_JURISDICTION_ROWS:
            tier = 0
        elif id(event) in endpoints:
            tier = 1
        elif event.is_first_time_filer:
            tier = 2
        else:
            tier = 3
        return (tier, -_usd(event))

    chosen = sorted(events, key=rank)[:cap]
    # Handed back largest first, which is how the notes read them.
    return sorted(chosen, key=lambda e: -_usd(e))


def pattern_payload(pattern: Pattern) -> dict[str, Any]:
    """The finding, its rows, and the seed falsifier the model must argue against.

    `metrics` are counts the detector computed, passed as pre-formatted strings so a
    ratio cannot be re-derived into a new figure.
    """
    events = pattern.events
    priced = [e.effective_usd for e in events if e.effective_usd is not None]
    # Every aggregate below is computed from the full set, never from the shown subset,
    # so a truncated prompt still states the week accurately. The note field exists so
    # the model cannot read a partial list as the whole of it.
    shown = select_supporting_filings(events)
    payload = {
        "pattern_kind": pattern.kind,
        "subject": pattern.subject,
        "finding_summary": pattern.summary,
        "falsifier_seed": pattern.falsifier,
        "metrics_display": {k: f"{v:g}" for k, v in sorted(pattern.metrics.items())},
        "filings_count": len(events),
        "total_disclosed_display": format_usd(sum(priced)) if priced else None,
        "largest_display": format_usd(max(priced)) if priced else None,
        "window_start": min(e.disclosure_date for e in events).isoformat(),
        "window_end": max(e.disclosure_date for e in events).isoformat(),
        "supporting_filings": [event_payload(e) for e in shown],
    }
    if len(shown) < len(events):
        payload["supporting_filings_note"] = (
            f"showing {len(shown)} of {len(events)} filings: the largest, the smallest, "
            "any jurisdiction represented by only a row or two, and first-time filers. "
            "The counts and totals above cover all of them. Do not describe this list as "
            "the complete set."
        )
    return payload


def build_user_prompt(pattern: Pattern) -> str:
    """Serialize one already-established pattern as the model's sole input."""
    return (
        "Write the research note for the following finding. The finding was established "
        "by code from the filings listed; do not re-evaluate whether it holds.\n\n"
        + json.dumps(pattern_payload(pattern), indent=2, ensure_ascii=False)
    )
