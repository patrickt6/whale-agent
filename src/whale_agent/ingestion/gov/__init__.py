"""Adapters for US government primary sources: free, keyless, redistributable.

These sit apart from `ingestion/vendors/` for a licensing reason, not a tidiness one.
Everything under `vendors/` is somebody's paid product held under a personal-use
licence; everything here is US federal public-domain data (17 U.S.C. 105) published by
the agency of record -- USASpending.gov for federal awards, the Senate Office of Public
Records for LDA lobbying filings. It can be stored, re-derived, and quoted in the digest
with no subscription and no redistribution question.

Attribution is still the honest thing to do, so every annotation names the agency it
came from in its `source`.
"""

from __future__ import annotations

from whale_agent.ingestion.gov.lobbying import SenateLdaAdapter
from whale_agent.ingestion.gov.usaspending import UsaSpendingAdapter

__all__ = ["SenateLdaAdapter", "UsaSpendingAdapter"]
