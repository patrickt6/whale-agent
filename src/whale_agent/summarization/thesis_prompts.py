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
    """The only view of one supporting filing the model ever sees."""
    return {
        "event_id": event.event_id,
        "filer_name": event.filer_name,
        "filer_type": event.filer_type,
        "issuer_name": event.issuer_name,
        "ticker": event.ticker,
        "jurisdiction": event.jurisdiction.value,
        "action": event.transaction_type.value,
        "usd_display": format_usd(event.effective_usd),
        "usd_is_estimate": event.usd_value_is_estimate,
        "price_source": event.price_source.value,
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


def pattern_payload(pattern: Pattern) -> dict[str, Any]:
    """The finding, its rows, and the seed falsifier the model must argue against.

    `metrics` are counts the detector computed, passed as pre-formatted strings so a
    ratio cannot be re-derived into a new figure.
    """
    events = pattern.events
    priced = [e.effective_usd for e in events if e.effective_usd is not None]
    return {
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
        "supporting_filings": [event_payload(e) for e in events],
    }


def build_user_prompt(pattern: Pattern) -> str:
    """Serialize one already-established pattern as the model's sole input."""
    return (
        "Write the research note for the following finding. The finding was established "
        "by code from the filings listed; do not re-evaluate whether it holds.\n\n"
        + json.dumps(pattern_payload(pattern), indent=2, ensure_ascii=False)
    )
