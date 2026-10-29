"""FastAPI entry point. Routers for /v1/mandates, /v1/decisions and /v1/disputes arrive with
their PRDs; Phase 0 ships only the liveness check. OpenAPI docs are served at /docs."""

from fastapi import FastAPI
from pydantic import BaseModel

from core.settings import Settings, get_settings


class Health(BaseModel):
    status: str
    service: str
    version: str


def create_app(settings: Settings) -> FastAPI:
    """Build the app from explicit settings, so tests need no global state."""
    app = FastAPI(title="AgentPay Guard", version=settings.app_version)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> Health:
        """Liveness only: no Postgres or Temporal call, so a dependency blip never
        restarts the API."""
        return Health(status="ok", service=settings.app_name, version=settings.app_version)

    return app


def asgi_app() -> FastAPI:
    """ASGI factory for uvicorn: `uvicorn api.main:asgi_app --factory`."""
    return create_app(get_settings())
