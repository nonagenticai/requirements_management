"""Integration tests for requirements-api health endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestHealthCheck:
    def test_health_returns_200(self, req_client):
        r = req_client.get("/health")
        assert r.status_code == 200, r.text

    def test_health_has_status(self, req_client):
        data = req_client.get("/health").json()
        assert data["status"] == "ok"

    def test_health_has_service_name(self, req_client):
        data = req_client.get("/health").json()
        assert data["service"] == "requirements-api"


class TestLiveness:
    def test_live_returns_200(self, req_client):
        r = req_client.get("/health/live")
        assert r.status_code == 200, r.text

    def test_live_returns_alive(self, req_client):
        data = req_client.get("/health/live").json()
        assert data["status"] == "alive"


class TestReadiness:
    def test_ready_returns_200(self, req_client):
        r = req_client.get("/health/ready")
        assert r.status_code == 200, r.text

    def test_ready_has_database_check(self, req_client):
        data = req_client.get("/health/ready").json()
        assert data["status"] == "ready"
        assert data["checks"]["database"] is True
