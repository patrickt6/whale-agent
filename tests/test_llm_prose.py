"""The LLM prose layer, with a stub provider. No live API calls, ever.

The behaviour that matters is not "does it write nice sentences" -- it is what happens
when it writes a wrong one. Every test here is about containment: a fabricated number is
dropped, a broken provider costs nothing, and the digest ships regardless.
"""

from __future__ import annotations

import json
from datetime import date

from tests.conftest import make_event
from whale_agent.config import Settings
from whale_agent.errors import SourceUnavailableError
from whale_agent.summarization.llm import (
    generate_digest_prose,
    parse_prose_response,
    sanitize_prose,
)
from whale_agent.summarization.prose import DigestProse


class StubProvider:
    """Returns a canned response, or raises, without touching the network."""

    name = "stub"

    def __init__(self, response: str = "", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.response


def _events():
    return [
        make_event(
            filer_name="Jane Example",
            issuer_name="TestCo",
            usd_value=12_400_000.0,
            transaction_date=date(2026, 7, 24),
            disclosure_date=date(2026, 7, 25),
        )
    ]


def _response(events, why: str, headline: str = "A quiet day.", note: str = "") -> str:
    return json.dumps(
        {
            "headline": headline,
            "items": [
                {"event_id": events[0].event_id, "why_it_matters": why, "skeptic_note": note}
            ],
        }
    )


def test_clean_prose_passes_through():
    events = _events()
    provider = StubProvider(_response(events, "A first-time buyer taking a $12.4M stake."))
    prose = generate_digest_prose(events, Settings(), provider)
    assert prose.headline == "A quiet day."
    assert "12.4M" in prose.why_it_matters[events[0].event_id]


def test_fabricated_number_is_dropped_not_shipped():
    events = _events()
    # $47.2M appears nowhere in the event data. The sentence must not survive.
    provider = StubProvider(_response(events, "They bought $47.2M of it."))
    prose = generate_digest_prose(events, Settings(), provider)
    assert prose.why_it_matters == {}


def test_one_bad_sentence_does_not_lose_the_good_ones():
    events = _events()
    raw = json.dumps(
        {
            "headline": "Market cap reached $900B today.",  # unsourced
            "items": [
                {
                    "event_id": events[0].event_id,
                    "why_it_matters": "A first-time filer at $12.4M.",  # sourced
                    "skeptic_note": "",
                }
            ],
        }
    )
    prose = generate_digest_prose(events, Settings(), StubProvider(raw))
    assert prose.headline == ""
    assert prose.why_it_matters  # the good sentence survived


def test_provider_failure_returns_empty_prose_rather_than_raising():
    events = _events()
    provider = StubProvider(error=SourceUnavailableError("HTTP 503"))
    prose = generate_digest_prose(events, Settings(), provider)
    assert prose.is_empty()


def test_malformed_json_returns_empty_prose():
    events = _events()
    prose = generate_digest_prose(events, Settings(), StubProvider("not json at all"))
    assert prose.is_empty()


def test_markdown_fenced_json_is_still_parsed():
    events = _events()
    fenced = "```json\n" + _response(events, "Steady accumulation.") + "\n```"
    prose = generate_digest_prose(events, Settings(), StubProvider(fenced))
    assert prose.why_it_matters[events[0].event_id] == "Steady accumulation."


def test_non_string_fields_are_discarded_not_coerced():
    events = _events()
    raw = json.dumps(
        {
            "headline": {"nope": 1},
            "items": [{"event_id": events[0].event_id, "why_it_matters": 42}],
        }
    )
    prose = parse_prose_response(raw)
    assert prose.headline == ""
    assert prose.why_it_matters == {}


def test_no_provider_means_no_prose_and_no_error():
    assert generate_digest_prose(_events(), Settings(), None).is_empty()


def test_empty_event_list_short_circuits():
    provider = StubProvider("should never be called")
    assert generate_digest_prose([], Settings(), provider).is_empty()
    assert provider.calls == []


def test_sanitize_reports_what_it_rejected():
    events = _events()
    prose = DigestProse(headline="They spent $999.9M.", why_it_matters={}, skeptic_notes={})
    cleaned, rejected = sanitize_prose(prose, events)
    assert cleaned.headline == ""
    assert len(rejected) == 1
    assert "999.9M" in rejected[0]


def test_prompt_never_exposes_the_score():
    """The model must not be able to quote an internal ranking number as if it were data."""
    events = _events()
    events[0].score = 3.14159
    provider = StubProvider(_response(events, "Fine."))
    generate_digest_prose(events, Settings(), provider)
    _, user_prompt = provider.calls[0]
    assert "3.14159" not in user_prompt


# -- Anthropic request shape ---------------------------------------------------
# Anthropic tightened the request surface from Opus 4.7 onward: `temperature` is
# rejected with a 400 on the newest models, and `output_config.effort` is rejected
# on the oldest. Sending the union would fail on every model.


def test_new_anthropic_models_omit_temperature():
    from whale_agent.summarization.llm import anthropic_request_body

    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"):
        body = anthropic_request_body(model, "sys", "user", 4000, 0.2)
        assert "temperature" not in body, model
        assert body["output_config"] == {"effort": "low"}, model


def test_older_anthropic_models_keep_temperature():
    from whale_agent.summarization.llm import anthropic_request_body

    body = anthropic_request_body("claude-sonnet-4-5", "sys", "user", 4000, 0.2)
    assert body["temperature"] == 0.2
    # Effort errors on Sonnet 4.5, so it must not be sent.
    assert "output_config" not in body


def test_mid_generation_models_take_effort_and_temperature():
    from whale_agent.summarization.llm import anthropic_request_body

    body = anthropic_request_body("claude-sonnet-4-6", "sys", "user", 4000, 0.2)
    assert body["temperature"] == 0.2
    assert body["output_config"] == {"effort": "low"}


# Thinking tokens bill as output tokens ($25/MTok on Opus 5), and on that model
# omitting `thinking` leaves adaptive thinking ON. This task is constrained
# rewriting of figures the code already established, so the reasoning is pure cost.
# The parameter is only valid on some families, and only at some effort levels.


def test_thinking_is_disabled_where_the_model_would_otherwise_think():
    from whale_agent.summarization.llm import anthropic_request_body

    for model in ("claude-opus-5", "claude-sonnet-5"):
        body = anthropic_request_body(model, "sys", "user", 4000, 0.2)
        assert body["thinking"] == {"type": "disabled"}, model


def test_fable_and_mythos_never_receive_the_thinking_parameter():
    """An explicit disabled returns 400 on these at any effort, so it is not sent."""
    from whale_agent.summarization.llm import anthropic_request_body

    for model in ("claude-fable-5", "claude-mythos-5"):
        assert "thinking" not in anthropic_request_body(model, "s", "u", 4000, 0.2), model


def test_older_models_never_receive_the_thinking_parameter():
    """Sonnet 4.5 and Haiku 4.5 use the older budget_tokens API, not this shape."""
    from whale_agent.summarization.llm import anthropic_request_body

    for model in ("claude-sonnet-4-5", "claude-haiku-4-5", "claude-sonnet-4-6"):
        assert "thinking" not in anthropic_request_body(model, "s", "u", 4000, 0.2), model


def test_raising_effort_drops_thinking_rather_than_producing_a_400():
    """Opus 5 rejects disabled thinking above effort high. The pair cannot be sent."""
    from whale_agent.summarization.llm import anthropic_request_body

    for effort in ("xhigh", "max"):
        body = anthropic_request_body("claude-opus-5", "s", "u", 4000, 0.2, effort=effort)
        assert body["output_config"] == {"effort": effort}
        assert "thinking" not in body, effort
    for effort in ("low", "medium", "high"):
        body = anthropic_request_body("claude-opus-5", "s", "u", 4000, 0.2, effort=effort)
        assert body["thinking"] == {"type": "disabled"}, effort


def test_anthropic_body_always_carries_the_core_fields():
    from whale_agent.summarization.llm import anthropic_request_body

    body = anthropic_request_body("claude-opus-5", "SYS", "USER", 1234, 0.2)
    assert body["model"] == "claude-opus-5"
    assert body["max_tokens"] == 1234
    assert body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": "USER"}]


# -- the manual provider ------------------------------------------------------
#
# Standing in for the API by hand is only safe if it is indistinguishable from a
# provider: it must spool the prompt, degrade like an outage while unanswered, and
# then be subject to the same provenance filtering once answered.


def _manual(tmp_path):
    from whale_agent.summarization.llm import ManualProvider

    return ManualProvider(Settings(llm_provider="manual", llm_spool_dir=str(tmp_path)))


def test_manual_provider_spools_the_prompt_and_reads_as_an_outage(tmp_path):
    from whale_agent.errors import NotConfiguredError

    provider = _manual(tmp_path)
    try:
        provider.complete("sys", "user")
    except NotConfiguredError:
        pass
    else:  # pragma: no cover - the assertion below reports it
        raise AssertionError("an unanswered prompt must raise")

    prompts = list(tmp_path.glob("*.prompt.txt"))
    assert len(prompts) == 1
    body = prompts[0].read_text()
    assert "sys" in body and "user" in body


def test_manual_provider_returns_the_hand_written_reply(tmp_path):
    provider = _manual(tmp_path)
    key = provider.key_for("sys", "user")
    (tmp_path / f"{key}.response.json").write_text('{"headline": "written by hand"}')

    assert provider.complete("sys", "user") == '{"headline": "written by hand"}'


def test_manual_provider_keys_are_prompt_specific(tmp_path):
    provider = _manual(tmp_path)
    assert provider.key_for("sys", "a") != provider.key_for("sys", "b")


def test_manual_prose_is_still_provenance_filtered(tmp_path):
    """A hand-written figure that no event supports is dropped, exactly as a model's is."""
    provider = _manual(tmp_path)
    events = [make_event(actor="Someone", value_usd=5_000_000)]
    system, user = "sys", "ignored"
    key = provider.key_for(system, user)
    (tmp_path / f"{key}.response.json").write_text(
        json.dumps({"headline": "A $999,999,999 position appeared."})
    )

    cleaned, _dropped = sanitize_prose(
        parse_prose_response(provider.complete(system, user)), events
    )
    assert "999,999,999" not in (cleaned.headline or "")


def test_manual_provider_is_enabled_without_any_key():
    assert Settings(llm_provider="manual").llm_enabled is True
