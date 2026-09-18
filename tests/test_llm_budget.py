"""A run cannot spend more model calls than its configured ceiling.

The ceiling exists because the failure mode being guarded against is not a wrong answer,
it is an unbounded bill: a pathological corpus, a retry loop, or a future caller added
inside a per-event loop. Exceeding it degrades output to the deterministic template,
which every caller already handles, rather than raising to the operator.
"""

from __future__ import annotations

import pytest

from whale_agent.summarization.llm import BudgetedProvider, BudgetExceeded


class CountingProvider:
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return "{}"


def test_calls_under_the_ceiling_pass_through():
    inner = CountingProvider()
    provider = BudgetedProvider(inner, max_calls=3)
    for _ in range(3):
        provider.complete("sys", "user")
    assert inner.calls == 3


def test_the_call_after_the_ceiling_is_refused():
    inner = CountingProvider()
    provider = BudgetedProvider(inner, max_calls=2)
    provider.complete("sys", "user")
    provider.complete("sys", "user")
    with pytest.raises(BudgetExceeded):
        provider.complete("sys", "user")


def test_a_refused_call_never_reaches_the_paid_api():
    """The ceiling has to be checked before the request, or it does not save anything."""
    inner = CountingProvider()
    provider = BudgetedProvider(inner, max_calls=1)
    provider.complete("sys", "user")
    with pytest.raises(BudgetExceeded):
        provider.complete("sys", "user")
    assert inner.calls == 1


def test_a_zero_budget_makes_every_call_refuse():
    inner = CountingProvider()
    provider = BudgetedProvider(inner, max_calls=0)
    with pytest.raises(BudgetExceeded):
        provider.complete("sys", "user")
    assert inner.calls == 0
