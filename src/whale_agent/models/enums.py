"""Enumerations: jurisdictions, transaction types, filer types, tiers, price sources."""

from __future__ import annotations

from enum import Enum


class Jurisdiction(str, Enum):
    US = "US"
    CANADA = "CA"
    UK = "UK"
    EU = "EU"
    TAIWAN = "TW"
    JAPAN = "JP"
    HONG_KONG = "HK"
    KOREA = "KR"
    INDIA = "IN"
    AUSTRALIA = "AU"
    CHINA = "CN"
    BRAZIL = "BR"
    # On-chain activity has no jurisdiction. It gets its own bucket rather than being
    # forced into a country's, so the digest can group and caveat it separately.
    CRYPTO = "CRYPTO"


class TransactionType(str, Enum):
    """Distinct event types. Never collapse these."""

    OPEN_MARKET_BUY = "open_market_buy"
    OPEN_MARKET_SELL = "open_market_sell"
    GRANT = "grant"  # compensation grant / award
    OPTION_EXERCISE = "option_exercise"
    SCHEDULED_SALE = "scheduled_sale"  # 10b5-1 plan sale
    ACTIVIST_13D = "activist_13d"
    PASSIVE_13G = "passive_13g"
    FUND_NEW_POSITION = "fund_new_position"
    FUND_ADD_POSITION = "fund_add_position"
    # A labelled on-chain movement. Not a disclosed position: nobody filed anything,
    # and the "owner" is a heuristic wallet label, so it scores well below a filing.
    CRYPTO_TRANSFER = "crypto_transfer"
    OTHER = "other"


# Form 4 transaction codes -> our taxonomy.
FORM4_CODE_MAP: dict[str, TransactionType] = {
    "P": TransactionType.OPEN_MARKET_BUY,
    "S": TransactionType.OPEN_MARKET_SELL,
    "A": TransactionType.GRANT,
    "M": TransactionType.OPTION_EXERCISE,
    "F": TransactionType.OTHER,  # tax withholding
    "G": TransactionType.OTHER,  # gift
}


class FilerType(str, Enum):
    INSIDER = "insider"
    FUND = "fund"
    INDIVIDUAL = "individual"
    SWF = "swf"  # sovereign wealth fund
    FAMILY_OFFICE = "family_office"
    UNKNOWN = "unknown"


class Tier(str, Enum):
    """Delivery tiers."""

    INSTANT = "tier1_instant"
    NOTABLE = "tier2_notable"
    WEEKLY = "tier3_weekly"
    BELOW = "below_threshold"


class PriceSource(str, Enum):
    """Provenance of the price used to value a share-count filing."""

    FILING_STATED = "filing_stated"  # transaction price in the filing itself
    CLOSE_ON_DATE = "close_on_date"  # close on transaction/disclosure date
    MOST_RECENT_CLOSE = "most_recent_close"
    NOT_PRICED = "not_priced"
