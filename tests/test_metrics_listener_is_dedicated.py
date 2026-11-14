"""Prometheus series are served on a DEDICATED metrics-only port, never the API port.

Scrape admission is PORT-scoped (Prometheus is outside the ambient mesh), so a
``/metrics`` route on the API port could only be scraped by opening the whole API
to the pod CIDR. See ``src/metrics_server.py``.
"""

from __future__ import annotations

import logging
import socket
import urllib.request

import httpx
import pytest
from fastapi import FastAPI

from metrics_server import start_metrics_server, stop_metrics_server

COUNTER = "requirements_management_effective_tenant_delegation_total"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_app() -> FastAPI:
    return FastAPI()


def _assert_not_listening(port: int) -> None:
    """Stopped means the socket is CLOSED, not merely forgotten on app.state.

    A connect probe, not a re-bind: the scrape leaves the server side of that
    connection in TIME_WAIT, which makes a re-bind fail on macOS even though
    nothing is listening.
    """
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def _scrape(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        assert resp.status == 200
        return resp.read().decode()


def test_counter_is_served_on_the_dedicated_port() -> None:
    import metrics  # noqa: F401  (registers the counters on REGISTRY)

    app = _fake_app()
    port = _free_port()
    start_metrics_server(app, port)
    try:
        assert app.state.metrics_httpd is not None
        body = _scrape(port)
        # The seeded zero series, not just the HELP line: proves the real
        # REGISTRY (with materialised children) is what this port serves.
        assert f"# TYPE {COUNTER} counter" in body
        assert f'{COUNTER}{{route_group="requirements"}} ' in body
    finally:
        stop_metrics_server(app)

    _assert_not_listening(port)


async def test_api_port_serves_no_metrics() -> None:
    from main import app

    mounted = [getattr(r, "path", None) for r in app.routes]
    assert not any(p and p.rstrip("/") == "/metrics" for p in mounted), mounted

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/metrics", "/metrics/"):
            resp = await client.get(path)
            # Default-deny: an unknown path is a 401 before routing would 404 it.
            assert resp.status_code == 401, (path, resp.status_code)
            assert COUNTER not in resp.text


def test_port_in_use_is_fail_soft(caplog: pytest.LogCaptureFixture) -> None:
    app = _fake_app()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        with caplog.at_level(logging.WARNING, logger="metrics_server"):
            start_metrics_server(app, port)  # must not raise
    assert not hasattr(app.state, "metrics_httpd")
    assert any("failed to bind" in r.getMessage() for r in caplog.records)
    stop_metrics_server(app)  # no-op, must not raise


def test_metrics_port_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from settings import Settings

    monkeypatch.delenv("METRICS_PORT", raising=False)
    assert Settings().metrics_port == 9921
    assert Settings().metrics_port != 8921  # never the API port
    monkeypatch.setenv("METRICS_PORT", "9999")
    assert Settings().metrics_port == 9999


async def test_lifespan_starts_and_stops_the_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    import settings as settings_module
    from lifespan import lifespan

    port = _free_port()
    monkeypatch.setenv("METRICS_PORT", str(port))
    settings_module.get_settings.cache_clear()

    app = _fake_app()
    try:
        # Telemetry is an external OTEL collector; the DB check runs against the real test DB.
        with patch("lifespan.setup_telemetry", return_value=False):
            async with lifespan(app):
                assert COUNTER in _scrape(port)
        assert app.state.metrics_httpd is None
        _assert_not_listening(port)
    finally:
        settings_module.get_settings.cache_clear()
