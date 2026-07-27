"""Deterministic composite scoring, the $5M gate, and tiering.

Ranking is fully deterministic in code. The rule that matters: never reorder by dollar
value alone. A $6M open-market buy that is 4% of a $200M company must be able to beat a
$200M index rebalance.

What changed, and why. The first version multiplied by *fame*: recognisable filers scored
up to 2.0x. That produced a digest of names the reader could have listed unaided, and
-- worse
-- promoted Vanguard and BlackRock, whose 13Gs are the least informative filings we
ingest. A digest that leads with an index fund has ranked a fund-flow mechanic above
somebody's decision.

The model now ranks on what can be measured about the *situation*, in rough order of how
much each term is trusted:

  1. what kind of event it is (a buy is not a grant, a 13D is not a 13G);
  2. how big it is relative to the company -- percent of float first, market cap second,
     because a $6M buy is a real bet in a $200M issuer and rounding error in Apple;
  3. how big it is in dollars, log-scaled so $200M cannot simply outrank everything;
  4. whether this filer is unfamiliar to us, and whether this filing is unusual *for
     them* -- both measured from our own history, never looked up;
  5. reputation, as a small tiebreaker in [0.9, 1.25], and only where involvement changes
     the situation (activists up, index managers down).

The evidence behind (2) and (4) is Cohen-Malloy-Pomorski (2012): insider signal
concentrated in small caps and in opportunistic, non-routine trades. Neither of those is
a property of the filer's fame.

Every context-derived term degrades to a neutral 1.0 when its input is None. This is not
politeness -- most non-US events have no FMP coverage at all, and a model that penalised
missing data would rank the entire Taiwan and Japan feed last for being foreign.
"""

from __future__ import annotations

import math
from datetime import date

from whale_agent.config import Settings, get_settings
from whale_agent.enrichment.entity_resolution import notability_for
from whale_agent.models.context import EventContext
from whale_agent.models.entity import is_passive_manager
from whale_agent.models.enums import Tier, TransactionType
from whale_agent.models.event import NormalizedEvent

# base_type_weight
BASE_TYPE_WEIGHT: dict[TransactionType, float] = {
    TransactionType.OPEN_MARKET_BUY: 1.0,
    TransactionType.ACTIVIST_13D: 0.9,
    TransactionType.FUND_NEW_POSITION: 0.7,
    TransactionType.PASSIVE_13G: 0.5,
    TransactionType.FUND_ADD_POSITION: 0.4,
    TransactionType.OPTION_EXERCISE: 0.2,
    TransactionType.GRANT: 0.2,
    TransactionType.OPEN_MARKET_SELL: 0.15,
    TransactionType.SCHEDULED_SALE: 0.1,
    TransactionType.OTHER: 0.1,
}

CLUSTER_BONUS_PER_EXTRA = 0.25  # per additional buyer beyond the first
FIRST_TIME_BONUS = 1.15  # was 1.3; novelty is now mostly carried by the measured history
ROUTINE_PENALTY = 0.7  # multiplicative discount (not subtraction) to stay well-behaved

# Market-cap bands. A dollar of insider conviction buys more information in a small
# company: it is a larger share of the float, and fewer analysts already knew.
SMALL_CAP_CEILING_USD = 2_000_000_000.0
_MARKET_CAP_BANDS: list[tuple[float, float]] = [
    (300_000_000.0, 1.50),  # micro cap: the band where the insider evidence is strongest
    (2_000_000_000.0, 1.30),  # small cap
    (10_000_000_000.0, 1.10),  # mid cap: mildly interesting
    (100_000_000_000.0, 1.00),  # large cap: baseline
]
MEGA_CAP_MULTIPLIER = 0.85  # $100B+: a $10M position is noise in a company this size

# Percent-of-float caps out at 2.0x -- a 5%+ stake is already a control-adjacent story
# and does not need further amplification to reach the top of a digest.
PERCENT_OF_FLOAT_FULL_CREDIT = 5.0
PERCENT_OF_FLOAT_MAX_BONUS = 1.0

# Passive 13G discount. Applied on top of the registry's below-baseline multiplier,
# because the *combination* -- index manager plus passive form -- is what makes the event
# mechanical. The same manager filing a 13D would be genuinely surprising.
PASSIVE_MECHANICAL_TYPES = frozenset(
    {TransactionType.PASSIVE_13G, TransactionType.FUND_ADD_POSITION}
)
PASSIVE_MECHANICAL_DISCOUNT = 0.5

# Unfamiliarity bands, keyed on how many prior filings we hold. The twentieth filing from
# a name is a subscription; the first is a decision.
_FAMILIARITY_BANDS: list[tuple[int, float]] = [
    (0, 1.35),  # never recorded before
    (2, 1.20),  # seen once or twice
    (9, 1.05),  # occasional
    (24, 0.95),  # regular
]
FAMILIAR_FILER_MULTIPLIER = 0.85  # 25+ prior filings: we hear from them constantly

# Unusual-for-this-filer. Capped jointly so a filer cannot win on novelty alone.
GAP_18M_DAYS = 548
GAP_12M_DAYS = 365
GAP_18M_MULTIPLIER = 1.30  # first appearance in 18 months: they chose to come back
GAP_12M_MULTIPLIER = 1.15
SIZE_VS_TYPICAL_LARGE = 5.0  # 5x their own median: a different kind of bet for them
SIZE_VS_TYPICAL_LARGE_MULTIPLIER = 1.30
SIZE_VS_TYPICAL_ABOVE = 2.0
SIZE_VS_TYPICAL_ABOVE_MULTIPLIER = 1.15
SIZE_VS_TYPICAL_SMALL = 0.5  # half their usual: likelier housekeeping than conviction
SIZE_VS_TYPICAL_SMALL_MULTIPLIER = 0.95
UNUSUAL_MAX_MULTIPLIER = 1.6

NEW_POSITION_MULTIPLIER = 1.15  # opening a position states a view; adding to one repeats it
EXISTING_POSITION_MULTIPLIER = 0.95


def passes_gate(event: NormalizedEvent, settings: Settings | None = None) -> bool:
    """The $5M gate. True if the event clears the threshold."""
    s = settings or get_settings()
    val = event.effective_usd
    if val is None:
        return False
    if val >= s.threshold_usd:
        return True
    # Percentage-threshold crossing with implied value clearing the bar.
    return bool(
        event.percentage_threshold_crossed
        and (event.implied_usd_value or 0) >= s.threshold_usd
    )


def is_near_threshold(event: NormalizedEvent, settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    val = event.effective_usd
    if val is None:
        return False
    return s.near_threshold_usd <= val < s.threshold_usd


def _conviction_multiplier(event: NormalizedEvent) -> float:
    """Log-scaled position value, optionally relative to filer size.

    Uses log10 of USD value so a $5M and a $200M event differ by a bounded factor
    rather than 40x, keeping type/size/history signals meaningful.
    """
    val = event.effective_usd or 0.0
    if val <= 0:
        return 0.1
    base = math.log10(val) - 6.0  # 0 at $1M, 1 at $10M, ~2.3 at $200M
    mult = max(0.2, 0.5 + base)
    # Conviction relative to filer size: a big slice of a small book ranks up.
    raw = event.raw_payload or {}
    filer_size = raw.get("filer_size_usd")
    if filer_size:
        frac = val / float(filer_size)
        mult *= 1.0 + min(2.0, frac * 5.0)
    return mult


def _company_size_multiplier(context: EventContext) -> float:
    """Small-cap tilt. Neutral -- never a penalty -- when market cap is unknown.

    Unknown is the common case outside the US, and an unenriched Taiwanese filing has not
    been shown to be large; it has only failed to be looked up.
    """
    cap = context.market_cap_usd
    if cap is None or cap <= 0:
        return 1.0
    for ceiling, mult in _MARKET_CAP_BANDS:
        if cap < ceiling:
            return mult
    return MEGA_CAP_MULTIPLIER


def _position_size_multiplier(context: EventContext) -> float:
    """Position size measured against the company rather than against other filings.

    Percent of float is the honest denominator and the better conviction measure: it says
    what fraction of the buyable company someone just took.
    """
    pct = context.percent_of_float
    if pct is None or pct <= 0:
        return 1.0
    return 1.0 + PERCENT_OF_FLOAT_MAX_BONUS * min(1.0, pct / PERCENT_OF_FLOAT_FULL_CREDIT)


def _familiarity_multiplier(context: EventContext) -> float:
    """Unfamiliar filers rank up. Measured from our own store, neutral when unmeasured."""
    prior = context.filer_prior_filings
    if prior is None:
        return 1.0  # cold store or unenriched: no claim either way
    for ceiling, mult in _FAMILIARITY_BANDS:
        if prior <= ceiling:
            return mult
    return FAMILIAR_FILER_MULTIPLIER


def _unusual_for_filer_multiplier(event: NormalizedEvent) -> float:
    """Departure from this filer's own pattern -- by gap since last seen, and by size.

    Both halves apply, since a filer who resurfaces after two years *and* does so at five
    times their usual size has broken their pattern twice. The joint cap keeps that from
    running away.
    """
    context = event.context
    mult = 1.0

    gap = context.filer_days_since_last
    if gap is not None:
        if gap >= GAP_18M_DAYS:
            mult *= GAP_18M_MULTIPLIER
        elif gap >= GAP_12M_DAYS:
            mult *= GAP_12M_MULTIPLIER

    typical = context.filer_typical_usd
    value = event.effective_usd
    if typical and typical > 0 and value:
        ratio = value / typical
        if ratio >= SIZE_VS_TYPICAL_LARGE:
            mult *= SIZE_VS_TYPICAL_LARGE_MULTIPLIER
        elif ratio >= SIZE_VS_TYPICAL_ABOVE:
            mult *= SIZE_VS_TYPICAL_ABOVE_MULTIPLIER
        elif ratio <= SIZE_VS_TYPICAL_SMALL:
            mult *= SIZE_VS_TYPICAL_SMALL_MULTIPLIER

    return min(UNUSUAL_MAX_MULTIPLIER, mult)


def _new_position_multiplier(context: EventContext) -> float:
    """Opening a position is a statement; adding to one is a continuation."""
    if context.is_new_position is None:
        return 1.0
    return NEW_POSITION_MULTIPLIER if context.is_new_position else EXISTING_POSITION_MULTIPLIER


def _reputation_multiplier(event: NormalizedEvent) -> float:
    """Filer reputation as a tiebreaker, plus the mechanical-filing discount.

    `notability_for` now spans [0.9, 1.25]. The extra discount fires only when a passive
    manager files a passive form: that pairing is the fund-flow mechanic, and it is the
    one thing in the dataset that should rank below an ordinary unknown filer.
    """
    mult = notability_for(event)
    if (
        is_passive_manager(event.filer_name)
        and event.transaction_type in PASSIVE_MECHANICAL_TYPES
    ):
        mult *= PASSIVE_MECHANICAL_DISCOUNT
    return mult


def _recency_multiplier(event: NormalizedEvent, today: date | None = None) -> float:
    """Fresh disclosures rank slightly higher; decays with filing lag."""
    ref = today or date.today()
    lag_days = max(0, (ref - event.disclosure_date).days)
    return 1.0 / (1.0 + lag_days / 30.0)


def _cluster_multiplier(event: NormalizedEvent) -> float:
    """Several independent buyers in one name beats one buyer.

    Takes the larger of the insider cluster and the cohort count so the signal survives
    whichever of the two enrichment stages managed to run.
    """
    buyers = max(event.cluster_size, event.context.distinct_buyers_this_period or 0)
    return 1.0 + CLUSTER_BONUS_PER_EXTRA * max(0, buyers - 1)


def _first_time_multiplier(event: NormalizedEvent) -> float:
    """The debut bonus, which must be corroborated by measured history to apply.

    This is the second half of the cold-start fix. `is_first_time_filer` is set upstream
    by set-membership against the store, which on a fresh database says yes to everyone --
    inflating every score and producing an opening digest in which all thirty names are
    debuts. The flag alone is therefore not enough: the bonus fires only when the filer
    history enrichment ran, found the store warm, and independently reported zero priors.

    Both failure modes collapse to neutral. A cold store leaves `filer_prior_filings`
    None, so nothing is claimed; a store that has seen the filer reports a positive count,
    which overrides the flag outright.
    """
    if not event.is_first_time_filer:
        return 1.0
    if event.context.filer_prior_filings != 0:
        return 1.0
    return FIRST_TIME_BONUS


def compute_score(event: NormalizedEvent, today: date | None = None) -> float:
    context = event.context
    score = BASE_TYPE_WEIGHT.get(event.transaction_type, 0.1)
    score *= _conviction_multiplier(event)
    score *= _company_size_multiplier(context)
    score *= _position_size_multiplier(context)
    score *= _familiarity_multiplier(context)
    score *= _unusual_for_filer_multiplier(event)
    score *= _new_position_multiplier(context)
    score *= _reputation_multiplier(event)
    score *= _recency_multiplier(event, today)
    score *= _cluster_multiplier(event)
    score *= _first_time_multiplier(event)
    if event.is_routine:
        score *= ROUTINE_PENALTY
    return score


def assign_tier(event: NormalizedEvent, settings: Settings | None = None) -> Tier:
    """Tier assignment.

    Tier 1 (instant): a single huge event -- >$100M implied, OR a first-time activist
    13D, OR a cluster buy of 3+ insiders. Tier 2: anything else clearing the gate.
    Below-gate events are tagged BELOW.
    """
    if not passes_gate(event, settings):
        return Tier.BELOW
    val = event.effective_usd or 0.0
    huge = val >= 100_000_000
    first_time_activist = (
        event.transaction_type == TransactionType.ACTIVIST_13D and event.is_first_time_filer
    )
    big_cluster = (
        event.transaction_type == TransactionType.OPEN_MARKET_BUY and event.cluster_size >= 3
    )
    if huge or first_time_activist or big_cluster:
        return Tier.INSTANT
    return Tier.NOTABLE


def score_event(
    event: NormalizedEvent,
    settings: Settings | None = None,
    today: date | None = None,
) -> NormalizedEvent:
    """Populate score + tier on the event, return it."""
    event.score = compute_score(event, today)
    event.tier = assign_tier(event, settings)
    return event


def rank_events(
    events: list[NormalizedEvent],
    settings: Settings | None = None,
    today: date | None = None,
) -> list[NormalizedEvent]:
    """Score every event and return those clearing the gate, highest score first."""
    scored = [score_event(e, settings, today) for e in events]
    passing = [e for e in scored if e.tier != Tier.BELOW]
    passing.sort(key=lambda e: e.score or 0.0, reverse=True)
    return passing
