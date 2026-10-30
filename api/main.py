"""FastAPI entry point. OpenAPI docs are served at /docs."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine

from adapters.claude.gateway import ClaudeGateway
from adapters.memory.model_gateway import UnconfiguredGateway
from adapters.otel.telemetry import OtelTelemetry
from adapters.postgres.store import PostgresStore
from adapters.system_clock import SystemClock
from api import errors
from api.deps import Store
from api.routes import consent_requests, decisions, intents, mandates, parties
from core.policy.packs import load_active_pack
from core.ports import Clock, ModelGateway, Telemetry, Workflow
from core.settings import Settings, get_settings
from workflows.client import TemporalWorkflow


class Health(BaseModel):
    status: str
    service: str
    version: str


def create_app(
    settings: Settings,
    *,
    store: Store | None = None,
    clock: Clock | None = None,
    telemetry: Telemetry | None = None,
    gateway: ModelGateway | None = None,
    workflow: Workflow | None = None,
) -> FastAPI:
    """Build the app from explicit settings, so tests need no global state. Without a
    store, it uses Postgres at `settings.database_url` (connecting on first use).

    Raises PackError if the active Policy pack doesn't match its Manifest hash, so an
    edited pack can never decide silently.
    """
    otel = None if telemetry is not None else OtelTelemetry.from_settings(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if otel is not None:
            otel.shutdown()

    app = FastAPI(title="AgentPay Guard", version=settings.app_version, lifespan=lifespan)
    app.state.policy_pack = load_active_pack(settings.policy_packs_dir, "default")
    app.state.store = store or PostgresStore(
        create_engine(str(settings.database_url), pool_pre_ping=True)
    )
    app.state.clock = clock or SystemClock()
    app.state.telemetry = telemetry or otel
    app.state.settings = settings
    app.state.gateway = gateway or _default_gateway(settings, app.state.telemetry)
    # Only resolutions signal from here (best effort); the worker's relay starts workflows.
    app.state.workflow = workflow or TemporalWorkflow.from_settings(settings)
    errors.install(app)
    app.include_router(parties.router)
    app.include_router(mandates.router)
    app.include_router(decisions.router)
    app.include_router(intents.router)
    app.include_router(consent_requests.router)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> Health:
        """Liveness only: no Postgres or Temporal call, so a dependency blip never
        restarts the API."""
        return Health(status="ok", service=settings.app_name, version=settings.app_version)

    return app


def _default_gateway(settings: Settings, telemetry: Telemetry) -> ModelGateway:
    """Claude when a key is configured. Settings refuse to load without one outside the
    test env while the Intent check is on, so the fallback only serves tests."""
    if settings.anthropic_api_key is None:
        return UnconfiguredGateway()
    return ClaudeGateway(settings.anthropic_api_key, telemetry)


def asgi_app() -> FastAPI:
    """ASGI factory for uvicorn: `uvicorn api.main:asgi_app --factory`."""
    return create_app(get_settings())
