"""Credentials never reach logs, whale.db or error text."""

from __future__ import annotations

import httpx
import pytest

from whale_agent.config import Settings
from whale_agent.errors import SourceUnavailableError
from whale_agent.ingestion._http import request_text
from whale_agent.redact import MASK, redact, settings_secrets

KEY = "fmpSECRETkey1234567890"  # gitleaks:allow -- invented fixture, not a real key


@pytest.mark.parametrize(
    "text",
    [
        f"GET https://financialmodelingprep.com/stable/x?cik=1&apikey={KEY} -> HTTP 401",
        f"https://api.stlouisfed.org/fred/series?api_key={KEY}&file_type=json",
        f"https://api.edinet-fsa.go.jp/api/v2/documents.json?type=2&Subscription-Key={KEY}",
        f"https://api.telegram.org/bot123456:{KEY}/sendMessage",
        f"Authorization: Bearer {KEY}",
    ],
)
def test_redact_masks_known_shapes(text):
    out = redact(text)
    assert KEY not in out and MASK in out


def test_redact_masks_literal_secret_values_anywhere():
    assert KEY not in redact(f"vendor said: bad key {KEY}", [KEY])


def test_settings_secrets_lists_keys_and_tokens():
    s = Settings(fmp_api_key=KEY, telegram_bot_token="123:abcdef", smtp_password="pw-123456")
    got = settings_secrets(s)
    assert KEY in got and "123:abcdef" in got and "pw-123456" in got


def _client(status: int, body: str = "") -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body))
    )


def test_http_error_message_never_carries_the_key():
    s = Settings(fmp_api_key=KEY, http_max_retries=1, http_backoff_seconds=0)
    with pytest.raises(SourceUnavailableError) as err:
        request_text(
            f"https://fmp.test/stable/x?apikey={KEY}",
            settings=s,
            client=_client(401, f"Invalid API KEY {KEY}"),
        )
    assert KEY not in str(err.value)


def test_http_retry_exhaustion_message_never_carries_the_key():
    s = Settings(fmp_api_key=KEY, http_max_retries=2, http_backoff_seconds=0)

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(SourceUnavailableError) as err:
        request_text(
            "https://fmp.test/stable/x",
            params={"apikey": KEY},
            settings=s,
            client=httpx.Client(transport=httpx.MockTransport(boom)),
        )
    assert KEY not in str(err.value)


def test_source_failure_is_redacted_in_log_result_and_database(caplog):
    from whale_agent.ingestion.registry import SourceSpec
    from whale_agent.jobs.pipeline import collect_events
    from whale_agent.storage.db import Store

    def run():
        raise SourceUnavailableError(f"GET https://fmp.test/x?apikey={KEY} -> HTTP 500")

    spec = SourceSpec(name="fmp_insider", label="FMP", enabled=True, run=run)
    store = Store(":memory:")
    try:
        with caplog.at_level("INFO"):
            result = collect_events([spec], store)
        dump = "\n".join(store.conn.iterdump())
    finally:
        store.close()
    assert KEY not in caplog.text
    assert KEY not in result.failures["fmp_insider"]
    assert KEY not in dump


def test_jsonl_log_redacts_on_write(tmp_path):
    from whale_agent.monitoring.jsonl_log import JsonlLog

    path = tmp_path / "quarantine.jsonl"
    JsonlLog(path).append({"reason": f"bad row from https://fmp.test/x?apikey={KEY}"})
    assert KEY not in path.read_text()
