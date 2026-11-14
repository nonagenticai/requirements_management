"""A DEDICATED plaintext Prometheus listener, on its own port.

⛔ WHY ``/metrics`` IS **NOT** ON THE API PORT (8921).

Prometheus runs outside the ambient mesh, and scrape admission (the port-level
PeerAuthentication relax + the ``allow-prometheus-scrape`` NetworkPolicy
``ipBlock``) is PORT-scoped, not path-scoped. Mounting ``/metrics`` on the
FastAPI app and relaxing ``:8921`` for the scrape would expose the whole
Requirements Management API's port to every pod in the cluster CIDR.

So this is the estate shape (``worker_management`` 8887→9887,
``inference-router`` 8150→9150, ``coding-security`` 8693→9693): a second, bare
WSGI server on a metrics-only port serving the same ``prometheus_client``
REGISTRY, with the API port left STRICT and serving no metrics at all.

⛔ THE AUTH EXEMPTION IS STRUCTURAL. This listener is a ``prometheus_client``
WSGI server, not the ASGI app, so ``AuthMiddleware`` never runs on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prometheus_client import start_http_server

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def start_metrics_server(app: FastAPI, port: int) -> None:
    """Bind the metrics-only listener and stash it on ``app.state``.

    Fail-soft on purpose: a metrics listener must never be the reason the
    service does not start. The realistic bind failure is a test harness or a
    sidecar already holding the port, and an unscraped service is strictly
    better than a dead one. The WARNING is the signal — pair it with an
    ``absent()`` alert in gitops, because a silent listener and a quiet service
    look identical in Prometheus.
    """
    try:
        httpd, _thread = start_http_server(port)
    except OSError as exc:
        logger.warning(
            "Metrics server failed to bind on port %s — this pod will export no series: %s",
            port,
            exc,
        )
        return
    app.state.metrics_httpd = httpd
    logger.info("Prometheus metrics listener started on port %s", port)


def stop_metrics_server(app: FastAPI) -> None:
    """Shut the listener down if one was started."""
    httpd = getattr(app.state, "metrics_httpd", None)
    if httpd is not None:
        httpd.shutdown()
        httpd.server_close()
        app.state.metrics_httpd = None
