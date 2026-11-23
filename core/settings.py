"""The single place configuration is read (CLAUDE.md non-negotiable 9).

Values come from the process environment or a local `.env` file, prefixed
`AGENTPAY_`. Every other module receives a `Settings` object by injection
(see `api.main.create_app`) or through `get_settings()`, and never reads the
environment itself. Required values have no default, so a missing or
malformed one fails at startup rather than at the first request.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, AnyUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.domain.rails import RailName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_name: str = "agentpay-guard"
    app_version: str = "1.2.0"
    env: Literal["local", "test", "staging", "production"]

    database_url: AnyUrl
    temporal_address: str = Field(pattern=r"^[\w.\-]+:\d+$")
    temporal_namespace: str = "default"
    # The task queue the consent workflow and its activities run on.
    temporal_task_queue: str = "agentpay-consent"
    otel_exporter_endpoint: AnyUrl
    # "otlp" exports spans and metrics to otel_exporter_endpoint; "none" builds them but
    # exports nothing, for tests and CI where no collector runs.
    telemetry_exporter: Literal["otlp", "none"] = "otlp"

    # Released Policy packs and their Manifest. The active `default` pack is loaded and
    # hash-checked at startup; a mismatch stops the app (ADR-0002).
    policy_packs_dir: Path = Path("policy_packs")

    # SecretStr keeps the value out of repr and logs. Without a key every Intent check
    # fails closed to an unknown verdict.
    anthropic_api_key: SecretStr | None = None

    # The Intent check (POST /v1/intents). Off: every check is unknown, no model call.
    intent_check_enabled: bool = True
    intent_check_model: str = "claude-haiku-5-5"
    # Measured ~900 ms round trip from a dev machine; the check is off the decision path.
    intent_check_timeout_ms: int = Field(default=2500, gt=0, le=30_000)

    # The dispute narrative: one model call, and at most one retry, per Evidence pack
    # version, off every request path. Without a key, or on failure, the narrative is
    # written by code. Off: always written by code, no model call.
    dispute_narrative_enabled: bool = True
    # Chosen by the Model comparison rule (ADR-0030).
    dispute_narrative_model: str = "claude-haiku-5-5"
    dispute_narrative_timeout_ms: int = Field(default=45_000, gt=0, le=120_000)

    # The Model spend cap (ADR-0020): model calls each Tenant may make per UTC day, per
    # purpose. Past it the model isn't called and the caller fails closed (Intent check
    # unknown, narrative written by code). 0 stops that purpose's calls.
    intent_check_daily_cap: int = Field(default=200, ge=0)
    dispute_narrative_daily_cap: int = Field(default=20, ge=0)

    # How long a Review link works, at most: never past the Dispute's respond_by.
    review_link_ttl_seconds: int = Field(default=72 * 3600, gt=0, le=30 * 86400)

    # The consent screen's origin: Consent links point here, and only this origin may
    # call the Consent link endpoints from a browser (CORS).
    web_origin: str = Field(default="http://localhost:3000", pattern=r"^https?://[^/\s]+$")

    # The most Evidence pack versions one Dispute may have: each Regenerate is a new one,
    # and a paid model call once the narrative is model-written.
    dispute_pack_versions_max: int = Field(default=3, ge=1, le=10)

    # The outbox relay: how often it looks for undelivered events (ADR-0009).
    outbox_poll_interval_ms: int = Field(default=1000, gt=0, le=60_000)

    # The PaymentRail that Payments go through. "mock" settles in-process and needs no
    # account, so tests, evals and a fresh clone run without Razorpay (ADR-0012, ADR-0013).
    rail: RailName = "mock"
    # The reconciliation sweep (#82): every `interval`, ask the rail about Payments still
    # scheduled `after` seconds after they were made.
    reconcile_interval_seconds: int = Field(default=300, gt=0)
    # At least a minute, so the sweep never fails a Payment whose rail call is in flight.
    reconcile_after_seconds: int = Field(default=600, ge=60)
    # The circuit breaker on the API's rail calls (#226, ADR-0025): after this many
    # unknown outcomes in a row, payments and refunds answer 503 RAIL_UNAVAILABLE for
    # rail_breaker_open_seconds, then one request is let through to try the rail.
    rail_breaker_failures: int = Field(default=5, ge=1)
    rail_breaker_open_seconds: int = Field(default=30, gt=0, le=3600)
    # Mock settle, for the hosted demo only (ADR-0019): when set, the worker's sweep
    # captures every debited mock Payment older than this many seconds, so a demo Payment can be
    # disputed. Unset, mock Payments stay scheduled. Refused with any real rail.
    mock_settle_after_seconds: int | None = Field(default=None, gt=0)
    # The mock rail's Pre-debit notice period, for the hosted demo only (#280): when set,
    # the API's mock rail books each charge with `debit_after` this far ahead, and the
    # worker's debit step debits it once the period is over, as a card debit after a
    # Pre-debit notice (ADR-0017). Needs mock settle; refused with any real rail.
    mock_notice_seconds: int | None = Field(default=None, gt=0, le=86400)
    # The one Tenant whose timers Demo controls may end early (ADR-0032): its id, as
    # `api.admin create-tenant` printed it. Unset, the control endpoints don't exist and
    # no deadline check reads a Demo control. Refused with any real rail.
    demo_controls_tenant_id: str | None = Field(default=None, min_length=1)
    # The Demo control's simulated rail outage (#281, ADR-0032): how long it lasts, and how
    # long from its start before another can begin, because it affects every visitor.
    demo_rail_outage_seconds: int = Field(default=60, gt=0, le=600)
    demo_rail_cooldown_seconds: int = Field(default=300, gt=0, le=3600)
    # The one Razorpay test-mode account every Tenant charges through (ADR-0012). All three
    # are required with AGENTPAY_RAIL=razorpay, and only a test key is ever accepted.
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    # Signs Razorpay's webhooks (#81). Without it every inbound rail webhook is refused.
    razorpay_webhook_secret: SecretStr | None = None
    # Encrypts Tenant webhook signing secrets at rest (ADR-0015): a Fernet key, made with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    # Without it no webhook endpoint can be registered and no Tenant webhook is sent.
    webhook_encryption_key: SecretStr | None = None
    # A registered test card, for the rail contract tests only. Printed by
    # `python -m adapters.razorpay.wizard`; Razorpay expires it within about a day.
    razorpay_test_customer_ref: str | None = None
    razorpay_test_token_ref: str | None = None
    # The Demo Tenant's two keys, as `api.admin create-tenant` printed them, for the demo
    # CLI (`python -m demo`) only. The API never reads them.
    demo_agent_key: SecretStr | None = None
    demo_tenant_key: SecretStr | None = None
    # Grafana Cloud, for pushing the metric alert rules from the operator's machine
    # (`python -m adapters.grafana.alerts`, #228) only. The app never reads them; the VM's
    # collector gets its own metrics key by compose.
    grafana_url: AnyUrl | None = None
    grafana_api_token: SecretStr | None = None
    grafana_datasource_uid: str = "grafanacloud-prom"
    alert_email: str | None = None

    @model_validator(mode="after")
    def _razorpay_test_mode_only(self) -> "Settings":
        if self.razorpay_key_id is not None and not self.razorpay_key_id.startswith("rzp_test_"):
            raise ValueError("AGENTPAY_RAZORPAY_KEY_ID must be a test key (rzp_test_...)")
        if self.rail != "mock" and self.mock_settle_after_seconds is not None:
            raise ValueError(
                "AGENTPAY_MOCK_SETTLE_AFTER_SECONDS is for the mock rail only; "
                f"unset it with AGENTPAY_RAIL={self.rail}"
            )
        if self.rail != "mock" and self.demo_controls_tenant_id is not None:
            raise ValueError(
                "AGENTPAY_DEMO_CONTROLS_TENANT_ID is for the mock rail only; "
                f"unset it with AGENTPAY_RAIL={self.rail}"
            )
        if self.demo_rail_cooldown_seconds < self.demo_rail_outage_seconds:
            raise ValueError(
                "AGENTPAY_DEMO_RAIL_COOLDOWN_SECONDS must be at least "
                "AGENTPAY_DEMO_RAIL_OUTAGE_SECONDS"
            )
        if self.mock_notice_seconds is not None and (
            self.rail != "mock" or self.mock_settle_after_seconds is None
        ):
            raise ValueError(
                "AGENTPAY_MOCK_NOTICE_SECONDS is for the mock rail with mock settle on: "
                "set AGENTPAY_MOCK_SETTLE_AFTER_SECONDS, and AGENTPAY_RAIL=mock"
            )
        if self.rail == "razorpay":
            missing = [
                f"AGENTPAY_{name.upper()}"
                for name in ("razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"AGENTPAY_RAIL=razorpay needs {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _key_when_checking(self) -> "Settings":
        if self.intent_check_enabled and self.env != "test" and self.anthropic_api_key is None:
            raise ValueError(
                "AGENTPAY_ANTHROPIC_API_KEY is required while the Intent check is enabled; "
                "set it or set AGENTPAY_INTENT_CHECK_ENABLED=false"
            )
        return self


class McpSettings(BaseSettings):
    """The MCP server's settings (#242), separate from the API's: the MCP process is a thin
    client over the HTTP API, so it never needs the Anthropic key, database or rail
    settings that `Settings` checks at startup."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTPAY_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Where the AgentPay Guard API runs, e.g. http://localhost:8000.
    api_base_url: AnyHttpUrl
    # An API key with exactly the `decide` scope; the server refuses to start with any
    # other (ADR-0011). Sent only as the Bearer token, never logged.
    api_key: SecretStr
    # Each API call's timeout. A Decision answers well inside a second.
    http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)


class DemoRunnerSettings(BaseSettings):
    """The Demo runner's settings (#273, ADR-0031), separate from the API's: the runner is
    a client of the HTTP API, so it never needs the database, rail or model settings that
    `Settings` checks at startup. It holds the Demo Tenant's two keys and refuses to start
    with a live one."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTPAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # The Demo Tenant's keys, as `api.admin create-tenant` printed them. Sent only as
    # Bearer tokens to the API, never to the browser, a Card or a log.
    demo_agent_key: SecretStr
    demo_tenant_key: SecretStr
    # Where the API runs, from the runner: http://api:8000 on the hosted compose network.
    demo_runner_api_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    # The web app's origin: the only one whose pages may call the runner (CORS).
    web_origin: str = Field(default="http://localhost:3000", pattern=r"^https?://[^/\s]+$")
    # Rate limits: runs one client IP may start a minute, and runs in progress at once.
    # Past either the runner answers 429 with Retry-After.
    demo_runner_runs_per_ip_per_minute: int = Field(default=5, ge=1)
    demo_runner_runs_in_progress_max: int = Field(default=20, ge=1)
    # How long a run is kept after it starts. A restart ends every run in progress.
    demo_runner_run_ttl_seconds: int = Field(default=1800, gt=0, le=6 * 3600)
    demo_runner_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("demo_agent_key", "demo_tenant_key")
    @classmethod
    def _test_keys_only(cls, key: SecretStr) -> SecretStr:
        if not key.get_secret_value().startswith("apg_test_"):
            raise ValueError(
                "the Demo runner takes only test keys (apg_test_...): the demo is public, "
                "so a live key must never be behind it"
            )
        return key


@lru_cache
def get_settings() -> Settings:
    """Load settings from the environment once. Raises ValidationError if invalid."""
    return Settings()  # required fields come from the environment
