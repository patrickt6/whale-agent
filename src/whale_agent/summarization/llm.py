"""LLM prose layer: optional language on top of a digest that is already complete.

Provider-agnostic by design. `LLMProvider` is a one-method protocol; Gemini,
Anthropic and OpenAI-compatible servers are implementations selected by
`settings.llm_provider`. Gemini is the
default because its free tier makes the digest cost nothing to run.

The contract with the rest of the system: this module can only ever *add* sentences. It
cannot change ranking, cannot change a number, and cannot prevent delivery. Every string
it produces is filtered through `provenance.py` before it reaches the render, and any
failure -- no key, HTTP error, malformed JSON, hallucinated figure -- degrades to the
deterministic template render rather than raising.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
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
    "ManualProvider",
    "OpenAIProvider",
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


# Model families that accept an explicit `thinking: {"type": "disabled"}` AND would
# otherwise think by default. On Opus 5 and Sonnet 5, omitting `thinking` runs adaptive
# thinking, and thinking tokens bill as output tokens ($25/MTok on Opus 5), so the
# parameter is the difference between paying for reasoning and not.
#
# Deliberately narrow. Fable 5 and Mythos 5 reject an explicit disabled with a 400 at
# any effort, so they must not appear here. Opus 4.8 and 4.7 accept it but do not think
# unless asked, so sending it buys nothing. Sonnet 4.5 and Haiku 4.5 use the older
# budget_tokens API and would reject this shape outright.
_ANTHROPIC_THINKING_DISABLED = (
    "claude-opus-5",
    "claude-sonnet-5",
)

# Effort levels at which disabled thinking is accepted. On Opus 5 the pair is valid only
# at "high" or below: "xhigh" and "max" return a 400. The effort below is "low", so this
# never bites today, but the check is here so that raising effort later silently costs
# thinking tokens rather than breaking every call with a 400.
_ANTHROPIC_EFFORT_ALLOWING_DISABLED_THINKING = ("low", "medium", "high")

# `effort: "low"` is deliberate: see the docstring below.
_ANTHROPIC_EFFORT_LEVEL = "low"


def anthropic_request_body(
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    effort: str = _ANTHROPIC_EFFORT_LEVEL,
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

    For the same reason thinking is switched off outright where the parameter is valid.
    The system prompt says the finding has already been established by code and the
    model's job is to describe it, so there is nothing to reason about. Note that the
    prompt does not, and must not, *tell* the model not to think: an explicit
    do-not-reason instruction measurably increases the chance of <thinking> tags leaking
    into the output. The parameter does the work silently.
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
        body["output_config"] = {"effort": effort}
    if (
        model.startswith(_ANTHROPIC_THINKING_DISABLED)
        and effort in _ANTHROPIC_EFFORT_ALLOWING_DISABLED_THINKING
    ):
        body["thinking"] = {"type": "disabled"}
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


def _is_local_url(base_url: str) -> bool:
    """True for a model server on this machine (Ollama, llama.cpp), where no key is needed."""
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def openai_request_body(
    model: str, system: str, user: str, max_tokens: int, temperature: float, base_url: str
) -> dict:
    """Build a Chat Completions body that the target server accepts.

    `response_format: json_object` asks for JSON on every server; `_extract_json` still
    tolerates fences where a server ignores it. On api.openai.com the current models are
    reasoning models, so the body uses `max_completion_tokens` and `reasoning_effort:
    "low"` and omits `temperature`. OpenRouter, Ollama and other compatible servers get
    the classic `max_tokens` and `temperature`, which they document widely.
    """
    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    if "api.openai.com" in base_url:
        body["max_completion_tokens"] = max_tokens
        body["reasoning_effort"] = "low"
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = temperature
    return body


class OpenAIProvider:
    """OpenAI-compatible Chat Completions (OpenAI, OpenRouter, Ollama) over plain httpx.

    Same contract as Gemini and Anthropic. `OPENAI_BASE_URL` selects the server; a key
    is required except for a server on localhost.
    """

    name = "openai"

    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        self.settings = settings or get_settings()
        self.client = client

    def complete(self, system: str, user: str) -> str:
        base = self.settings.openai_base_url.rstrip("/")
        key = self.settings.openai_api_key
        if not key and not _is_local_url(base):
            raise NotConfiguredError("OPENAI_API_KEY not set")
        if not self.settings.openai_model:
            raise NotConfiguredError("OPENAI_MODEL not set")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = request_json(
            f"{base}/chat/completions",
            method="POST",
            headers=headers,
            json_body=openai_request_body(
                self.settings.openai_model,
                system,
                user,
                self.settings.llm_max_tokens,
                self.settings.llm_temperature,
                base,
            ),
            settings=self.settings,
            client=self.client,
        )
        return _join_openai_choices(data)


def _join_openai_choices(data: Any) -> str:
    choices = (data or {}).get("choices") if isinstance(data, dict) else None
    for choice in choices or []:
        content = ((choice or {}).get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content
    raise SourceUnavailableError(
        f"OpenAI-compatible server returned no text: {str(data)[:200]}"
    )


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


class BudgetExceeded(RuntimeError):
    """Raised when a run has already made as many model calls as it is allowed."""


class BudgetedProvider:
    """Wraps a provider and refuses to make more than `max_calls` requests per run.

    This is a spend ceiling that does not depend on anything outside the process. Every
    caller of `complete` already treats a raised exception as "prose is unavailable" and
    falls back to the deterministic template, so hitting the ceiling degrades output
    rather than failing the run: the digest still ships, it just ships without language.

    The ceiling is per `BudgetedProvider` instance, which is per run, because that is the
    unit a person actually reasons about when asking what one run can cost.
    """

    def __init__(self, inner: LLMProvider, max_calls: int) -> None:
        self._inner = inner
        self._max_calls = max_calls
        self.calls_made = 0
        self.name = inner.name

    def complete(self, system: str, user: str) -> str:
        if self.calls_made >= self._max_calls:
            raise BudgetExceeded(
                f"model call budget of {self._max_calls} for this run is spent; "
                "remaining sections render from the deterministic template"
            )
        self.calls_made += 1
        log.info("Model call %d of %d", self.calls_made, self._max_calls)
        return self._inner.complete(system, user)


class ManualProvider:
    """A human (or an agent at a terminal) standing in for the model API.

    Exists because the hosted key can be rate-limited for weeks at a time while the
    weekly still has to go out with prose. It is the same one-method contract, served
    off a directory instead of a socket:

      1. `complete` hashes the prompt pair. If `<key>.response.json` is already in the
         spool, its contents are returned exactly as an API response would be.
      2. If not, the prompt is written to `<key>.prompt.txt` and the call raises
         `NotConfiguredError` -- the one error the callers already treat as "no prose
         today", so the first pass renders deterministically and costs nothing.

    So a two-pass run captures every prompt, gets them answered by hand, and renders
    again with the answers in place. Nothing downstream changes: replies still go
    through `provenance.py`, and a reply that invents a figure is dropped exactly as a
    model's would be.
    """

    name = "manual"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.spool = Path(self.settings.llm_spool_dir)

    @staticmethod
    def key_for(system: str, user: str) -> str:
        digest = hashlib.sha256(f"{system}\0{user}".encode()).hexdigest()
        return digest[:16]

    def complete(self, system: str, user: str) -> str:
        key = self.key_for(system, user)
        reply = self.spool / f"{key}.response.json"
        if reply.exists():
            text = reply.read_text(encoding="utf-8")
            if text.strip():
                return text
            raise SourceUnavailableError(f"manual reply {reply} is empty")

        self.spool.mkdir(parents=True, exist_ok=True)
        prompt = self.spool / f"{key}.prompt.txt"
        prompt.write_text(
            f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}\n", encoding="utf-8"
        )
        raise NotConfiguredError(
            f"awaiting a hand-written reply: write {reply} answering {prompt}"
        )


def get_provider(settings: Settings | None = None) -> LLMProvider | None:
    """Build the configured provider, or None when prose is disabled/unconfigured."""
    s = settings or get_settings()
    if not s.llm_enabled:
        return None
    # The manual provider is answered by a human off a spool directory, so it costs
    # nothing and is deliberately left unbudgeted: a call ceiling would stop the first
    # pass partway through writing out the very prompts it exists to collect.
    if s.llm_provider == "manual":
        return ManualProvider(s)
    inner: LLMProvider | None = None
    if s.llm_provider == "gemini":
        inner = GeminiProvider(s)
    elif s.llm_provider == "anthropic":
        inner = AnthropicProvider(s)
    elif s.llm_provider == "openai":
        inner = OpenAIProvider(s)
    if inner is None:
        return None
    return BudgetedProvider(inner, s.llm_max_calls_per_run)


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
