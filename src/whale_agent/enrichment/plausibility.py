"""Reject figures that are arithmetically valid and obviously false.

The provenance gate proves a number came from a filing field. It cannot prove the field
held what the vendor claimed. Those are different guarantees, and sixteen days of real
data showed why the second one is needed:

    MetLife / FINS    shares=40,000,000  price=40,000,000  ->  $1.6 quadrillion
    Sutherland / PSX  shares=3,523       price=2,110,482   ->  $7.4 billion

FMP's `price` field sometimes carries the transaction's *total value*, or a duplicate of
the share count, rather than a per-share price. Multiplying shares by that produces a
figure every downstream check happily accepts: it traces to a filing field, the tokenizer
recognises it, the gate passes it. It is also nonsense, and a nonsense figure in a product
whose entire claim is careful arithmetic is worse than no figure at all.

So this is a second, independent test: not "where did this come from" but "could this
possibly be true". The strongest check is the cheapest -- a position cannot be worth more
than the entire company -- and it only became available once market-cap enrichment landed.

Implausible events are marked and excluded from the digest rather than deleted. A silent
drop is indistinguishable from a source outage, and the count of rejected rows is itself a
signal worth watching: if it moves, a vendor changed something.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from whale_agent.models.event import NormalizedEvent

# No single disclosed position in this dataset should approach this. The largest real
# 13D stakes run to tens of billions; a quarter-trillion-dollar entry is a data error, and
# treating it as a hard ceiling costs us nothing we would want to publish.
ABSOLUTE_CEILING_USD = 250_000_000_000.0

# A position worth more than the whole company is impossible rather than unlikely. The
# margin allows for a stale market cap and for genuine cases near 100% (a take-private),
# without admitting the order-of-magnitude errors this exists to catch.
MARKET_CAP_TOLERANCE = 1.5

# Per-share prices outside this band are not prices. US listings that trade below a cent
# are vanishingly rare. The upper bound sits above Berkshire A, which trades near
# $700,000 and is the most expensive listed share there is: an earlier $10,000 ceiling
# was written with Berkshire in mind and was an order of magnitude too low to admit it,
# which silently dropped the most recognisable holding in the dataset.
#
# A million still catches both observed incidents by a wide margin, FMP price fields of
# 40,000,000 and 2,110,482, so nothing is given up to gain the true negative.
MIN_PLAUSIBLE_SHARE_PRICE = 0.01
MAX_PLAUSIBLE_SHARE_PRICE = 1_000_000.0

# The ceiling that applies when market-cap enrichment did not run or returned nothing.
# Less evidence has to mean a lower ceiling, not the same one. Without a market cap the
# decisive check is unavailable, and the remaining checks are the crude ones, so the
# absolute ceiling on its own is the only thing standing between a vendor error and the
# digest. Real disclosures above $25B exist but are rare enough, and important enough,
# that routing them through the quarantine queue for a human look is the right trade.
UNVERIFIED_CEILING_USD = 25_000_000_000.0

# No filer holds more shares than the issuer has issued. A margin is allowed for a stale
# share count and for genuine near-100% cases, matching MARKET_CAP_TOLERANCE.
SHARES_OUTSTANDING_TOLERANCE = 1.5

# shares x price and a vendor-stated dollar value should agree. When they disagree by
# more than this they are not describing the same transaction, and at least one of them
# is wrong. This is the cross-check field pattern: emit both, then compare in code.
MAX_VALUE_DISCREPANCY_RATIO = 10.0

# Values a vendor emits where a number belongs. The int64 extremes are what a database
# column serves when it means null, and -1 is the hand-rolled version of the same idea.
# The magnitude test is deliberately loose rather than an exact match: 2**63 - 1 does not
# survive a round trip through a float, so an equality check against the sentinel misses
# the very value it was written for.
INT64_SENTINELS = {-1}
SENTINEL_MAGNITUDE = float(2**62)

# Strings vendors emit where a company name belongs. Uppercased for comparison.
PLACEHOLDER_ISSUER_NAMES = {"", "NONE", "NULL", "N/A", "UNKNOWN", "-", "--"}


@dataclass(frozen=True)
class Verdict:
    """Whether an event's figures can be believed, and why not if they cannot."""

    plausible: bool
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.plausible


def _is_finite(value: float) -> bool:
    """False for NaN and for both infinities.

    A comparison against NaN is False whichever way it is written, so a bounds check
    written the obvious way silently passes a NaN through. It has to be excluded first.
    """
    return math.isfinite(value)


def _is_sentinel(value: float) -> bool:
    """True for the values vendors emit to mean "no value" rather than a quantity."""
    return value in INT64_SENTINELS or abs(value) >= SENTINEL_MAGNITUDE


def _discrepancy_reasons(event: NormalizedEvent) -> list[str]:
    """Compare the two independent routes to a dollar figure and report disagreement.

    `shares x price x fx` and a stated dollar value are usually the same arithmetic, in
    which case this is silent. They come apart exactly when one input is wrong: a
    filing-stated total sitting next to a looked-up price, or the vendor's price field
    holding something that is not a price. Disagreement does not say which one is wrong,
    only that the pair cannot both be believed.
    """
    stated = event.usd_value
    shares, price = event.share_count, event.price_used
    if stated is None or shares is None or price is None:
        return []
    if not all(_is_finite(v) for v in (stated, shares, price)):
        return []
    fx = event.fx_rate_used if event.fx_rate_used and _is_finite(event.fx_rate_used) else 1.0
    computed = shares * price * fx
    if computed <= 0 or stated <= 0:
        return []
    ratio = max(computed, stated) / min(computed, stated)
    if ratio <= MAX_VALUE_DISCREPANCY_RATIO:
        return []
    return [
        f"shares x price gives {computed:,.0f} but the stated value is {stated:,.0f}, "
        f"a factor of {ratio:,.0f}; the two cannot both be right"
    ]


def check_event(event: NormalizedEvent) -> Verdict:
    """Test one event's figures for internal contradiction. No network, no clock."""
    reasons: list[str] = []

    if event.share_count is not None:
        if not _is_finite(event.share_count):
            reasons.append("share count is not a finite number")
        elif _is_sentinel(event.share_count):
            reasons.append("share count is a vendor sentinel value, not a count")
        elif event.share_count <= 0:
            reasons.append("share count is zero or negative")

    if event.price_used is not None:
        if not _is_finite(event.price_used):
            reasons.append("per-share price is not a finite number")
        elif _is_sentinel(event.price_used):
            reasons.append("per-share price is a vendor sentinel value, not a price")
        elif event.price_used <= 0:
            reasons.append("per-share price is zero or negative")
        elif event.price_used > MAX_PLAUSIBLE_SHARE_PRICE:
            # The observed failure mode: a transaction total parked in the price field.
            reasons.append(
                f"per-share price of {event.price_used:,.0f} is implausible; the field "
                "most likely holds a transaction total rather than a unit price"
            )
        elif event.price_used < MIN_PLAUSIBLE_SHARE_PRICE:
            reasons.append("per-share price is below any real listing")

    value = event.effective_usd
    if value is not None:
        if not _is_finite(value):
            reasons.append("position value is not a finite number")
        elif value <= 0:
            reasons.append("position value is zero or negative")
        elif value > ABSOLUTE_CEILING_USD:
            reasons.append(f"position value of {value:,.0f} exceeds any real disclosure")

        cap = event.context.market_cap_usd
        if cap is not None and _is_finite(cap) and cap > 0:
            # The decisive check, and the one that needs enrichment to exist.
            if value > cap * MARKET_CAP_TOLERANCE:
                reasons.append(
                    f"position value of {value:,.0f} exceeds the company's market "
                    f"capitalisation of {cap:,.0f}"
                )
        elif value > UNVERIFIED_CEILING_USD:
            # No market cap means the decisive check could not run. Tighten rather than
            # loosen: an unverifiable figure this large is quarantined for review, not
            # published on the strength of the checks that happen to remain.
            reasons.append(
                f"position value of {value:,.0f} exceeds the unverified ceiling of "
                f"{UNVERIFIED_CEILING_USD:,.0f}; no market capitalisation was available "
                "to check it against"
            )

    # A second cap-free consistency test: nobody holds more shares than were issued.
    # This catches the duplicated-share-count failure on percentage-threshold filings,
    # where there is often no dollar value and no market cap to compare against.
    shares_out = event.shares_outstanding
    if (
        event.share_count is not None
        and shares_out is not None
        and _is_finite(shares_out)
        and shares_out > 0
        and event.share_count > shares_out * SHARES_OUTSTANDING_TOLERANCE
    ):
        reasons.append(
            f"share count of {event.share_count:,.0f} exceeds the {shares_out:,.0f} "
            "shares outstanding"
        )

    # Cross-check the two independent routes to a dollar figure against each other.
    reasons.extend(_discrepancy_reasons(event))

    # A placeholder where an issuer name belongs means the row was never populated.
    if is_placeholder_issuer(event.issuer_name):
        reasons.append("issuer name is a placeholder")

    return Verdict(not reasons, tuple(reasons))


def is_placeholder_issuer(name: str | None) -> bool:
    """True when an issuer name carries no information.

    Shared rather than inlined because the same string that fails this check here also
    has to be kept out of every reader-facing ranking: a vendor null once reached the
    weekly report as an issuer literally named "NONE", credited with $20.2M.
    """
    return (name or "").strip().upper() in PLACEHOLDER_ISSUER_NAMES


def screen(
    events: list[NormalizedEvent],
    *,
    sink: Any = None,
) -> tuple[list[NormalizedEvent], list[tuple[NormalizedEvent, Verdict]]]:
    """Split events into believable and rejected, keeping the reason for each rejection.

    Returns `(kept, rejected)`. Callers should log the rejected count: a sudden change in
    it is how a vendor schema change announces itself, and is far more informative than
    the rows quietly disappearing.

    `sink` is an optional `monitoring.quarantine.QuarantineLog`. Passing one writes every
    rejection to the durable quarantine record; omitting it keeps this function pure, no
    clock and no file handle, which is what the rest of the module promises and what lets
    it be tested without a filesystem. The default is deliberately not the process-wide
    log: a screening call inside a unit test should not append to the operator's record.
    """
    kept: list[NormalizedEvent] = []
    rejected: list[tuple[NormalizedEvent, Verdict]] = []
    for event in events:
        verdict = check_event(event)
        if verdict.plausible:
            kept.append(event)
            continue
        rejected.append((event, verdict))
        if sink is not None:
            sink.record_event("plausibility", event, verdict.reasons)
    return kept, rejected
