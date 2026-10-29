"""FastAPI entry point. Routers for /v1/mandates, /v1/decisions and /v1/disputes arrive with
their PRDs; Phase 0 ships only the health check."""

from fastapi import FastAPI

from core.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AgentPay Guard", version="0.0.1")

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": settings.service_name, "env": settings.env}

    return app


app = create_app()
