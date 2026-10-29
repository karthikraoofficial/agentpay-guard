"""FastAPI entry point. OpenAPI docs are served at /docs."""

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine

from adapters.postgres.store import PostgresStore
from adapters.system_clock import SystemClock
from api import errors
from api.deps import Store
from api.routes import mandates, parties
from core.policy.packs import load_active_pack
from core.ports import Clock
from core.settings import Settings, get_settings


class Health(BaseModel):
    status: str
    service: str
    version: str


def create_app(
    settings: Settings, *, store: Store | None = None, clock: Clock | None = None
) -> FastAPI:
    """Build the app from explicit settings, so tests need no global state. Without a
    store, it uses Postgres at `settings.database_url` (connecting on first use).

    Raises PackError if the active Policy pack doesn't match its Manifest hash, so an
    edited pack can never decide silently.
    """
    app = FastAPI(title="AgentPay Guard", version=settings.app_version)
    app.state.policy_pack = load_active_pack(settings.policy_packs_dir, "default")
    app.state.store = store or PostgresStore(
        create_engine(str(settings.database_url), pool_pre_ping=True)
    )
    app.state.clock = clock or SystemClock()
    errors.install(app)
    app.include_router(parties.router)
    app.include_router(mandates.router)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> Health:
        """Liveness only: no Postgres or Temporal call, so a dependency blip never
        restarts the API."""
        return Health(status="ok", service=settings.app_name, version=settings.app_version)

    return app


def asgi_app() -> FastAPI:
    """ASGI factory for uvicorn: `uvicorn api.main:asgi_app --factory`."""
    return create_app(get_settings())
