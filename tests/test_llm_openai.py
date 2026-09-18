"""OpenAI-compatible provider: fake httpx transport, no network."""

from __future__ import annotations

import json
from datetime import date

import httpx

from tests.conftest import make_event
from whale_agent.cli.doctor import check_llm
from whale_agent.config import Settings
from whale_agent.summarization.llm import (
    BudgetedProvider,
    OpenAIProvider,
    generate_digest_prose,
    get_provider,
    openai_request_body,
)


def _settings(**kw) -> Settings:
    base = dict(
        llm_provider="openai",
        openai_api_key="sk-test",
        http_max_retries=1,
        http_backoff_seconds=0.0,
    )
    base.update(kw)
    return Settings(**base)


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


def _provider(settings, handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return OpenAIProvider(settings, client=client), seen


def _chat(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_success_returns_prose_and_sends_json_request():
    events = _events()
    reply = json.dumps(
        {
            "headline": "A quiet day.",
            "items": [
                {"event_id": events[0].event_id, "why_it_matters": "A large holder added."}
            ],
        }
    )
    s = _settings()
    provider, seen = _provider(s, lambda r: _chat(reply))
    prose = generate_digest_prose(events, settings=s, provider=provider)
    assert prose.headline == "A quiet day."
    assert prose.why_it_matters[events[0].event_id] == "A large holder added."
    req = seen[0]
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == s.openai_model


def test_http_error_falls_back_to_empty_prose():
    s = _settings()
    provider, _ = _provider(s, lambda r: httpx.Response(401, json={"error": "bad key"}))
    prose = generate_digest_prose(_events(), settings=s, provider=provider)
    assert not prose.headline and not prose.why_it_matters


def test_malformed_json_falls_back_to_empty_prose():
    s = _settings()
    provider, _ = _provider(s, lambda r: _chat("not json at all"))
    prose = generate_digest_prose(_events(), settings=s, provider=provider)
    assert not prose.headline and not prose.why_it_matters


def test_no_key_for_remote_server_disables_provider():
    s = _settings(openai_api_key="")
    assert not s.llm_enabled
    assert get_provider(s) is None
    assert not check_llm(s).ok


def test_localhost_needs_no_key_and_sends_no_auth_header():
    s = _settings(
        openai_api_key="", openai_base_url="http://localhost:11434/v1", openai_model="llama3.1"
    )
    assert s.llm_enabled
    # `get_provider` wraps every billable provider in the per-run call budget, so the
    # OpenAI provider is reached through `.inner`.
    provider = get_provider(s)
    assert isinstance(provider, BudgetedProvider)
    assert isinstance(provider._inner, OpenAIProvider)
    provider, seen = _provider(s, lambda r: _chat('{"headline": "Hi."}'))
    assert provider.complete("sys", "user") == '{"headline": "Hi."}'
    assert "Authorization" not in seen[0].headers
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == s.llm_max_tokens and "temperature" in body


def test_openai_host_body_uses_reasoning_parameters():
    body = openai_request_body("gpt-5.6-luna", "s", "u", 100, 0.2, "https://api.openai.com/v1")
    assert body["max_completion_tokens"] == 100
    assert "temperature" not in body and "max_tokens" not in body
