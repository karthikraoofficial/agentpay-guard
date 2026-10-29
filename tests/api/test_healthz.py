from fastapi.testclient import TestClient

from api.main import create_app
from core.settings import Settings


def test_healthz_returns_status_service_and_version_from_settings(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "agentpay-guard-test", "version": "9.9.9"}


def test_openapi_docs_are_served(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
