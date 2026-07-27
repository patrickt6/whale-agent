"""Thesis generation: turn an already-established pattern into an article.

The division of labour is the same one `analysis/patterns.py` sets up and it is the whole
design. Code decided that something happened; this module decides how to say it. Nothing
here can promote a weak pattern, add a filing to one, or change a figure.

Three properties are worth explaining, because each is a deliberate cost.

**The gate runs against `pattern.events`, not the week.** A thesis about four filings is
checked against those four filings only. Checking against the full window would let a
figure belonging to some unrelated issuer pass silently -- the allowlist is a union, so a
wider window is a weaker gate, exactly backwards from how it looks.

**The falsifier survives the model.** `Pattern.falsifier` is copied into the thesis
verbatim and the model's version can only be appended to it. Every other field is the
model's to write and the model's to lose; this one is not, because a layer arguing a case
must not also hold the pen on what would refute it.

**No model is the normal case, not the failure case.** The deterministic fallback is a
complete, publishable article assembled from the pattern's own summary and the events'
fields. It reads plainer. It is not a degraded mode with a warning attached, and the
weekly does not distinguish the two -- which is the only honest arrangement when the
product's claim is that the figures, not the sentences, are the thing being sold.

A thesis that fails `is_publishable()` returns None. A quiet week should produce nothing.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date
from typing import TYPE_CHECKING, Any

from whale_agent.enrichment.plausibility import is_placeholder_issuer
from whale_agent.errors import NotConfiguredError, SourceUnavailableError
from whale_agent.models.enums import FilerType, TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.models.thesis import Citation, Thesis
from whale_agent.publishing.charts import choose_chart_kind
from whale_agent.summarization.llm import LLMProvider, _extract_json
from whale_agent.summarization.provenance import check_unsourced_numbers
from whale_agent.summarization.render import format_usd
from whale_agent.summarization.render_html import _ACTION_HTML
from whale_agent.summarization.thesis_prompts import SYSTEM_PROMPT, build_user_prompt

if TYPE_CHECKING:  # pragma: no cover
    from whale_agent.analysis.patterns import Pattern

log = logging.getLogger(__name__)

__all__ = ["generate_thesis", "generate_theses", "fallback_thesis", "slug_for", "headline_for"]

# Reporting regimes whose data is a quarter-old snapshot rather than a fresh event. Any
# pattern resting on these carries a staleness caveat that is true by construction.
_LAGGED_TYPES = {
    TransactionType.FUND_NEW_POSITION,
    TransactionType.FUND_ADD_POSITION,
}

# Headline phrasing per detector: actor, then money, then the company. Written here rather
# than generated so that a week with no model available still produces titles a reader can
# tell apart -- and, more importantly, so the headline is never a place a model could put a
# number. Every figure below is computed from `pattern.events`, which is the same allowlist
# the provenance gate checks the finished page against.
#
# The verb differs per detector because the actions differ: buying into a name is not the
# same claim as opening a position in it, and a headline that flattens them is telling the
# reader something the filings do not say.
_HEADLINE = {
    "issuer_cluster": "{actor} put {amount} into {company}",
    "new_position_wave": "{actor} opened {amount} of new positions in {company}",
    "cross_jurisdiction": "{actor} filed on {company} in more than one regime",
    "unusual_concentration": "{actor} put {amount} into {company}, well above its norm",
    "filer_across_issuers": "{company} deployed {amount} across several names",
    "sector_concentration": "{actor} put {amount} into {company} names",
}

# Without a figure to carry. Used when nothing in the pattern is priced, because a headline
# implying a total we never learned is the exact failure the whole product guards against.
_HEADLINE_UNPRICED = {
    "issuer_cluster": "{actor} bought into {company}",
    "new_position_wave": "{actor} opened new positions in {company}",
    "cross_jurisdiction": "{actor} filed on {company} in more than one regime",
    "unusual_concentration": "{actor} filed on {company} well above its norm",
    "filer_across_issuers": "{company} filed across several names at once",
    "sector_concentration": "{actor} bought into {company} names",
}

# Spelled out rather than printed as digits: a headline reads as prose, and "6 insiders"
# in a product whose credibility rests on figures makes a count look like a measurement.
_COUNT_WORDS = {
    1: "One",
    2: "Two",
    3: "Three",
    4: "Four",
    5: "Five",
    6: "Six",
    7: "Seven",
    8: "Eight",
    9: "Nine",
    10: "Ten",
    11: "Eleven",
    12: "Twelve",
}

# What to call the filers. Keyed on the dominant filer type in the pattern.
_ACTOR_NOUN = {
    FilerType.INSIDER: ("insider", "insiders"),
    FilerType.FUND: ("institution", "institutions"),
    FilerType.SWF: ("sovereign fund", "sovereign funds"),
    FilerType.FAMILY_OFFICE: ("family office", "family offices"),
    FilerType.INDIVIDUAL: ("investor", "investors"),
    FilerType.UNKNOWN: ("filer", "filers"),
}


def _company_name(pattern: Pattern, events: list[NormalizedEvent]) -> str:
    """The company as a person would say it, falling back to the symbol.

    FMP returns `companyName` as null often enough that the issuer frequently arrives as
    nothing but its own ticker, which is how an article once went out headlined "FSBC".
    Prefer a real name from any supporting filing; use the symbol only when no filing
    supplied one.
    """
    for ev in events:
        name = (ev.issuer_name or "").strip()
        if not name or is_placeholder_issuer(name):
            continue
        if ev.ticker and name.upper() == ev.ticker.strip().upper():
            continue  # the name is just the symbol wearing a hat
        return name
    return pattern.subject


def _actor(events: list[NormalizedEvent]) -> str:
    """ "Six insiders", "One institution" -- distinct filers, named by what they are."""
    distinct: dict[str, FilerType] = {}
    for ev in events:
        distinct.setdefault((ev.filer_id or ev.filer_name).strip().lower(), ev.filer_type)
    n = len(distinct)
    dominant = (
        Counter(distinct.values()).most_common(1)[0][0] if distinct else FilerType.UNKNOWN
    )
    singular, plural = _ACTOR_NOUN.get(dominant, _ACTOR_NOUN[FilerType.UNKNOWN])
    word = _COUNT_WORDS.get(n, str(n))
    return f"{word} {singular if n == 1 else plural}"


def headline_for(pattern: Pattern) -> str:
    """The article title: who acted, how much, and into what.

    Built from the pattern's own filings and nothing else. The dollar figure is the sum of
    the priced supporting events, so it is a number the reader can re-add from the evidence
    table on the same page; when none of them are priced the headline simply does not carry
    one.
    """
    events = pattern.events
    priced = [e.effective_usd for e in events if e.effective_usd is not None]
    company = _company_name(pattern, events)
    actor = _actor(events)
    if priced:
        template = _HEADLINE.get(pattern.kind, "{actor} put {amount} into {company}")
        return template.format(actor=actor, amount=format_usd(sum(priced)), company=company)
    template = _HEADLINE_UNPRICED.get(pattern.kind, "{actor} bought into {company}")
    return template.format(actor=actor, company=company)


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug_for(pattern: Pattern, on: date) -> str:
    """Stable across regenerations: same pattern on the same date is the same URL."""
    subject = _SLUG_STRIP.sub("-", pattern.subject.lower()).strip("-") or "subject"
    return f"{on.isoformat()}-{pattern.kind.replace('_', '-')}-{subject}"[:120]


# -- citations ----------------------------------------------------------------------


def build_citations(events: list[NormalizedEvent]) -> list[Citation]:
    """One pointer per supporting filing, plus one for any vendor field used.

    Built in code from the events, never from the model. `detail` is written so a reader
    can re-find the filing without trusting the link: the regime, the parties, and the
    transaction date are enough to search any of these registries directly.
    """
    citations: list[Citation] = []
    for ev in events:
        citations.append(
            Citation(
                label=(
                    f"{ev.filer_name} in {ev.issuer_name}, "
                    f"filed {ev.disclosure_date.isoformat()}"
                ),
                url=ev.source_url,
                detail=(
                    f"{ev.jurisdiction.value} disclosure via {ev.source}; "
                    f"transaction dated {ev.transaction_date.isoformat()}"
                    + (f"; issuer id {ev.issuer_id}" if ev.issuer_id else "")
                ),
                source=ev.source,
            )
        )
    if any(
        e.context.market_cap_usd is not None or e.context.percent_of_float is not None
        for e in events
    ):
        citations.append(
            Citation(
                label="Company scale and float",
                detail=(
                    "Market capitalisation and shares-float fields attached at enrichment "
                    "time from the market-data vendor profile endpoint"
                ),
                source="fmp",
            )
        )
    return citations


# -- deterministic assembly ---------------------------------------------------------


def _action(ev: NormalizedEvent) -> str:
    return _ACTION_HTML.get(ev.transaction_type, "Disclosure")


def _lede(pattern, events: list[NormalizedEvent]) -> str:
    """The opening line, which must not simply restate the section below it.

    `pattern.summary` is the detector's own sentence and it already appears verbatim as
    the trigger. Repeating it as the lede makes the first two paragraphs identical, which
    is the single most obvious tell that a page was assembled rather than written. So the
    lede leads with the company-level fact the trigger does not carry -- scale -- and
    leaves the counting to the section whose job that is.
    """
    caps = [e.context.market_cap_usd for e in events if e.context.market_cap_usd is not None]
    filers = len({(e.filer_id or e.filer_name).lower() for e in events})
    issuers = sorted({e.issuer_name for e in events})
    subject = issuers[0] if len(issuers) == 1 else f"{len(issuers)} companies"

    opening = (
        f"{filers} separate filers showed up on {subject} this week"
        if filers > 1
        else f"One filer showed up on {subject} this week"
    )
    if caps and len(issuers) == 1:
        return f"{opening}, a company worth {format_usd(min(caps))}."
    return opening + "."


def _evidence_lines(events: list[NormalizedEvent]) -> list[str]:
    """One line per filing, every figure copied from a field.

    Company-level facts are stated once rather than on every row. Repeating "company
    valued at $412.0M" four times for four filings in the same issuer reads as
    generated text, and it buries the per-filing figures that actually differ.
    """
    issuers = {e.issuer_name for e in events}
    repeat_market_cap = len(issuers) > 1

    lines: list[str] = []
    for ev in sorted(events, key=lambda e: (e.disclosure_date, e.filer_name)):
        parts = [
            f"{ev.filer_name}, {_action(ev).lower()} in {ev.issuer_name}",
            format_usd(ev.effective_usd),
        ]
        if ev.percent_of_company is not None:
            parts.append(f"{ev.percent_of_company:.1f}% of the company")
        if ev.context.percent_of_float is not None:
            parts.append(f"{ev.context.percent_of_float:.1f}% of float")
        if repeat_market_cap and ev.context.market_cap_usd is not None:
            parts.append(f"company valued at {format_usd(ev.context.market_cap_usd)}")
        parts.append(
            f"transacted {ev.transaction_date.isoformat()}, "
            f"filed {ev.disclosure_date.isoformat()}"
        )
        lines.append(", ".join(parts) + ".")
    return lines


def _context_lines(events: list[NormalizedEvent]) -> list[str]:
    """Only what enrichment actually attached. Absent context stays absent."""
    lines: list[str] = []
    sectors = sorted({e.context.sector for e in events if e.context.sector})
    if sectors:
        lines.append(f"Sector as classified by the vendor profile: {', '.join(sectors)}.")
    caps = [e.context.market_cap_usd for e in events if e.context.market_cap_usd is not None]
    if caps:
        lines.append(
            "The evidence for insider signal was concentrated in small caps, so company "
            f"scale is the relevant denominator; the smallest here is valued at "
            f"{format_usd(min(caps))}."
        )
    firsts = [e.filer_name for e in events if e.is_first_time_filer]
    if firsts:
        lines.append(
            "First appearance in our records for: " + ", ".join(sorted(set(firsts))) + "."
        )
    return lines


def _carry(n: int) -> str:
    return "carries" if n == 1 else "carry"


def _is(n: int) -> str:
    return "is" if n == 1 else "are"


def _counter_evidence(pattern: Pattern) -> list[str]:
    """The reasons this is nothing, stated before anyone has to ask for them."""
    events = pattern.events
    notes = [
        "A disclosure is a record of a transaction, not a view about it: a filer buying a "
        "name discloses because a rule required it, not because they wish to be read."
    ]
    unpriced = sum(1 for e in events if e.effective_usd is None)
    if unpriced:
        notes.append(
            f"{unpriced} of the {len(events)} filings {_carry(unpriced)} no disclosed "
            "value, so any total here is a floor rather than the size of the position."
        )
    estimated = sum(1 for e in events if e.usd_value_is_estimate)
    if estimated:
        notes.append(
            f"{estimated} of the {len(events)} figures {_is(estimated)} estimated from a "
            "market close rather than stated in the filing, and a close is not an "
            "execution price."
        )
    if any(e.transaction_type in _LAGGED_TYPES for e in events):
        notes.append(
            "Institutional position data is reported with a quarter's lag and on a common "
            "deadline, so it describes a past portfolio rather than a current view."
        )
    if any(e.is_routine for e in events):
        notes.append(
            "At least one of these is classified routine. Calendar-driven trades have "
            "historically carried little signal, and the classification is ours, not the "
            "filer's."
        )
    lags = [(e.disclosure_date - e.transaction_date).days for e in events]
    if lags and max(lags) > 0:
        notes.append(
            f"The longest gap between transaction and disclosure here is {max(lags)} days, "
            "and whatever the filer knew is that much older than this note."
        )
    return notes


def _claim(pattern: Pattern, events: list[NormalizedEvent]) -> str:
    """State what happened and what would make it worth a second look, concretely.

    The old version of this sentence said almost nothing: it gestured at
    "concentration" without saying what happened or what would separate signal from
    noise. This version names the count and the subject, then says plainly what
    repeating would mean and what not repeating would mean, so the claim can actually
    be checked against next week's filings.
    """
    filers = len({(e.filer_id or e.filer_name).strip().lower() for e in events})
    n = _COUNT_WORDS.get(len(events), str(len(events)))
    plural = "filings" if len(events) != 1 else "filing"
    if filers > 1:
        return (
            f"{n} {plural} on {pattern.subject} landed in the same window from separate "
            "filers. That is the whole finding. If it keeps happening next week, this "
            "was a decision. If it does not, it was the calendar."
        )
    return (
        f"{n} {plural} on {pattern.subject} landed in the same window. That is the whole "
        "finding. It gets interesting if the same filer or others follow it up; it does "
        "not if this was a one-off."
    )


def _what_to_watch(pattern: Pattern) -> str:
    if pattern.kind in {"issuer_cluster", "unusual_concentration", "new_position_wave"}:
        return (
            f"Whether more filers disclose purchases in {pattern.subject} in the windows "
            "after this one. If the count stops here, the calendar explanation wins."
        )
    if pattern.kind == "filer_across_issuers":
        return (
            f"Whether {pattern.subject} discloses these names again outside a quarterly "
            "reporting deadline. That is what would separate a decision from a filing "
            "calendar."
        )
    if pattern.kind == "cross_jurisdiction":
        return (
            f"Whether the {pattern.subject} filings in each regime describe distinct "
            "transactions or one transaction reported twice. The transaction dates and "
            "share counts on the filings themselves settle it."
        )
    return (
        f"Whether disclosed buying in {pattern.subject} continues in subsequent windows "
        "rather than reverting to the usual rate."
    )


def fallback_thesis(pattern: Pattern, on: date) -> Thesis | None:
    """A complete article assembled with no model at all.

    Plainer than a written one, and every sentence traceable to a field. This is the
    output when the provider is absent, broken, or produced something that failed the
    gate, and it is a publishable article rather than a placeholder.
    """
    events = pattern.events
    citations = build_citations(events)
    priced = [e.effective_usd for e in events if e.effective_usd is not None]
    # Some detectors already put the total in their summary; repeating it in the next
    # breath reads as padding, which is the one thing the lede cannot afford.
    (
        f" The largest single disclosure is {format_usd(max(priced))}."
        if priced and "totalling" in pattern.summary
        else f" The disclosed total across the priced filings is {format_usd(sum(priced))}."
        if priced
        else ""
    )

    thesis = Thesis(
        slug=slug_for(pattern, on),
        title=headline_for(pattern),
        published_on=on,
        kind="pattern",
        lede=_lede(pattern, events),
        claim=_claim(pattern, events),
        trigger_summary=pattern.summary,
        evidence=_evidence_lines(events),
        context_notes=_context_lines(events),
        counter_evidence=_counter_evidence(pattern),
        falsifier=pattern.falsifier,
        what_to_watch=_what_to_watch(pattern),
        citations=citations,
        event_ids=pattern.event_ids,
        pattern_kind=pattern.kind,
        chart_kind=choose_chart_kind(events),
    )
    return thesis if thesis.is_publishable() else None


# -- model path ---------------------------------------------------------------------


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for s in (_clean(v) for v in value) if s]


def _keep(text: str, events: list[NormalizedEvent], rejected: list[str]) -> str:
    """Drop one string if it carries a figure the supporting filings cannot account for."""
    if not text:
        return ""
    bad = check_unsourced_numbers(text, events)
    if bad:
        rejected.append(f"{bad}: {text}")
        return ""
    return text


def _apply_model_output(base: Thesis, data: dict, events: list[NormalizedEvent]) -> Thesis:
    """Overlay the model's language on the deterministic thesis, field by field.

    Per-field rather than all-or-nothing, matching `sanitize_prose`: a bad sentence costs
    a sentence. Any field the model loses falls back to the assembled version, so the
    result is always complete. The falsifier is only ever extended.
    """
    rejected: list[str] = []

    def one(key: str, current: str) -> str:
        return _keep(_clean(data.get(key)), events, rejected) or current

    def many(key: str, current: list[str]) -> list[str]:
        kept = [
            s for s in (_keep(v, events, rejected) for v in _clean_list(data.get(key))) if s
        ]
        return kept or current

    elaboration = _keep(_clean(data.get("falsifier")), events, rejected)
    falsifier = base.falsifier
    if elaboration and elaboration.lower() not in falsifier.lower():
        falsifier = f"{falsifier} {elaboration}"

    updated = base.model_copy(
        update={
            "title": one("title", base.title),
            "lede": one("lede", base.lede),
            "claim": one("claim", base.claim),
            "trigger_summary": one("trigger_summary", base.trigger_summary),
            "evidence": many("evidence", base.evidence),
            "context_notes": many("context_notes", base.context_notes),
            # Counter-evidence is the one list where the model's version is added to
            # rather than substituted: the deterministic caveats are facts about the data
            # (unpriced rows, reporting lag) and stay true whatever the model wrote.
            "counter_evidence": base.counter_evidence
            + [
                c
                for c in _clean_list(data.get("counter_evidence"))
                if _keep(c, events, rejected)
            ],
            "falsifier": falsifier,
            "what_to_watch": one("what_to_watch", base.what_to_watch),
        }
    )
    if rejected:
        log.warning(
            "Dropped %d thesis sentence(s) containing unsourced numbers: %s",
            len(rejected),
            rejected,
        )
    return updated


def generate_thesis(
    pattern: Pattern,
    provider: LLMProvider | None = None,
    on: date | None = None,
) -> Thesis | None:
    """One article from one pattern, or None if it cannot be published honestly.

    Never raises. Every failure path -- no provider, transport error, unparseable
    response, fabricated figure -- lands on the deterministic article, because a pattern
    that survived detection deserves to be described whether or not a model is reachable.
    """
    on = on or date.today()
    base = fallback_thesis(pattern, on)
    if base is None or provider is None:
        return base

    try:
        raw = provider.complete(SYSTEM_PROMPT, build_user_prompt(pattern))
    except (NotConfiguredError, SourceUnavailableError) as exc:
        log.warning("Thesis prose unavailable (%s): %s", provider.name, exc)
        return base
    except Exception as exc:  # a provider is third-party code; it must not cost the article
        log.warning("Thesis provider failed (%s): %s", provider.name, exc)
        return base

    try:
        data = _extract_json(raw)
    except ValueError as exc:
        log.warning("Thesis response was not usable JSON (%s): %s", provider.name, exc)
        return base

    written = _apply_model_output(base, data, pattern.events)
    return written if written.is_publishable() else base


def generate_theses(
    patterns: list[Pattern],
    provider: LLMProvider | None = None,
    on: date | None = None,
    limit: int | None = None,
) -> list[Thesis]:
    """Articles for ranked patterns, strongest first, skipping any that cannot ship.

    `limit` is a volume control, not a quality one: the patterns arrive ranked and this
    takes a prefix. Padding a quiet week to reach a count is the failure mode the whole
    layer exists to avoid, so nothing is added when the list runs short.
    """
    on = on or date.today()
    chosen = patterns if limit is None else patterns[:limit]
    out: list[Thesis] = []
    for pattern in chosen:
        thesis = generate_thesis(pattern, provider, on)
        if thesis is not None:
            out.append(thesis)
    return out
