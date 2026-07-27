"""`ContextAnnotation`: slow background signal that is not a whale event.

Corporate lobbying spend and federal contract awards are quarterly, company-level, and
have no filer taking a position -- forcing them through `NormalizedEvent` and the $5M
gate would either flood the daily digest with noise or drop them entirely. They are
context: material for the weekly report's "what else is going on at these names"
section, never a Tier-1 alert.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class ContextAnnotation(BaseModel):
    """A dated, company-level fact used to colour the weekly report."""

    source: str
    kind: str  # "lobbying" | "gov_contract" | ...
    ticker: str | None = None
    issuer_name: str | None = None
    as_of: date
    amount_usd: float | None = None
    label: str = ""  # short human-readable description
    source_url: str | None = None
    raw_payload: dict = Field(default_factory=dict)
