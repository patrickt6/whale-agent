"""LLM prose layer: optional language on top of a digest that is already complete.

Provider-agnostic by design. `LLMProvider` is a one-method protocol; Gemini and
Anthropic are two implementations selected by `settings.llm_provider`. Gemini is the
default because its free tier makes the digest cost nothing to run.

The contract with the rest of the system: this module can only ever *add* sentences. It
cannot change ranking, cannot change a number, and cannot prevent delivery. Every string
it produces is filtered through `provenance.py` before it reaches the render, and any
failure -- no key, HTTP error, malformed JSON, hallucinated figure -- degrades to the
deterministic template render rather than raising.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from whale_agent.config import Settings, get_settings
from whale_agent.errors import NotConfiguredError, SourceUnavailableError
from whale_agent.ingestion._http import request_json
from whale_agent.models.event import NormalizedEvent
from whale_agent.summarization.prompts import SYSTEM_PROMPT, build_user_prompt
from whale_agent.summarization.prose import DigestProse
from whale_agent.summarization.provenance import check_unsourced_numbers

log = logging.getLogger(__name__)

__all__ = [
    "DigestProse",
    "LLMProvider",
    "GeminiProvider",
    "AnthropicProvider",
    "get_provider",
    "generate_digest_prose",
    "parse_prose_response",
    "sanitize_prose",
    "extract_json",
]


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str:
        """Return the model's raw text response. Raises on transport failure."""
        ...


class GeminiProvider:
    """Google Gemini via the free-tier Generative Language REST API.

    Uses `responseMimeType: application/json` so the model returns parseable JSON
    without markdown fences; `_extract_json` still tolerates fences in case a model
    version ignores it.
    """

    name = "gemini"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def complete(self, system: str, user: str) -> str:
        key = self.settings.require("gemini_api_key")
        url = (
            f"{self.settings.gemini_base_url.rstrip('/')}"
            f"/models/{self.settings.gemini_model}:generateContent"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": self.settings.llm_temperature,
                "maxOutputTokens": self.settings.llm_max_tokens,
                "responseMimeType": "application/json",
            },
        }
        data = request_json(
            url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key,
            },
            json_body=payload,
            settings=self.settings,
        )
        return _join_gemini_parts(data)


# Model families that REJECT `temperature` with a 400 (Opus 4.7 onward, Sonnet 5,
# Fable 5). On these, response style is steered by the prompt and by `effort`.
_ANTHROPIC_NO_TEMPERATURE = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
)

# Model families that ACCEPT `output_config.effort`. Older models (Sonnet 4.5,
# Haiku 4.5) return an error for it, so it is sent only where it is supported.
_ANTHROPIC_EFFORT = _ANTHROPIC_NO_TEMPERATURE + (
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
)


def anthropic_request_body(
    model: str, system: str, user: str, max_tokens: int, temperature: float
) -> dict:
    """Build the Messages API body, including only parameters this model accepts.

    Anthropic tightened the request surface from Opus 4.7 onward: `temperature` is
    rejected outright on the newest models, while `output_config.effort` is rejected on
    the oldest. Sending the union of both would 400 on every model, so the body is
    assembled per family rather than assumed.

    `effort: "low"` is deliberate. This task is constrained rewriting of pre-computed
    figures -- the model must not reason about the numbers, only phrase them -- so deep
    thinking buys nothing and costs output tokens. On models where thinking is on by
    default it also keeps thinking from consuming the `max_tokens` budget the prose
    needs.
    """
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if not model.startswith(_ANTHROPIC_NO_TEMPERATURE):
        body["temperature"] = temperature
    if model.startswith(_ANTHROPIC_EFFORT):
        body["output_config"] = {"effort": "low"}
    return body


class AnthropicProvider:
    """Anthropic Messages API. Same contract as Gemini; swap via WHALE_LLM_PROVIDER."""

    name = "anthropic"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def complete(self, system: str, user: str) -> str:
        key = self.settings.require("anthropic_api_key")
        data = request_json(
            "https://api.anthropic.com/v1/messages",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
            json_body=anthropic_request_body(
                self.settings.anthropic_model,
                system,
                user,
                self.settings.llm_max_tokens,
                self.settings.llm_temperature,
            ),
            settings=self.settings,
        )
        return _join_anthropic_blocks(data)


def _join_gemini_parts(data: Any) -> str:
    candidates = (data or {}).get("candidates") or []
    for cand in candidates:
        parts = ((cand or {}).get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if text.strip():
            return text
    raise SourceUnavailableError(f"Gemini returned no text: {str(data)[:200]}")


def _join_anthropic_blocks(data: Any) -> str:
    blocks = (data or {}).get("content") or []
    text = "".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )
    if not text.strip():
        raise SourceUnavailableError(f"Anthropic returned no text: {str(data)[:200]}")
    return text


def get_provider(settings: Settings | None = None) -> LLMProvider | None:
    """Build the configured provider, or None when prose is disabled/unconfigured."""
    s = settings or get_settings()
    if not s.llm_enabled:
        return None
    if s.llm_provider == "gemini":
        return GeminiProvider(s)
    if s.llm_provider == "anthropic":
        return AnthropicProvider(s)
    return None


def _extract_json(text: str) -> dict:
    """Parse the model's response, tolerating markdown fences and leading chatter."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response was not a JSON object")
    return parsed


def extract_json(text: str) -> dict:
    """Public entry to the tolerant JSON parse, for other model callers.

    The layer-2 verifier faces the same problem this solves: a model that was asked for
    strict JSON and wrapped it in a fence anyway. Sharing the parser rather than copying
    it keeps one behaviour to fix when a provider invents a new way to be helpful.
    """
    return _extract_json(text)


def _clean_sentence(value: Any) -> str:
    """Accept only a plain string; anything else is discarded rather than coerced."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def sanitize_prose(
    prose: DigestProse, events: list[NormalizedEvent]
) -> tuple[DigestProse, list[str]]:
    """Drop any individual string containing a number not traceable to the events.

    Per-string filtering rather than all-or-nothing: one bad sentence loses one
    sentence, and the rest of the generated language still ships. The full rendered
    text is re-checked by the pipeline afterwards, so this is a first pass, not the
    only line of defence.
    """
    rejected: list[str] = []

    def keep(text: str) -> str:
        if not text:
            return ""
        bad = check_unsourced_numbers(text, events)
        if bad:
            rejected.append(f"{bad}: {text}")
            return ""
        return text

    def keep_all(mapping: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, text in mapping.items():
            kept = keep(text)
            if kept:
                out[key] = kept
        return out

    return (
        DigestProse(
            headline=keep(prose.headline),
            why_it_matters=keep_all(prose.why_it_matters),
            skeptic_notes=keep_all(prose.skeptic_notes),
        ),
        rejected,
    )


def parse_prose_response(text: str) -> DigestProse:
    """Turn a raw model response into a `DigestProse` (no provenance filtering yet)."""
    data = _extract_json(text)
    prose = DigestProse(headline=_clean_sentence(data.get("headline")))
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        event_id = _clean_sentence(item.get("event_id"))
        if not event_id:
            continue
        why = _clean_sentence(item.get("why_it_matters"))
        note = _clean_sentence(item.get("skeptic_note"))
        if why:
            prose.why_it_matters[event_id] = why
        if note:
            prose.skeptic_notes[event_id] = note
    return prose


def generate_digest_prose(
    events: list[NormalizedEvent],
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    coverage_notes: list[str] | None = None,
) -> DigestProse:
    """Generate digest prose, returning empty prose on any failure.

    Never raises. The digest is a complete, correct document without this function;
    prose is strictly an upgrade, and an upgrade that fails must not cost delivery.
    """
    if not events:
        return DigestProse()
    s = settings or get_settings()
    provider = provider or get_provider(s)
    if provider is None:
        log.info("LLM prose disabled (provider=%s, key set=%s)", s.llm_provider, s.llm_enabled)
        return DigestProse()

    try:
        raw = provider.complete(SYSTEM_PROMPT, build_user_prompt(events, coverage_notes))
    except (NotConfiguredError, SourceUnavailableError) as exc:
        log.warning("LLM prose unavailable (%s): %s", provider.name, exc)
        return DigestProse()

    try:
        prose = parse_prose_response(raw)
    except ValueError as exc:
        log.warning("LLM prose response was not usable JSON (%s): %s", provider.name, exc)
        return DigestProse()

    prose, rejected = sanitize_prose(prose, events)
    if rejected:
        log.warning(
            "Dropped %d LLM sentence(s) containing unsourced numbers: %s",
            len(rejected),
            rejected,
        )
    return prose
