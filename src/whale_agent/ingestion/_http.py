"""Shared HTTP client for every vendor and jurisdiction adapter.

One place for timeouts, retry/backoff, and rate-limit politeness so no adapter invents
its own. Retries are limited to transient conditions (429, 5xx, network errors); a 401
or 404 fails immediately, because retrying a bad key just wastes the quota.

Tests never hit the network: `parse()`/`normalize()` are pure and exercised against
saved fixtures, and the few tests that touch this module inject a fake transport.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from whale_agent.config import Settings, get_settings
from whale_agent.errors import SourceUnavailableError

# httpx logs the full request URL at INFO, and several vendors authenticate with the key
# as a query parameter -- FMP's `?apikey=`, Whale Alert's `?api_key=`. Any run at INFO
# verbosity therefore prints live credentials into the terminal, the log file, and
# anywhere those get pasted. Silencing the request logger here, at the one place all our
# HTTP goes through, is the only fix that cannot be forgotten by a future caller.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Status codes worth retrying: rate limiting and server-side faults.
_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def request_json(
    url: str,
    *,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """Perform an HTTP request and return the decoded JSON body.

    Raises `SourceUnavailableError` on exhausted retries, non-retryable HTTP errors,
    or a body that is not valid JSON. Callers catch that and degrade gracefully.
    """
    body = request_text(
        url,
        method=method,
        params=params,
        headers=headers,
        json_body=json_body,
        settings=settings,
        client=client,
    )
    import json as _json

    try:
        return _json.loads(body)
    except ValueError as exc:
        raise SourceUnavailableError(f"{url} returned non-JSON body: {exc}") from exc


def request_text(
    url: str,
    *,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Perform an HTTP request with retry/backoff and return the raw response text."""
    s = settings or get_settings()
    owned = client is None
    cli = client or httpx.Client(timeout=s.http_timeout_seconds, follow_redirects=True)
    last_error: Exception | None = None
    try:
        for attempt in range(s.http_max_retries):
            try:
                resp = cli.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    headers=dict(headers) if headers else None,
                    json=json_body,
                )
            except httpx.HTTPError as exc:  # timeout, DNS, connection reset
                last_error = exc
            else:
                if resp.status_code < 400:
                    return resp.text
                detail = resp.text[:200]
                error = SourceUnavailableError(
                    f"{method} {url} -> HTTP {resp.status_code}: {detail}"
                )
                if resp.status_code not in _RETRYABLE:
                    raise error  # bad key / bad path: retrying cannot help
                last_error = error

            if attempt < s.http_max_retries - 1:
                # Exponential backoff; keeps us inside documented rate limits.
                time.sleep(s.http_backoff_seconds * (2**attempt))
        raise SourceUnavailableError(
            f"{method} {url} failed after {s.http_max_retries} attempts: {last_error}"
        )
    finally:
        if owned:
            cli.close()


def polite_headers(user_agent: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Standard headers. A real contact string in User-Agent is required by SEC and
    expected by TWSE; several of these sources block generic library agents."""
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if extra:
        headers.update(extra)
    return headers
