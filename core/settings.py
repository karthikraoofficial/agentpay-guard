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

from pydantic import AnyUrl, Field, SecretStr, model_validator
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
    app_version: str = "0.2.0"
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

    # The consent screen's origin: Consent links point here, and only this origin may
    # call the Consent link endpoints from a browser (CORS).
    web_origin: str = Field(default="http://localhost:3000", pattern=r"^https?://[^/\s]+$")

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
    # The one Razorpay test-mode account every Tenant charges through (ADR-0012). All three
    # are required with AGENTPAY_RAIL=razorpay, and only a test key is ever accepted.
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    # Signs Razorpay's webhooks (#81). Without it every inbound rail webhook is refused.
    razorpay_webhook_secret: SecretStr | None = None
    # A registered test card, for the rail contract tests only. Printed by
    # `python -m adapters.razorpay.wizard`; Razorpay expires it within about a day.
    razorpay_test_customer_ref: str | None = None
    razorpay_test_token_ref: str | None = None

    @model_validator(mode="after")
    def _razorpay_test_mode_only(self) -> "Settings":
        if self.razorpay_key_id is not None and not self.razorpay_key_id.startswith("rzp_test_"):
            raise ValueError("AGENTPAY_RAZORPAY_KEY_ID must be a test key (rzp_test_...)")
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


@lru_cache
def get_settings() -> Settings:
    """Load settings from the environment once. Raises ValidationError if invalid."""
    return Settings()  # required fields come from the environment
