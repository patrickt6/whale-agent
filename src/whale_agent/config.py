"""Runtime settings, thresholds, and secrets for whale-agent.

Reads from environment variables (see `.env.example`). Every field has a default that
lets the app boot with nothing configured at all: the deterministic US core plus the
keyless free sources run out of the box, and every paid or key-gated source stays off
until its key is present.

The `*_enabled` properties are the switch that keeps paid vendors optional: a source is
used only when it is both explicitly enabled (default true) and actually holds the
credential it needs. So a blank `FMP_API_KEY=` means "FMP is off" with no code change
and no crash.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from whale_agent.errors import NotConfiguredError


def load_env_file(path: str = ".env") -> int:
    """Load `KEY=value` lines from a .env file into the environment.

    Hand-rolled rather than depending on python-dotenv: this is thirty lines and the
    dependency buys nothing else. Real environment variables always win, so a value
    exported in the shell or set by a CI secret is never overwritten by the file.

    Returns the number of variables set, so the CLI can say whether it found anything.
    """
    from pathlib import Path

    file = Path(path)
    if not file.is_file():
        return 0
    count = 0
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip an inline comment, but only when it is clearly separated, so a value
        # containing a '#' survives.
        value = value.split("  #", 1)[0].strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = True) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env(name)
    if not raw:
        return list(default) if default else []
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable settings snapshot, built from the environment."""

    # -- core ------------------------------------------------------------------
    sec_user_agent: str = "whale-agent contact@example.com"
    db_path: str = "whale.db"
    database_url: str = ""  # Postgres DSN; SQLite (db_path) is used when blank
    threshold_usd: float = 5_000_000.0
    near_threshold_usd: float = 4_500_000.0
    instant_usd: float = 100_000_000.0
    digest_url: str = ""  # optional link included in instant alerts

    # -- source selection ------------------------------------------------------
    # Blank `sources` = "every registered source that is configured". Otherwise an
    # explicit allowlist of adapter names (see ingestion/registry.py).
    sources: list[str] = field(default_factory=list)

    # -- profile filters ---------------------------------------------------------
    # Deterministic post-collection narrowing (see profiles/README comments in each
    # shipped .toml). Empty means "no restriction", so the default profile is a no-op
    # here and every value flows straight through, unchanged from today's behaviour.
    filter_jurisdictions: list[str] = field(default_factory=list)
    filter_event_types: list[str] = field(default_factory=list)
    filter_filer_roles: list[str] = field(default_factory=list)
    # Roles to drop even when filter_filer_roles would otherwise keep them -- the
    # "exclude passive/index filers" knob a profile can set independently of an allowlist.
    filter_exclude_roles: list[str] = field(default_factory=list)
    enable_fmp: bool = True
    enable_quiver: bool = True
    enable_taiwan: bool = True
    enable_japan: bool = True
    enable_arkham: bool = True
    enable_whale_alert: bool = True
    enable_dataroma: bool = True
    enable_usaspending: bool = True
    enable_senate_lda: bool = True
    enable_treasury: bool = True
    enable_fred: bool = True

    # -- paid vendors ----------------------------------------------------------
    fmp_api_key: str = ""
    fmp_base_url: str = "https://financialmodelingprep.com"
    quiver_api_key: str = ""
    quiver_base_url: str = "https://api.quiverquant.com"

    # -- free / low-cost sources -----------------------------------------------
    taiwan_mops_user_agent: str = "whale-agent contact@example.com"
    taiwan_base_url: str = "https://openapi.twse.com.tw/v1"
    japan_edinet_api_key: str = ""  # EDINET requires a free registered key
    japan_edinet_base_url: str = "https://api.edinet-fsa.go.jp/api/v2"
    arkham_api_key: str = ""  # Arkham free tier still issues a key
    arkham_base_url: str = "https://api.arkm.com"
    whale_alert_api_key: str = ""  # free tier key
    whale_alert_base_url: str = "https://api.whale-alert.io/v1"
    whale_alert_min_usd: float = 5_000_000.0
    dataroma_base_url: str = "https://www.dataroma.com"
    dataroma_user_agent: str = "whale-agent contact@example.com"

    # -- US government primary sources ------------------------------------------
    # All keyless except FRED. The LDA host is lda.senate.gov; the newer lda.gov
    # answers 403 to programmatic clients.
    usaspending_base_url: str = "https://api.usaspending.gov"
    senate_lda_base_url: str = "https://lda.senate.gov/api/v1"
    senate_lda_page_delay_seconds: float = 1.0  # no published rate limit, so be polite
    treasury_base_url: str = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
    fred_api_key: str = ""  # free, but we do not have one yet
    fred_base_url: str = "https://api.stlouisfed.org/fred"

    # -- LLM prose layer -------------------------------------------------------
    llm_provider: str = "gemini"  # "gemini" | "anthropic" | "none"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4000

    # -- delivery --------------------------------------------------------------
    email_provider: str = "smtp"  # "smtp" | "resend" | "none"
    email_to: str = ""
    email_from: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""  # Gmail App Password, not the account password
    smtp_use_tls: bool = True
    resend_api_key: str = ""
    resend_base_url: str = "https://api.resend.com"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_to_number: str = ""
    slack_webhook_url: str = ""

    # -- monitoring ------------------------------------------------------------
    operator_alert_email: str = ""
    digest_deadline_hour_utc: int = 13

    # -- HTTP behaviour --------------------------------------------------------
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 3
    http_backoff_seconds: float = 1.0

    @classmethod
    def from_env(cls, base: Settings | None = None) -> Settings:
        """Build settings from the environment, layered on top of `base`.

        `base` is the fallback every `_env*` lookup falls back to when its variable is
        unset -- normally the dataclass defaults, but the profile loader passes a
        profile-derived `Settings` here instead. That is the whole precedence mechanism:
        environment variables only ever override what is explicitly set, so a profile
        value survives untouched unless its variable is actually present, and plain
        `from_env()` with no base is unchanged from before this existed.
        """
        d = base or cls()  # fallback for each getenv
        return cls(
            sec_user_agent=_env("WHALE_SEC_USER_AGENT", d.sec_user_agent),
            db_path=_env("WHALE_DB_PATH", d.db_path),
            database_url=_env("DATABASE_URL", d.database_url),
            threshold_usd=_env_float("WHALE_THRESHOLD_USD", d.threshold_usd),
            near_threshold_usd=_env_float("WHALE_NEAR_THRESHOLD_USD", d.near_threshold_usd),
            instant_usd=_env_float("WHALE_INSTANT_USD", d.instant_usd),
            digest_url=_env("WHALE_DIGEST_URL", d.digest_url),
            sources=_env_list("WHALE_SOURCES", d.sources),
            filter_jurisdictions=_env_list(
                "WHALE_FILTER_JURISDICTIONS", d.filter_jurisdictions
            ),
            filter_event_types=_env_list("WHALE_FILTER_EVENT_TYPES", d.filter_event_types),
            filter_filer_roles=_env_list("WHALE_FILTER_FILER_ROLES", d.filter_filer_roles),
            filter_exclude_roles=_env_list(
                "WHALE_FILTER_EXCLUDE_ROLES", d.filter_exclude_roles
            ),
            enable_fmp=_env_bool("WHALE_ENABLE_FMP", d.enable_fmp),
            enable_quiver=_env_bool("WHALE_ENABLE_QUIVER", d.enable_quiver),
            enable_taiwan=_env_bool("WHALE_ENABLE_TAIWAN", d.enable_taiwan),
            enable_japan=_env_bool("WHALE_ENABLE_JAPAN", d.enable_japan),
            enable_arkham=_env_bool("WHALE_ENABLE_ARKHAM", d.enable_arkham),
            enable_whale_alert=_env_bool("WHALE_ENABLE_WHALE_ALERT", d.enable_whale_alert),
            enable_dataroma=_env_bool("WHALE_ENABLE_DATAROMA", d.enable_dataroma),
            enable_usaspending=_env_bool("WHALE_ENABLE_USASPENDING", d.enable_usaspending),
            enable_senate_lda=_env_bool("WHALE_ENABLE_SENATE_LDA", d.enable_senate_lda),
            enable_treasury=_env_bool("WHALE_ENABLE_TREASURY", d.enable_treasury),
            enable_fred=_env_bool("WHALE_ENABLE_FRED", d.enable_fred),
            fmp_api_key=_env("FMP_API_KEY", d.fmp_api_key),
            fmp_base_url=_env("FMP_BASE_URL", d.fmp_base_url),
            quiver_api_key=_env("QUIVER_QUANT_API_KEY", d.quiver_api_key),
            quiver_base_url=_env("QUIVER_BASE_URL", d.quiver_base_url),
            taiwan_mops_user_agent=_env("TAIWAN_MOPS_USER_AGENT", d.taiwan_mops_user_agent),
            taiwan_base_url=_env("TAIWAN_BASE_URL", d.taiwan_base_url),
            japan_edinet_api_key=_env("JAPAN_EDINET_API_KEY", d.japan_edinet_api_key),
            japan_edinet_base_url=_env("JAPAN_EDINET_BASE_URL", d.japan_edinet_base_url),
            arkham_api_key=_env("ARKHAM_API_KEY", d.arkham_api_key),
            arkham_base_url=_env("ARKHAM_BASE_URL", d.arkham_base_url),
            whale_alert_api_key=_env("WHALE_ALERT_API_KEY", d.whale_alert_api_key),
            whale_alert_base_url=_env("WHALE_ALERT_BASE_URL", d.whale_alert_base_url),
            whale_alert_min_usd=_env_float("WHALE_ALERT_MIN_USD", d.whale_alert_min_usd),
            dataroma_base_url=_env("DATAROMA_BASE_URL", d.dataroma_base_url),
            dataroma_user_agent=_env("DATAROMA_USER_AGENT", d.dataroma_user_agent),
            usaspending_base_url=_env("USASPENDING_BASE_URL", d.usaspending_base_url),
            senate_lda_base_url=_env("SENATE_LDA_BASE_URL", d.senate_lda_base_url),
            senate_lda_page_delay_seconds=_env_float(
                "SENATE_LDA_PAGE_DELAY", d.senate_lda_page_delay_seconds
            ),
            treasury_base_url=_env("TREASURY_BASE_URL", d.treasury_base_url),
            fred_api_key=_env("FRED_API_KEY", d.fred_api_key),
            fred_base_url=_env("FRED_BASE_URL", d.fred_base_url),
            llm_provider=_env("WHALE_LLM_PROVIDER", d.llm_provider).lower(),
            gemini_api_key=_env("GEMINI_API_KEY", d.gemini_api_key),
            gemini_model=_env("GEMINI_MODEL", d.gemini_model),
            gemini_base_url=_env("GEMINI_BASE_URL", d.gemini_base_url),
            anthropic_api_key=_env("ANTHROPIC_API_KEY", d.anthropic_api_key),
            anthropic_model=_env("ANTHROPIC_MODEL", d.anthropic_model),
            llm_temperature=_env_float("WHALE_LLM_TEMPERATURE", d.llm_temperature),
            llm_max_tokens=_env_int("WHALE_LLM_MAX_TOKENS", d.llm_max_tokens),
            email_provider=_env("WHALE_EMAIL_PROVIDER", d.email_provider).lower(),
            email_to=_env("WHALE_EMAIL_TO", d.email_to),
            email_from=_env("WHALE_EMAIL_FROM", d.email_from),
            smtp_host=_env("SMTP_HOST", d.smtp_host),
            smtp_port=_env_int("SMTP_PORT", d.smtp_port),
            smtp_username=_env("SMTP_USERNAME", d.smtp_username),
            smtp_password=_env("SMTP_PASSWORD", d.smtp_password),
            smtp_use_tls=_env_bool("SMTP_USE_TLS", d.smtp_use_tls),
            resend_api_key=_env("RESEND_API_KEY", d.resend_api_key),
            resend_base_url=_env("RESEND_BASE_URL", d.resend_base_url),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", d.telegram_bot_token),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID", d.telegram_chat_id),
            twilio_account_sid=_env("TWILIO_ACCOUNT_SID", d.twilio_account_sid),
            twilio_auth_token=_env("TWILIO_AUTH_TOKEN", d.twilio_auth_token),
            twilio_from_number=_env("TWILIO_FROM_NUMBER", d.twilio_from_number),
            twilio_to_number=_env("TWILIO_TO_NUMBER", d.twilio_to_number),
            slack_webhook_url=_env("SLACK_WEBHOOK_URL", d.slack_webhook_url),
            operator_alert_email=_env("OPERATOR_ALERT_EMAIL", d.operator_alert_email),
            digest_deadline_hour_utc=_env_int(
                "DIGEST_DEADLINE_HOUR_UTC", d.digest_deadline_hour_utc
            ),
            http_timeout_seconds=_env_float("WHALE_HTTP_TIMEOUT", d.http_timeout_seconds),
            http_max_retries=_env_int("WHALE_HTTP_MAX_RETRIES", d.http_max_retries),
            http_backoff_seconds=_env_float("WHALE_HTTP_BACKOFF", d.http_backoff_seconds),
        )

    # -- "is this source usable today?" ----------------------------------------
    # Each is enabled-flag AND credential-present. A source with no credential is
    # silently skipped, never a crash, so the digest still ships.

    @property
    def fmp_enabled(self) -> bool:
        return self.enable_fmp and bool(self.fmp_api_key)

    @property
    def quiver_enabled(self) -> bool:
        return self.enable_quiver and bool(self.quiver_api_key)

    @property
    def taiwan_enabled(self) -> bool:
        return self.enable_taiwan  # TWSE OpenAPI is keyless

    @property
    def japan_enabled(self) -> bool:
        return self.enable_japan and bool(self.japan_edinet_api_key)

    @property
    def arkham_enabled(self) -> bool:
        return self.enable_arkham and bool(self.arkham_api_key)

    @property
    def whale_alert_enabled(self) -> bool:
        return self.enable_whale_alert and bool(self.whale_alert_api_key)

    @property
    def dataroma_enabled(self) -> bool:
        return self.enable_dataroma  # public HTML, no key

    @property
    def usaspending_enabled(self) -> bool:
        return self.enable_usaspending  # USASpending.gov is keyless

    @property
    def senate_lda_enabled(self) -> bool:
        return self.enable_senate_lda  # Senate LDA API is keyless

    @property
    def treasury_enabled(self) -> bool:
        return self.enable_treasury  # Treasury Fiscal Data is keyless

    @property
    def fred_enabled(self) -> bool:
        return self.enable_fred and bool(self.fred_api_key)

    @property
    def email_enabled(self) -> bool:
        if self.email_provider == "none" or not self.email_to:
            return False
        if self.email_provider == "smtp":
            return bool(self.smtp_username and self.smtp_password)
        if self.email_provider == "resend":
            return bool(self.resend_api_key)
        return False

    @property
    def llm_enabled(self) -> bool:
        if self.llm_provider == "gemini":
            return bool(self.gemini_api_key)
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return False

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def twilio_enabled(self) -> bool:
        return bool(
            self.twilio_account_sid
            and self.twilio_auth_token
            and self.twilio_from_number
            and self.twilio_to_number
        )

    def require(self, attr: str) -> str:
        """Return a credential, or raise a specific `NotConfiguredError`.

        Adapters call this instead of reading the field directly so a missing key
        fails with the env-var name rather than as an opaque 401 deep in a request.
        """
        value = getattr(self, attr, "")
        if not value:
            raise NotConfiguredError(
                f"Settings.{attr} is not set. See .env.example "
                f"for how to obtain and configure it."
            )
        return str(value)


def get_settings() -> Settings:
    return Settings.from_env()
