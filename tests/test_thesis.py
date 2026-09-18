"""Thesis generation, with a stub provider. No live API calls, ever.

The behaviour under test is containment, not eloquence: a model that fabricates a figure
loses the sentence, a model that is absent costs nothing, and the falsifier the detector
attached survives whatever the model does with it.
"""

from __future__ import annotations

import json
from datetime import date

from tests.conftest import make_event
from tests.test_llm_prose import StubProvider
from whale_agent.analysis.patterns import Pattern, detect_patterns
from whale_agent.models.context import EventContext
from whale_agent.models.enums import TransactionType
from whale_agent.models.thesis import Thesis
from whale_agent.summarization.thesis import (
    fallback_thesis,
    generate_theses,
    generate_thesis,
    slug_for,
)

ON = date(2026, 7, 25)


def buy(filer: str, issuer: str = "SmallCo", **overrides):
    base = dict(
        filer_name=filer,
        filer_id="F-" + filer.lower().replace(" ", "-"),
        issuer_name=issuer,
        issuer_id="I-" + issuer.lower().replace(" ", "-"),
        transaction_type=TransactionType.OPEN_MARKET_BUY,
        usd_value=10_000_000.0,
        source_url="https://www.sec.gov/Archives/edgar/data/123/000186332826000002.txt",
    )
    base.update(overrides)
    return make_event(**base)


def a_pattern(**overrides) -> Pattern:
    """A three-filer cluster, the cleanest thing the detectors can find."""
    events = [buy("Alpha Capital"), buy("Beta Partners"), buy("Gamma Advisors")]
    found = detect_patterns(events)
    assert found, "fixture must produce a reportable pattern"
    pattern = found[0]
    return Pattern(**{**pattern.__dict__, **overrides}) if overrides else pattern


def model_response(**overrides) -> str:
    body = {
        "title": "Three funds bought the same small company",
        "lede": "Three unrelated filers disclosed purchases in the same name this week.",
        "claim": "The concentration is worth checking against the next reporting window.",
        "trigger_summary": "Three separate filings landed in one window.",
        "evidence": ["Each filer disclosed a position of $10.0M."],
        "context_notes": ["All three filings came through the same national regime."],
        "counter_evidence": ["Three funds can react to the same public news independently."],
        "falsifier": "No further filer discloses a purchase in the following windows.",
        "what_to_watch": "The next round of filings for the same issuer.",
    }
    body.update(overrides)
    return json.dumps(body)


def test_the_deterministic_fallback_produces_a_publishable_thesis_with_no_model():
    """With no provider at all, the layer still emits a complete, citable article."""
    thesis = generate_thesis(a_pattern(), provider=None, on=ON)
    assert isinstance(thesis, Thesis)
    assert thesis.is_publishable()
    assert thesis.falsifier
    assert thesis.citations
    assert len(thesis.evidence) == 3


def test_a_thesis_without_a_falsifier_is_not_publishable():
    """`is_publishable` is the ship gate, and a claim with nothing to refute it fails it."""
    thesis = fallback_thesis(a_pattern(), ON)
    assert thesis is not None
    assert not thesis.model_copy(update={"falsifier": ""}).is_publishable()
    assert not thesis.model_copy(update={"citations": []}).is_publishable()


def test_a_fabricated_figure_in_the_model_output_is_dropped():
    """A sentence carrying a number the filings cannot account for loses the sentence."""
    pattern = a_pattern()
    provider = StubProvider(
        model_response(claim="The three filers committed $940.0M between them.")
    )
    thesis = generate_thesis(pattern, provider, ON)
    assert thesis is not None
    assert "940" not in thesis.claim
    # The deterministic claim is what survives, so the article is still complete.
    assert thesis.claim == fallback_thesis(pattern, ON).claim


def test_the_model_cannot_weaken_the_detectors_falsifier():
    """The pattern's own falsifier is kept verbatim; the model may only add to it."""
    pattern = a_pattern()
    provider = StubProvider(model_response(falsifier="Probably nothing to worry about."))
    thesis = generate_thesis(pattern, provider, ON)
    assert thesis is not None
    assert pattern.falsifier in thesis.falsifier
    assert "Probably nothing to worry about." in thesis.falsifier


def test_model_language_is_used_when_it_survives_the_gate():
    """Clean output replaces the plainer deterministic phrasing field by field."""
    thesis = generate_thesis(a_pattern(), StubProvider(model_response()), ON)
    assert thesis is not None
    assert thesis.title == "Three funds bought the same small company"
    assert "Three separate filings" in thesis.trigger_summary


def test_deterministic_counter_evidence_is_never_replaced_by_the_model():
    """Caveats about the data itself are facts, and the model does not get to drop them."""
    pattern = a_pattern()
    base = fallback_thesis(pattern, ON)
    thesis = generate_thesis(pattern, StubProvider(model_response()), ON)
    assert thesis is not None
    for note in base.counter_evidence:
        assert note in thesis.counter_evidence
    assert len(thesis.counter_evidence) > len(base.counter_evidence)


def test_a_broken_provider_costs_the_language_and_nothing_else():
    """Transport failure degrades to the deterministic article rather than raising."""
    pattern = a_pattern()
    thesis = generate_thesis(pattern, StubProvider(error=RuntimeError("boom")), ON)
    assert thesis is not None
    assert thesis.claim == fallback_thesis(pattern, ON).claim


def test_unparseable_model_output_degrades_to_the_deterministic_article():
    """Prose that is not JSON is discarded whole, and the article still ships."""
    pattern = a_pattern()
    thesis = generate_thesis(pattern, StubProvider("I would rather write a poem."), ON)
    assert thesis is not None
    assert thesis.title == fallback_thesis(pattern, ON).title


def test_slugs_are_stable_across_regeneration():
    """The same pattern on the same date must resolve to the same URL every run."""
    pattern = a_pattern()
    assert slug_for(pattern, ON) == slug_for(pattern, ON)
    assert generate_thesis(pattern, None, ON).slug == slug_for(pattern, ON)


def test_no_patterns_means_no_theses():
    """A quiet week produces nothing rather than a manufactured note."""
    assert generate_theses([], None, ON) == []


def test_context_notes_only_mention_enrichment_that_was_actually_attached():
    """Absent vendor context stays absent; nothing is filled in to round out a section."""
    plain = fallback_thesis(a_pattern(), ON)
    assert not any("Sector" in note for note in plain.context_notes)

    events = [
        buy(name, context=EventContext(sector="Industrials", market_cap_usd=210_000_000.0))
        for name in ("Alpha Capital", "Beta Partners", "Gamma Advisors")
    ]
    enriched = fallback_thesis(detect_patterns(events)[0], ON)
    assert any("Industrials" in note for note in enriched.context_notes)
    assert any("$210.0M" in note for note in enriched.context_notes)
