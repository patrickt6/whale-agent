"""Remove credentials from text before it is logged, stored or shown.

Several vendors take the key as a URL query parameter (FMP `apikey`, FRED `api_key`,
EDINET `Subscription-Key`), and Telegram puts the bot token in the URL path. An error
message that quotes the URL would then carry the key into logs, `whale.db`
(`source_runs.last_error`), JSONL records and terminal output. Call `redact` on any
such text first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MASK = "REDACTED"

_SECRET_PARAM = re.compile(
    r"(?i)([?&;](?:apikey|api_key|api-key|key|token|access_token|auth|secret|password|"
    r"subscription-key|client_secret)=)[^&#\s'\"<>]+"
)
_TELEGRAM_BOT = re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def redact(text: object, secrets: Iterable[str] = ()) -> str:
    """Return `text` as a string with credentials masked.

    Masks secret-looking query parameters, Telegram bot tokens in a URL path, Bearer
    tokens, and every literal value in `secrets` that is 6 characters or longer."""
    out = str(text)
    for value in secrets:
        if value and len(value) >= 6:
            out = out.replace(value, MASK)
    out = _SECRET_PARAM.sub(rf"\1{MASK}", out)
    out = _TELEGRAM_BOT.sub(rf"\1{MASK}", out)
    out = _BEARER.sub(rf"\1{MASK}", out)
    return out


def settings_secrets(settings: object) -> list[str]:
    """Every non-empty credential field on a Settings object."""
    names = [
        n
        for n in dir(settings)
        if n.endswith(("_api_key", "_token", "_password")) and not n.startswith("_")
    ]
    values = [getattr(settings, n, "") for n in names]
    return [v for v in values if isinstance(v, str) and v]
