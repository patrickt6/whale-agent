"""Digest render.

Every dollar figure is formatted directly from a structured field, so the numeric-
provenance invariant holds by construction. Generated prose is optional and additive:
pass a `DigestProse` and its sentences replace the template `why_it_matters` line and
add a per-event skeptic note; pass nothing and the digest is identical to the fully
deterministic Phase 0 output. Nothing downstream may depend on prose being present.
"""

from __future__ import annotations

from datetime import date

from whale_agent.errors import SourceFailure
from whale_agent.models.enums import Tier, TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.summarization.prose import DigestProse

_FLAGS = {
    "US": "🇺🇸",
    "CA": "🇨🇦",
    "UK": "🇬🇧",
    "EU": "🇪🇺",
    "TW": "🇹🇼",
    "JP": "🇯🇵",
    "HK": "🇭🇰",
    "KR": "🇰🇷",
    "IN": "🇮🇳",
    "AU": "🇦🇺",
    "CN": "🇨🇳",
    "BR": "🇧🇷",
}

_ACTION_LABEL = {
    TransactionType.OPEN_MARKET_BUY: "OPEN-MARKET BUY",
    TransactionType.OPEN_MARKET_SELL: "OPEN-MARKET SELL",
    TransactionType.ACTIVIST_13D: "NEW 13D STAKE",
    TransactionType.PASSIVE_13G: "13G STAKE",
    TransactionType.FUND_NEW_POSITION: "NEW FUND POSITION",
    TransactionType.FUND_ADD_POSITION: "ADD TO POSITION",
    TransactionType.GRANT: "COMP GRANT",
    TransactionType.OPTION_EXERCISE: "OPTION EXERCISE",
    TransactionType.SCHEDULED_SALE: "SCHEDULED SALE",
    TransactionType.OTHER: "OTHER",
}

_TIER_HEADING = {
    Tier.INSTANT: "TIER 1: INSTANT-WORTHY",
    Tier.NOTABLE: "TIER 2: NOTABLE",
    Tier.WEEKLY: "TIER 3: WEEKLY",
}


def format_usd(value: float | None) -> str:
    """Human dollar string. Returns 'not disclosed' for None."""
    if value is None:
        return "not disclosed"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def why_it_matters(ev: NormalizedEvent) -> str:
    """Deterministic one-liner naming the reasons this event ranked where it did.

    Every clause corresponds to a term the scorer actually used. That correspondence is
    the point: a reader who disagrees with the ordering can see which input produced it,
    and a row whose only claim is "it was big" says exactly that instead of dressing it
    up. Ordered strongest-first, then truncated -- five reasons is a shrug, two is an
    argument.

    The fallback line matters more than it looks. Once the cold-start guard suppresses
    the first-time flag, an unenriched event genuinely has nothing to say for itself, and
    saying so plainly is better than manufacturing a reason.
    """
    ctx = ev.context
    bits: list[str] = []

    if ev.transaction_type == TransactionType.ACTIVIST_13D:
        bits.append("activist intent (13D)")
    if ev.cluster_size >= 3:
        bits.append(f"cluster of {ev.cluster_size} insiders")

    # Size relative to the company beats size in dollars, so it is stated first.
    if ctx.percent_of_float is not None and ctx.percent_of_float >= 0.5:
        bits.append(f"{ctx.percent_of_float:.1f}% of float")
    elif ev.percent_of_company:
        bits.append(f"{ev.percent_of_company:.1f}% of company")

    small = ctx.is_small_cap()
    if small and ctx.market_cap_usd is not None:
        bits.append(f"small company at {format_usd(ctx.market_cap_usd)}")

    # Filer novelty, now measured rather than assumed.
    if ctx.filer_prior_filings == 0:
        bits.append("never seen this filer before")
    elif ctx.filer_days_since_last is not None and ctx.filer_days_since_last >= 365:
        bits.append("first filing in over a year")
    elif ev.is_first_time_filer:
        bits.append("first-time filer")

    if (
        ctx.filer_typical_usd
        and ev.effective_usd
        and ev.effective_usd >= 3 * ctx.filer_typical_usd
    ):
        bits.append("far larger than their usual position")

    if ctx.is_new_position:
        bits.append("newly opened position")

    if ev.is_routine:
        bits.append("routine, low signal")

    if not bits:
        bits.append("clears the $5M threshold, with nothing else to distinguish it")
    return "; ".join(bits[:3])


def _row(rank: int, ev: NormalizedEvent, prose: DigestProse | None = None) -> str:
    flag = _FLAGS.get(ev.jurisdiction.value, "")
    action = _ACTION_LABEL.get(ev.transaction_type, "OTHER")
    est = " (est.)" if ev.usd_value_is_estimate else ""
    usd = format_usd(ev.effective_usd)
    lag = (ev.disclosure_date - ev.transaction_date).days
    line1 = (
        f"{rank}. {ev.filer_name}: {action} {usd}{est} in {ev.issuer_name} "
        f"{flag} ({ev.jurisdiction.value}), {ev.disclosure_date.isoformat()}, "
        f"{lag}-day lag"
    )
    # Generated prose replaces the template line when available; the template is the
    # fallback, so a missing or rejected sentence costs nothing.
    generated = prose.why_it_matters.get(ev.event_id) if prose else None
    line2 = f"   Why: {generated or (why_it_matters(ev) + '.')}"
    if ev.source_url:
        line2 += f" [{ev.source_url}]"
    lines = [line1, line2]
    # The CMP opportunistic/routine label, always shown and never omitted. A missing
    # value here must read as "unknown" rather than disappear: silence would let a
    # reader assume "opportunistic" (the unflagged default), which is exactly the kind
    # of claim the numeric-provenance gate exists to catch for figures and that this
    # line exists to prevent for a signal instead. This label is descriptive only --
    # see `enrichment/routine_classifier.py` -- and never changes the ranking above.
    lines.append(f"   Trading pattern: {ev.routine_label or 'unknown'}")
    note = prose.skeptic_notes.get(ev.event_id) if prose else None
    if note:
        lines.append(f"   Skeptic's note: {note}")
    elif ev.is_routine:
        # : a routine classification must always surface a caveat, prose
        # layer or not.
        lines.append(
            "   Skeptic's note: this looks like a routine, calendar-driven trade; low signal."
        )
    return "\n".join(lines)


def render_coverage_note(coverage_notes: list[str] | None) -> str:
    """One line naming any source that did not report, so silence is never mistaken
    for an empty day."""
    if not coverage_notes:
        return ""
    return "Coverage note: " + "; ".join(coverage_notes) + "."


def render_coverage_note_from_failures(failures: list[SourceFailure] | None) -> str:
    """The same coverage line, built from structured failures rather than raw strings.

    A collection loop that adopts `SourceFailure` (see `errors.py`) has, by construction,
    already separated "a source failed" from "a source legitimately reported nothing":
    only the first produces a `SourceFailure` at all. This renders exactly the sentence
    `render_coverage_note` would have produced from the equivalent strings, via
    `describe()`, so the reader-visible line does not change shape when a caller migrates
    to the structured type -- only the thing it was computed from does.
    """
    if not failures:
        return ""
    return render_coverage_note([f.describe() for f in failures])


def render_digest(
    events: list[NormalizedEvent],
    on: date | None = None,
    prose: DigestProse | None = None,
    coverage_notes: list[str] | None = None,
) -> str:
    """Render ranked events (already scored, gate-passing, sorted) to markdown."""
    on = on or date.today()
    lines: list[str] = [f"WHALE DIGEST, {on.isoformat()}"]
    coverage = render_coverage_note(coverage_notes)

    if not events:
        lines.append("\nNo disclosed moves cleared the $5M threshold today.")
        if coverage:
            lines.append(coverage)
        return "\n".join(lines)

    top = events[0]
    lines.append(
        f"Top move: {top.filer_name}: {_ACTION_LABEL.get(top.transaction_type, 'OTHER')} "
        f"{format_usd(top.effective_usd)} in {top.issuer_name}."
    )
    if prose and prose.headline:
        lines.append(prose.headline)
    if coverage:
        lines.append(coverage)

    rank = 1
    for tier in (Tier.INSTANT, Tier.NOTABLE, Tier.WEEKLY):
        group = [e for e in events if e.tier == tier]
        if not group:
            continue
        lines.append("\n" + _TIER_HEADING[tier])
        for ev in group:
            lines.append(_row(rank, ev, prose))
            rank += 1

    lines.append(
        "\nEvery figure traces to a filing field. Each row's trading-pattern label "
        "(opportunistic/routine/unknown, Cohen-Malloy-Pomorski 2012) is shown for "
        "context and does not affect ranking. Awareness tool, not investment advice."
    )
    return "\n".join(lines)
