"""Bounded coercion of vendor numeric fields, applied where the payload is still in scope.

`float(row["price"])` is a claim that whatever the vendor put in that field is a
per-share price. FMP's insider feed sometimes puts the transaction total there, or a
duplicate of the share count, and the resulting figure is arithmetically perfect and
completely false. Detecting it two hundred lines downstream works, but by then the
payload that would explain it is gone and the log line can only say that a number was
large.

So the check moves to the boundary. These helpers return a value *or* a reason, never a
substituted number: a clamped price is still a false claim, just a less obvious one. A
rejected field becomes `None`, which the whole codebase already reads as "not learned"
rather than as zero, and the reason is recorded on the event so the drop is visible
rather than silent.

Nothing here raises. One unbelievable row must not cost a source its whole batch -- a
failing source becomes a coverage note, and a failing *field* should cost less than that.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from whale_agent.enrichment.plausibility import (
    MAX_PLAUSIBLE_SHARE_PRICE,
    MIN_PLAUSIBLE_SHARE_PRICE,
)

log = logging.getLogger(__name__)

# Values that mean "no value" in a system that stored one in a fixed-width integer.
# They arrive as real numbers and would otherwise be multiplied like real numbers.
INT64_SENTINELS = {2**63 - 1, -(2**63), -1}

# More shares than any issuer has ever had outstanding. Not a plausibility judgement
# about a position, which `plausibility.py` makes with the market cap in hand; only a
# floor under "this field does not hold a share count at all".
MAX_SHARE_COUNT = 1e13

# Where the reasons live on the event. Chosen over a new model field because it is
# free-form provenance about one vendor row, and it persists with the payload.
REJECTIONS_KEY = "_vendor_rejections"


@dataclass(frozen=True)
class VendorValue:
    """A coerced vendor field: a believable number, or a reason there is none.

    `value is None` covers both "the vendor sent nothing" and "the vendor sent something
    we will not use". Only the second carries a `rejection`, so an absent field stays
    ordinary and a rejected one stays visible.
    """

    value: float | None
    rejection: str | None = None

    def record_on(self, event: Any) -> Any:
        """Append this rejection, if any, to the event's raw payload. Returns the event."""
        if self.rejection:
            event.raw_payload.setdefault(REJECTIONS_KEY, []).append(self.rejection)
        return event


def _finite(raw: Any) -> float | None:
    """Plain numeric coercion. None for anything that is not a finite number."""
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip().replace(",", "").replace("$", "")
    if text in ("", "N/A", "None", "null", "-"):
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _reject(vendor: str, field: str, raw: Any, why: str, ctx: dict | None) -> VendorValue:
    reason = f"{vendor}.{field}={raw!r} rejected: {why}"
    # The payload is logged here and only here, because here is where it still exists.
    log.warning("%s | row=%s", reason, ctx)
    return VendorValue(None, reason)


def coerce_price_per_share(
    raw: Any, *, vendor: str, field: str = "price", ctx: dict | None = None
) -> VendorValue:
    """A per-share price, or a reason the field did not hold one.

    The upper bound is the decisive one. A five-figure share price is Berkshire A and
    almost nothing else, so a larger number is far more likely to be a transaction
    total -- which is exactly the observed failure: shares=40,000,000 and
    price=40,000,000 on the same row, giving $1.6 quadrillion.
    """
    value = _finite(raw)
    if value is None:
        return VendorValue(None)
    if int(value) in INT64_SENTINELS:
        return _reject(vendor, field, raw, "integer sentinel, not a price", ctx)
    if value <= 0:
        return _reject(vendor, field, raw, "a price is not zero or negative", ctx)
    if value < MIN_PLAUSIBLE_SHARE_PRICE:
        return _reject(vendor, field, raw, "below any real listing", ctx)
    if value > MAX_PLAUSIBLE_SHARE_PRICE:
        return _reject(
            vendor,
            field,
            raw,
            f"above the {MAX_PLAUSIBLE_SHARE_PRICE:,.0f} per-share ceiling; the field "
            "most likely holds a transaction total rather than a unit price",
            ctx,
        )
    return VendorValue(value)


def coerce_share_count(
    raw: Any, *, vendor: str, field: str = "shares", ctx: dict | None = None
) -> VendorValue:
    """A share count, or a reason the field did not hold one. Zero is rejected, not kept:
    a transaction of no shares is not a transaction."""
    value = _finite(raw)
    if value is None:
        return VendorValue(None)
    if int(value) in INT64_SENTINELS:
        return _reject(vendor, field, raw, "integer sentinel, not a share count", ctx)
    if value <= 0:
        return _reject(vendor, field, raw, "share count is zero or negative", ctx)
    if value > MAX_SHARE_COUNT:
        return _reject(
            vendor,
            field,
            raw,
            f"above the {MAX_SHARE_COUNT:,.0f} ceiling for any issuer's shares",
            ctx,
        )
    return VendorValue(value)


def coerce_usd_amount(
    raw: Any, *, vendor: str, field: str = "value", ctx: dict | None = None
) -> VendorValue:
    """A stated dollar amount, or a reason the field did not hold one.

    Deliberately looser than the price bound: a 13F holding really can be worth tens of
    billions. Only the ceiling that `plausibility.py` calls absolute applies, and it is
    re-checked downstream once the market cap is known.
    """
    from whale_agent.enrichment.plausibility import ABSOLUTE_CEILING_USD

    value = _finite(raw)
    if value is None:
        return VendorValue(None)
    if int(value) in INT64_SENTINELS:
        return _reject(vendor, field, raw, "integer sentinel, not an amount", ctx)
    if value > ABSOLUTE_CEILING_USD:
        return _reject(
            vendor,
            field,
            raw,
            f"exceeds the {ABSOLUTE_CEILING_USD:,.0f} ceiling for any real disclosure",
            ctx,
        )
    return VendorValue(value)
