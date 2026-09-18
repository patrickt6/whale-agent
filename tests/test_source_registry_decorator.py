"""The decorator-based source registry: registration, unknown-name errors, annotation
sources being visible but off.
"""

from __future__ import annotations

import pytest

from whale_agent.config import Settings
from whale_agent.ingestion.registry import (
    UnknownSourceError,
    build_sources,
    register_source,
    registered_source_names,
)


def test_previously_invisible_annotation_sources_are_now_registered():
    """The task's headline gap: lobbying/usaspending/quiver context adapters ran for
    nobody because they were absent from the registry. They must be visible now."""
    names = set(registered_source_names())
    assert {"senate_lda", "usaspending", "quiver_lobbying", "quiver_gov_contracts"} <= names


def test_annotation_sources_are_off_by_default_and_labelled():
    specs = {s.name: s for s in build_sources(Settings())}
    for name in ("senate_lda", "usaspending", "quiver_lobbying", "quiver_gov_contracts"):
        spec = specs[name]
        assert spec.enabled is False
        assert spec.kind == "annotation"
        assert spec.disabled_reason  # never silently blank


def test_event_sources_are_unaffected_by_the_conversion():
    specs = {s.name: s for s in build_sources(Settings())}
    assert specs["sec_form4"].enabled is True
    assert specs["sec_form4"].kind == "event"
    assert specs["dataroma"].enabled is True


def test_unknown_source_name_in_whale_sources_is_a_hard_error():
    settings = Settings(sources=["not_a_real_source"])
    with pytest.raises(UnknownSourceError, match="not_a_real_source"):
        build_sources(settings)


def test_unknown_source_error_lists_registered_names():
    settings = Settings(sources=["nope"])
    try:
        build_sources(settings)
    except UnknownSourceError as exc:
        assert "sec_form4" in str(exc)
    else:
        pytest.fail("expected UnknownSourceError")


def test_registering_a_duplicate_name_raises():
    with pytest.raises(ValueError, match="already registered"):

        @register_source("sec_form4")
        def _dup(s, limit, on):  # pragma: no cover - never called
            raise AssertionError
