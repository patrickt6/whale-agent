"""Pattern detection: code decides what counts as a pattern, the model only describes it.

The thesis layer exists because a week of filings sometimes tells a story. The danger is
that a language model handed three thousand rows will produce a story either way -- it
has no way to tell "four funds opened the same name" from "four rows happen to be
adjacent in my context window". So the decision of what is notable is made here, in
arithmetic, and the model is handed only the patterns that survive, with the rows that
constitute them attached. Nothing reaches the prose stage that cannot be counted.

Three commitments follow from that, and they explain most of the code below.

**Conservatism, because the sample is tiny.** A week yields roughly 10-30 events that
clear the $5M gate. At that volume coincidence is the rule: with a dozen buys spread over
eleven GICS sectors, two landing in the same sector is the expected outcome, not a
rotation. Every threshold here is therefore set above what chance comfortably produces at
that volume, and `strength` grows with evidence *beyond* the threshold rather than
treating a bare pass as a finding. `MIN_REPORTABLE_STRENGTH` then drops the bare passes.

**Distinctness is counted, never filings.** One insider filing three Form 4s in a week is
one person changing their mind about paperwork; three insiders filing once each is three
people acting. Every detector counts distinct filers or distinct issuers, so an amended
or split filing cannot manufacture a cluster.

**Unknown is not zero.** `EventContext` fields are all optional and `None` means "we did
not learn this". A detector that reads `sector` skips events without one rather than
grouping them under a null sector -- a "pattern" among six companies whose only shared
property is that enrichment failed on them is an artifact of our vendor coverage.

Every function here is pure: no clock, no network, no database. A baseline for "unusual
versus normal" is injected as a callable, because the alternative -- querying history
inside a detector -- would make the honest thresholds untestable.

Each `Pattern` carries a `falsifier`: the boring explanation that would account for the
same rows. It travels downstream so the prose layer must argue against it rather than
around it, and so a reader is told what the pattern is not evidence of.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from whale_agent.models.enums import TransactionType
from whale_agent.models.event import NormalizedEvent
from whale_agent.summarization.render import format_usd

# -- thresholds -------------------------------------------------------------------
#
# Each is a judgement call about a ~10-30 event week, and each is written here rather
# than inline so it can be argued with.

# Three distinct filers into one issuer. Two is a pair, and pairs happen: a company with
# eight officers reporting in the same disclosure window produces two filings routinely,
# often because both were released from the same blackout. Three independent people
# choosing the same name in the same days is the smallest count that is awkward to
# explain by the calendar alone.
MIN_CLUSTER_FILERS = 3

# Three distinct *issuers* in one sector. With roughly a dozen gated buys spread over
# eleven sectors, two-in-a-sector is the modal outcome of pure chance; three is not.
# Distinct issuers, not filings, or one heavily-covered company becomes a sector call.
MIN_SECTOR_ISSUERS = 3

# One filer into three distinct issuers in the window. A fund reporting two names is
# ordinary portfolio maintenance. Three at once starts to look like a deployment.
MIN_FILER_ISSUERS = 3

# Two jurisdictions is the whole pattern here: the point is that separate national
# disclosure regimes independently surfaced the same issuer, which cannot happen by
# clerical accident the way two filings in one regime can.
MIN_JURISDICTIONS = 2

# Activity at an issuer must be at least 3x its own trailing norm AND clear an absolute
# floor. The ratio alone is worthless on small numbers -- an issuer that normally sees
# 0.3 events a week hits 3x on its second filing ever.
UNUSUAL_RATIO = 3.0
MIN_UNUSUAL_EVENTS = 3
# A baseline computed from too little history is not a baseline. Below this we do not
# claim to know the issuer's norm and emit nothing.
MIN_BASELINE_FOR_RATIO = 0.5

# Three institutions opening a *new* position in the same name. Adds are continuous and
# mostly mechanical; opens are discrete decisions, so three of them coinciding is a
# stronger claim than three adds and is held to the same count.
MIN_NEW_POSITION_FILERS = 3

# Evidence beyond the threshold is what separates a finding from a bare pass. K sets how
# fast strength climbs: at exactly the threshold a pattern scores 1/(1+K) of its kind
# weight, and it takes several extra participants to approach the ceiling. Deliberately
# slow -- at this sample size, one extra filer is not much extra evidence.
_SUPPORT_K = 3.0

# Per-kind priors: how much a pattern of this kind means when it does appear. These are
# the honest ordering of the detectors by how often they fire on noise (see module
# docstring reasoning); they multiply strength so a noisy kind must be much better
# supported than a clean one to reach the same rank.
KIND_WEIGHT: dict[str, float] = {
    "issuer_cluster": 1.0,  # independent people, one name: the hardest to fake
    "new_position_wave": 0.95,  # discrete decisions, but 13F data is stale by design
    "cross_jurisdiction": 0.85,  # rare, and rare things deserve attention
    "unusual_concentration": 0.8,  # only as good as the baseline handed in
    "filer_across_issuers": 0.6,  # often just a fund's quarterly reporting cadence
    "sector_concentration": 0.5,  # the loosest grouping, and the easiest coincidence
}

# Patterns below this never reach the model. A bare-threshold pass on a weak kind lands
# around 0.12-0.15, and those are exactly the "findings" a thesis should not be built on.
MIN_REPORTABLE_STRENGTH = 0.2

# A pattern's threshold belongs on the GROUP, not on each filing in it.
#
# Measured over 16 days of real filings: gating events at $5M each before detection left
# zero issuers with three distinct buyers, because a cluster is a count phenomenon and the
# gate is a dollar filter -- four insiders buying $2M apiece is a stronger signal than one
# buying $6M, and every one of those filings is individually invisible. Removing the gate
# instead surfaced "31 filers bought TSM totalling $423K", which is thirteen thousand
# dollars each and means nothing.
#
# So detection runs on ungated events and the group must clear the threshold in aggregate.
# On the same window this keeps GLOO (4 filers, $9.0M) and FSBC (6 filers, $6.3M) and
# drops TSM.
MIN_PATTERN_AGGREGATE_USD = 5_000_000.0

# What "bought" means. Sells, grants, exercises and on-chain movements are excluded from
# accumulation patterns: a cluster of compensation grants is a payroll calendar.
BUY_SIDE: frozenset[TransactionType] = frozenset(
    {
        TransactionType.OPEN_MARKET_BUY,
        TransactionType.ACTIVIST_13D,
        TransactionType.PASSIVE_13G,
        TransactionType.FUND_NEW_POSITION,
        TransactionType.FUND_ADD_POSITION,
    }
)


@dataclass(frozen=True)
class Pattern:
    """One countable regularity in a window of events, with its supporting rows attached.

    `summary` is plain language containing only figures computed from `events`, because
    it is fed to the provenance gate alongside `events` and any other number would be
    rejected. `strength` is for ranking only and is not a probability.
    """

    kind: str
    subject: str  # the issuer, sector, or filer the pattern is about
    summary: str
    events: list[NormalizedEvent]
    strength: float
    falsifier: str
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def event_ids(self) -> list[str]:
        return [e.event_id for e in self.events]


# -- keys and small helpers ---------------------------------------------------------


def issuer_key(event: NormalizedEvent) -> str:
    """Identity of the company. Falls back to name so unenriched rows still group."""
    return (event.issuer_id or event.issuer_name).strip().lower()


def filer_key(event: NormalizedEvent) -> str:
    return (event.filer_id or event.filer_name).strip().lower()


def _support_strength(support: int, threshold: int, kind: str) -> float:
    """Strength from evidence beyond the threshold, scaled by the kind's prior weight."""
    excess = max(0, support - threshold)
    return KIND_WEIGHT.get(kind, 0.5) * (excess + 1.0) / (excess + 1.0 + _SUPPORT_K)


def _total_usd(events: list[NormalizedEvent]) -> float | None:
    values = [e.effective_usd for e in events if e.effective_usd is not None]
    return sum(values) if values else None


def _usd_clause(events: list[NormalizedEvent]) -> str:
    """' totalling $X' when values are known, empty when they are not.

    Silence beats a zero here: an unpriced filing is not a $0 filing, and a summed total
    that quietly omits half the rows would be a figure nobody could reproduce.
    """
    total = _total_usd(events)
    if total is None:
        return ""
    priced = sum(1 for e in events if e.effective_usd is not None)
    if priced < len(events):
        return f" totalling at least {format_usd(total)} across the priced filings"
    return f" totalling {format_usd(total)}"


def _buy_side(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
    return [e for e in events if e.transaction_type in BUY_SIDE]


def _group(
    events: list[NormalizedEvent], key: Callable[[NormalizedEvent], str]
) -> dict[str, list[NormalizedEvent]]:
    out: dict[str, list[NormalizedEvent]] = defaultdict(list)
    for e in events:
        out[key(e)].append(e)
    return dict(out)


def _label_issuer(events: list[NormalizedEvent]) -> str:
    return events[0].issuer_name


# -- detectors ----------------------------------------------------------------------


def detect_issuer_clusters(events: list[NormalizedEvent]) -> list[Pattern]:
    """Several *distinct* filers buying into the same company in the window."""
    patterns: list[Pattern] = []
    for _, group in sorted(_group(_buy_side(events), issuer_key).items()):
        filers = {filer_key(e) for e in group}
        if len(filers) < MIN_CLUSTER_FILERS:
            continue
        name = _label_issuer(group)
        patterns.append(
            Pattern(
                kind="issuer_cluster",
                subject=name,
                summary=(
                    f"{len(filers)} distinct filers disclosed purchases in {name} "
                    f"in this window across {len(group)} filings{_usd_clause(group)}."
                ),
                events=list(group),
                strength=_support_strength(len(filers), MIN_CLUSTER_FILERS, "issuer_cluster"),
                falsifier=(
                    "A scheduled compensation or blackout window ending in the same days "
                    "would release several insiders to transact at once without any of "
                    "them forming a view."
                ),
                metrics={"distinct_filers": float(len(filers)), "filings": float(len(group))},
            )
        )
    return patterns


def detect_sector_concentration(events: list[NormalizedEvent]) -> list[Pattern]:
    """Distinct issuers within one sector bought in the window.

    Events with no known sector are skipped rather than pooled: the group would be
    "companies our enrichment does not cover", which is a fact about the vendor.
    """
    with_sector = [e for e in _buy_side(events) if e.context.sector]
    patterns: list[Pattern] = []
    for sector, group in sorted(_group(with_sector, lambda e: e.context.sector or "").items()):
        issuers = {issuer_key(e) for e in group}
        if len(issuers) < MIN_SECTOR_ISSUERS:
            continue
        filers = {filer_key(e) for e in group}
        patterns.append(
            Pattern(
                kind="sector_concentration",
                subject=sector,
                summary=(
                    f"{len(issuers)} distinct {sector} companies drew disclosed purchases "
                    f"in this window, from {len(filers)} filers{_usd_clause(group)}."
                ),
                events=list(group),
                strength=_support_strength(
                    len(issuers), MIN_SECTOR_ISSUERS, "sector_concentration"
                ),
                falsifier=(
                    "A sector with many listed companies produces more filings than a "
                    "small one at any level of interest, so the concentration may be a "
                    "count of listings rather than a rotation into the sector."
                ),
                metrics={
                    "distinct_issuers": float(len(issuers)),
                    "distinct_filers": float(len(filers)),
                },
            )
        )
    return patterns


def detect_filer_across_issuers(events: list[NormalizedEvent]) -> list[Pattern]:
    """One filer taking positions in several different companies at once."""
    patterns: list[Pattern] = []
    for _, group in sorted(_group(_buy_side(events), filer_key).items()):
        issuers = {issuer_key(e) for e in group}
        if len(issuers) < MIN_FILER_ISSUERS:
            continue
        name = group[0].filer_name
        patterns.append(
            Pattern(
                kind="filer_across_issuers",
                subject=name,
                summary=(
                    f"{name} disclosed positions in {len(issuers)} different companies "
                    f"in this window{_usd_clause(group)}."
                ),
                events=list(group),
                strength=_support_strength(
                    len(issuers), MIN_FILER_ISSUERS, "filer_across_issuers"
                ),
                falsifier=(
                    "A quarterly 13F reports an entire book on one date, so several names "
                    "appearing together may reflect the filing calendar rather than "
                    "simultaneous decisions."
                ),
                metrics={"distinct_issuers": float(len(issuers))},
            )
        )
    return patterns


def detect_cross_jurisdiction(events: list[NormalizedEvent]) -> list[Pattern]:
    """The same issuer surfacing in more than one country's disclosure regime."""
    patterns: list[Pattern] = []
    for _, group in sorted(_group(events, issuer_key).items()):
        jurisdictions = {e.jurisdiction.value for e in group}
        if len(jurisdictions) < MIN_JURISDICTIONS:
            continue
        name = _label_issuer(group)
        listed = ", ".join(sorted(jurisdictions))
        patterns.append(
            Pattern(
                kind="cross_jurisdiction",
                subject=name,
                summary=(
                    f"{name} appeared in the disclosures of {len(jurisdictions)} "
                    f"jurisdictions ({listed}) in this window across {len(group)} "
                    f"filings{_usd_clause(group)}."
                ),
                events=list(group),
                strength=_support_strength(
                    len(jurisdictions), MIN_JURISDICTIONS, "cross_jurisdiction"
                ),
                falsifier=(
                    "A dual-listed or cross-listed issuer can be required to disclose the "
                    "same underlying transaction in two regimes, so this may be one event "
                    "counted twice rather than two."
                ),
                metrics={"jurisdictions": float(len(jurisdictions))},
            )
        )
    return patterns


def detect_unusual_concentration(
    events: list[NormalizedEvent],
    baseline: Callable[[str], float | None],
) -> list[Pattern]:
    """Activity at an issuer well above its own trailing norm.

    `baseline` maps an issuer key to that issuer's typical filings-per-window. It is
    injected rather than queried so the thresholds stay testable and the function stays
    pure. Returning None means "no reliable history", and no pattern is emitted -- an
    issuer we have never seen is not thereby anomalous.
    """
    patterns: list[Pattern] = []
    for key, group in sorted(_group(_buy_side(events), issuer_key).items()):
        if len(group) < MIN_UNUSUAL_EVENTS:
            continue
        norm = baseline(key)
        if norm is None or norm < MIN_BASELINE_FOR_RATIO:
            continue
        ratio = len(group) / norm
        if ratio < UNUSUAL_RATIO:
            continue
        name = _label_issuer(group)
        filers = {filer_key(e) for e in group}
        # Support is expressed in filings above the norm, so an issuer that is 3x a busy
        # baseline outranks one that is 3x a quiet baseline.
        support = int(len(group) - norm)
        patterns.append(
            Pattern(
                kind="unusual_concentration",
                subject=name,
                summary=(
                    f"{name} drew {len(group)} disclosed purchases from {len(filers)} "
                    f"filers in this window{_usd_clause(group)}, against a trailing norm "
                    f"of about {norm:.1f} per window."
                ),
                events=list(group),
                strength=_support_strength(
                    support, MIN_UNUSUAL_EVENTS, "unusual_concentration"
                ),
                falsifier=(
                    "A single corporate event -- an index inclusion, an offering, or a "
                    "deal announcement -- mechanically raises filing counts at one issuer "
                    "without anyone forming an independent view."
                ),
                metrics={
                    "filings": float(len(group)),
                    "baseline": float(norm),
                    "ratio": ratio,
                },
            )
        )
    return patterns


def detect_new_position_wave(events: list[NormalizedEvent]) -> list[Pattern]:
    """Several institutions opening genuinely NEW positions in the same name.

    Only events where `is_new_position` is explicitly True count. Unknown is not new:
    the 13F snapshot endpoint cannot tell an open from an add, and treating its silence
    as an open would turn every crowded large cap into a wave.
    """
    opens = [e for e in _buy_side(events) if e.context.is_new_position is True]
    patterns: list[Pattern] = []
    for _, group in sorted(_group(opens, issuer_key).items()):
        filers = {filer_key(e) for e in group}
        if len(filers) < MIN_NEW_POSITION_FILERS:
            continue
        name = _label_issuer(group)
        patterns.append(
            Pattern(
                kind="new_position_wave",
                subject=name,
                summary=(
                    f"{len(filers)} institutions disclosed newly opened positions in "
                    f"{name} in this window{_usd_clause(group)}."
                ),
                events=list(group),
                strength=_support_strength(
                    len(filers), MIN_NEW_POSITION_FILERS, "new_position_wave"
                ),
                falsifier=(
                    "Institutional position data is reported with a quarter's lag and on a "
                    "common deadline, so simultaneous opens may be simultaneous reporting "
                    "of decisions taken weeks apart and independently."
                ),
                metrics={"distinct_filers": float(len(filers))},
            )
        )
    return patterns


# -- selection ----------------------------------------------------------------------


def aggregate_usd(pattern: Pattern) -> float:
    """Total disclosed value across a pattern's filings, counting only priced rows."""
    return sum(e.effective_usd for e in pattern.events if e.effective_usd is not None)


def filter_by_aggregate(
    patterns: list[Pattern], minimum: float = MIN_PATTERN_AGGREGATE_USD
) -> list[Pattern]:
    """Drop patterns whose filings do not add up to a meaningful sum.

    This is what makes ungated detection safe: the count test finds candidates, and this
    decides whether the money behind them is worth a reader's attention.
    """
    return [p for p in patterns if aggregate_usd(p) >= minimum]


def filter_weak(
    patterns: list[Pattern], min_strength: float = MIN_REPORTABLE_STRENGTH
) -> list[Pattern]:
    """Drop bare-threshold passes. What survives is what the model is allowed to see."""
    return [p for p in patterns if p.strength >= min_strength]


def rank_patterns(patterns: list[Pattern], top_n: int | None = None) -> list[Pattern]:
    """Strongest first. Ties break on supporting-event count, then kind, then subject.

    The tiebreak is spelled out because the output feeds a prompt: identical input must
    produce an identical ordering, or the same week rewrites itself between runs.
    """
    ordered = sorted(
        patterns,
        key=lambda p: (-p.strength, -len(p.events), p.kind, p.subject),
    )
    return ordered if top_n is None else ordered[:top_n]


def detect_patterns(
    events: list[NormalizedEvent],
    baseline: Callable[[str], float | None] | None = None,
    top_n: int | None = None,
    min_strength: float = MIN_REPORTABLE_STRENGTH,
    min_aggregate_usd: float = MIN_PATTERN_AGGREGATE_USD,
) -> list[Pattern]:
    """Run every detector over one window and return the ranked, filtered survivors.

    Pass the FULL plausible event set, not events already filtered by the $5M gate --
    see `MIN_PATTERN_AGGREGATE_USD` for why. Two filters then apply in order: the group
    must be worth something in aggregate, and it must clear the strength bar.

    With no `baseline` the concentration-versus-history detector is simply not run,
    rather than run against an assumed norm of zero.
    """
    if not events:
        return []
    found: list[Pattern] = []
    found.extend(detect_issuer_clusters(events))
    found.extend(detect_sector_concentration(events))
    found.extend(detect_filer_across_issuers(events))
    found.extend(detect_cross_jurisdiction(events))
    found.extend(detect_new_position_wave(events))
    if baseline is not None:
        found.extend(detect_unusual_concentration(events, baseline))
    surviving = filter_by_aggregate(found, min_aggregate_usd)
    return rank_patterns(filter_weak(surviving, min_strength), top_n)
