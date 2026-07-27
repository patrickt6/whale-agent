"""`Thesis`: one article-length argument built from filings.

The shared contract between the layer that *writes* a thesis and the layer that
*renders* it. Kept in models/ so neither has to import the other.

The field list is the argument's skeleton, and it is deliberately not free-form prose:
a research note that states a claim without stating what would disprove it is a press
release. Every thesis must carry its own counter-evidence and its own falsifier, and the
renderer shows both. If a section cannot be filled honestly, it stays empty and the piece
says so -- that is a better outcome than inventing a counter-argument to look balanced.

`citations` is what keeps this checkable. Every figure appearing anywhere in the thesis
must resolve to a filing, a vendor field, or a government dataset, and the provenance
gate runs over the assembled text before it is published.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """A resolvable pointer behind one claim.

    `url` is what a reader clicks. `detail` is how someone would re-derive the figure
    without trusting us -- an accession number, an endpoint and its parameters, a series
    ID. A citation without one of the two is not a citation.
    """

    label: str
    url: str | None = None
    detail: str | None = None
    source: str = ""  # "sec", "fmp", "usaspending", "lda", "fred", ...


class Thesis(BaseModel):
    """One article. Sections map to the skeleton a credible research note follows."""

    # Identity
    slug: str  # url-safe, stable for the life of the article
    title: str
    published_on: date
    kind: str = "pattern"  # "pattern" | "single_event" | "context"

    # 1. The claim, stated so it can be wrong.
    lede: str
    claim: str

    # 2-3. What triggered it, and the pattern quantified against a baseline.
    trigger_summary: str = ""
    evidence: list[str] = Field(default_factory=list)

    # 4. Why now -- macro, policy, or contract context.
    context_notes: list[str] = Field(default_factory=list)

    # 5-6. The parts that make it research rather than promotion.
    counter_evidence: list[str] = Field(default_factory=list)
    falsifier: str = ""

    # 7. The dated thing that will test it.
    what_to_watch: str = ""

    # Provenance and linkage.
    citations: list[Citation] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    pattern_kind: str | None = None

    # Rendering hint: the chart the header image should show, computed from our data.
    chart_kind: str | None = None  # "price_around_filing" | "ownership_over_time" | ...

    def is_publishable(self) -> bool:
        """A thesis without a falsifier or a citation is an opinion, and does not ship."""
        return bool(self.claim and self.falsifier and self.citations)
