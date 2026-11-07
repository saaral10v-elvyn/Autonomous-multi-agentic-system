"""Health endpoint smoke test."""

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_returns_ok() -> None:
    """The health endpoint is available without database connectivity."""
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
