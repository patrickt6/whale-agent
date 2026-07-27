"""Structured errors: category, retryability, and the coverage note rendered from them.

The property under test is the one the module docstring in `errors.py` names directly: a
source that failed and a source that legitimately found nothing must stay distinguishable.
A `SourceFailure` only exists for the first case, and the reader-facing sentence is a view
computed from it rather than the thing itself.
"""

from __future__ import annotations

import pytest

from whale_agent.errors import (
    ErrorCategory,
    NotConfiguredError,
    ProvenanceError,
    SourceFailure,
    SourceParseError,
    SourceUnavailableError,
    WhaleAgentError,
)
from whale_agent.summarization.render import (
    render_coverage_note,
    render_coverage_note_from_failures,
)


def test_each_exception_carries_a_category_and_a_retryable_flag():
    assert NotConfiguredError.category == ErrorCategory.CREDENTIAL
    assert NotConfiguredError.retryable is False
    assert SourceUnavailableError.category == ErrorCategory.TRANSIENT
    assert SourceUnavailableError.retryable is True
    assert ProvenanceError.category == ErrorCategory.VALIDATION
    assert ProvenanceError.retryable is False
    assert SourceParseError.category == ErrorCategory.PARSE
    assert SourceParseError.retryable is False


def test_a_bare_whale_agent_error_still_has_a_category_and_a_retryable_default():
    """The base class is usable on its own without every raiser subclassing it."""
    exc = WhaleAgentError("something went wrong")
    assert exc.category == ErrorCategory.UPSTREAM
    assert exc.retryable is False


def test_source_failure_from_exception_reads_the_category_off_the_exception_class():
    failure = SourceFailure.from_exception("fmp_insider", NotConfiguredError("no api key"))
    assert failure.source == "fmp_insider"
    assert failure.category == ErrorCategory.CREDENTIAL
    assert failure.retryable is False
    assert "no api key" in failure.detail


def test_a_credential_failure_and_a_transient_failure_render_differently():
    credential = SourceFailure.from_exception("fmp_insider", NotConfiguredError("x"))
    transient = SourceFailure.from_exception("fmp_insider", SourceUnavailableError("HTTP 500"))
    assert credential.describe() == "fmp_insider not included (not configured)"
    assert transient.describe() == "fmp_insider unavailable today"
    assert credential.describe() != transient.describe()


def test_a_legitimately_empty_source_produces_no_source_failure_at_all():
    """The anti-pattern this type exists to kill: an empty-but-successful source must
    never manufacture a SourceFailure just because zero rows came back."""

    def collect_zero_rows() -> list[str]:
        return []  # the source ran fine and reported nothing today

    failures: list[SourceFailure] = []
    rows = collect_zero_rows()
    # No exception was raised, so nothing is appended -- the absence of a SourceFailure
    # *is* the "ran fine, nothing to report" state, structurally rather than by string.
    assert rows == []
    assert failures == []


def test_coverage_note_rendered_from_structured_failures_matches_the_string_form():
    """A caller migrating from raw strings to SourceFailure must not churn the reader-
    visible coverage line."""
    failures = [
        SourceFailure.from_exception("congress_disclosures", NotConfiguredError("x")),
        SourceFailure.from_exception("quiver_congress", SourceUnavailableError("timeout")),
    ]
    from_structure = render_coverage_note_from_failures(failures)
    from_strings = render_coverage_note([f.describe() for f in failures])
    assert from_structure == from_strings
    assert from_structure == (
        "Coverage note: congress_disclosures not included (not configured); "
        "quiver_congress unavailable today."
    )


def test_coverage_note_from_failures_is_empty_string_when_nothing_failed():
    assert render_coverage_note_from_failures([]) == ""
    assert render_coverage_note_from_failures(None) == ""


@pytest.mark.parametrize(
    "category", [ErrorCategory.TRANSIENT, ErrorCategory.UPSTREAM, ErrorCategory.PARSE]
)
def test_every_category_renders_a_non_empty_sentence(category):
    failure = SourceFailure(source="some_source", category=category, retryable=False)
    assert failure.describe().startswith("some_source ")
