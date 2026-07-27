"""The `DigestProse` container.

Lives in its own module so `render.py` can accept generated prose without importing
`llm.py` -- which imports `provenance.py`, which imports `render.py`. Keeping the data
shape separate from the thing that produces it breaks that cycle and keeps the render
layer free of any LLM dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DigestProse:
    """Generated language, keyed by event id. Every field is optional by construction.

    An empty instance is the normal, fully supported state: it is what the pipeline
    uses whenever the LLM is disabled, unreachable, or produced something untrustworthy.
    """

    headline: str = ""
    why_it_matters: dict[str, str] = field(default_factory=dict)
    skeptic_notes: dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.headline or self.why_it_matters or self.skeptic_notes)
