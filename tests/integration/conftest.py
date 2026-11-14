"""Integration fixtures for the requirements-api HTTP surface.

Two modes, and the default is the one CI uses.

**In-process (default).** The suite drives ``main.app`` over
``httpx.ASGITransport``. A bare ``X-Tenant-Id: <uuid>`` is turned into a test
licence for that tenant by ``tests/conftest.py`` (RM admits no tokenless caller),
so no licence server is needed. That is the same mechanism the whole unit suite
uses — this file invents no new auth path.

**Live (opt-in).** Set ``REQUIREMENTS_URL`` *and* ``LICENSE_TOKEN`` to point at a
deployed instance. Both must be explicit: there is deliberately no default
token, because the one that used to live here was hardcoded and dead.

⛔ Why this file is shaped this way — measured 2026-09-04, ``main`` at ab51ac0.
Against the previous live-only client the suite reported **7 passed, 39 failed,
40 skipped** out of 86. Every one of the 39 failures and all 40 skips were the
same thing: ``403 {"error": "license_invalid", "reason": "token_invalid"}``. The
7 passes were ``test_health.py``, the only file whose endpoints are auth-exempt.
So **not one of the 86 had ever exercised a business endpoint**, and because
``addopts`` deselected the marker, nobody ever saw the 39 failures either.

⛔ A skip here is a lie. If a request does not do what it should, this suite must
FAIL and name the status and body. Setup helpers assert; they never skip. A
guard (``tests/test_integration_suite_can_fail.py``) enforces that.
"""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest

# Live mode is opt-in and needs BOTH. Absent -> in-process against main.app.
REQUIREMENTS_URL = os.environ.get("REQUIREMENTS_URL", "")
LICENSE_TOKEN = os.environ.get("LICENSE_TOKEN", "")
TENANT_ID = os.environ.get("TENANT_ID", "0195f48b-d000-7000-8000-000000000001")

LIVE_MODE = bool(REQUIREMENTS_URL and LICENSE_TOKEN)

if REQUIREMENTS_URL and not LICENSE_TOKEN:
    # Fail loudly rather than falling back. Silently running in-process while the
    # developer believes they tested the cluster is exactly the class of lie this
    # whole change exists to remove — an adversarial review set REQUIREMENTS_URL
    # alone and got a green "cluster" run it never made.
    raise RuntimeError(
        "REQUIREMENTS_URL is set but LICENSE_TOKEN is not, so live mode cannot "
        "authenticate. Set both to run against a deployed instance, or unset "
        "REQUIREMENTS_URL to run in-process. Refusing to silently run in-process "
        "and report it as a live result."
    )


def _uid():
    return uuid4().hex[:8]


# --------------------------------------------------------------------------- #
# Runtime backstop: in in-process mode, a skipped integration test is a defect.
#
# The static guard (tests/test_integration_suite_can_fail.py) walks the AST for
# `pytest.skip(...)`. An adversarial review defeated it in one line with a
# module-level `pytest.mark.skipif(True, ...)`, which is a MARK, not a call —
# and there will always be another spelling (`request.applymarker`,
# `unittest.SkipTest`, a `skipif` on a condition that is quietly always true).
#
# So the static walk is not the last line of defence. This is: whatever produced
# the skip, the run fails. It costs nothing when the suite is healthy, because a
# healthy in-process run skips nothing.
#
# LIVE_MODE is exempt on purpose — a live run against a deployed instance may
# legitimately skip, and that mode is opt-in and never used by CI.
# --------------------------------------------------------------------------- #
class _SyncASGIClient:
    """A sync facade over an async ASGI client, so the tests stay sync.

    ``httpx.ASGITransport`` is ``AsyncBaseTransport``-only, so a plain
    ``httpx.Client`` cannot drive it. Rather than rewrite 86 tests as ``async
    def``, every call is driven through ONE ``asyncio.Runner`` that lives for the
    whole session.

    The single runner is load-bearing, not tidiness: the app's psycopg pool binds
    its connections to the loop that created them. A fresh loop per request —
    which is what ``TestClient`` does outside a ``with`` block — would hand a live
    connection to a dead loop. One runner, one loop, one pool.
    """

    def __init__(self, app, headers):
        self._runner = asyncio.Runner()
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
            headers=headers,
            timeout=30.0,
        )

    def _call(self, method, url, **kwargs):
        return self._runner.run(self._client.request(method, url, **kwargs))

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)

    def patch(self, url, **kw):
        return self._call("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)

    def close(self):
        self._runner.run(self._client.aclose())
        self._runner.close()


def _is_integration(nodeid: str) -> bool:
    return str(nodeid).startswith("tests/integration/")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Turn a skipped integration test into a FAILED one, in-process mode only.

    ⚠️ This deliberately does NOT use ``pytest_sessionfinish`` to set
    ``session.exitstatus``. An adversarial audit measured that version passing
    under ``-n 2``: the hook never registers on the xdist CONTROLLER, so a skip
    in a worker yielded **exit 0** — and this repo's own pre-push hook adds
    ``-n auto``, so layer 2 was off on every developer push while CI (serial)
    stayed green. Rewriting the individual report instead means each worker
    reports a failure and xdist aggregates it like any other.

    LIVE_MODE is exempt on purpose: a run against a deployed instance may
    legitimately skip, and that mode is opt-in and never used by CI.
    """
    report = yield
    # ANY phase, not just "call": a `@pytest.mark.skip` is decided during SETUP and
    # the body never runs, so a call-phase-only check reported exit 0 on the exact
    # bypass an adversarial audit planted — measured `83 passed, 3 skipped`, exit 0,
    # serial AND under `-n 2`.
    if (
        not LIVE_MODE
        and report.skipped
        and report.when in {"setup", "call"}
        and _is_integration(report.nodeid)
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"{report.nodeid} was SKIPPED in in-process mode. A skip here hides real "
            "breakage — assert on the status and body instead. "
            "(tests/integration/conftest.py::pytest_runtest_makereport)"
        )
    return report


@pytest.fixture(scope="session")
def req_client(request):
    """The HTTP client the whole integration suite runs through."""
    if LIVE_MODE:
        # The ingress `license-authz` ext_authz reads `Authorization`; the
        # service's own AuthMiddleware reads `X-Tenant-Id: Bearer <t>` first and
        # falls back to `Authorization`. Both are sent; X-Tenant-Id wins.
        with httpx.Client(
            base_url=REQUIREMENTS_URL,
            headers={
                "Authorization": f"Bearer {LICENSE_TOKEN}",
                "X-Tenant-Id": f"Bearer {LICENSE_TOKEN}",
            },
            verify=False,
            timeout=30.0,
        ) as client:
            yield client
        return

    # Force the schema up before the first request. The autouse table-cleaner in
    # tests/conftest.py pulls `test_engine` too, but relying on cross-scope
    # ordering to create the schema would be an assumption, not a guarantee.
    request.getfixturevalue("test_engine")

    from main import app

    client = _SyncASGIClient(app, headers={"X-Tenant-Id": TENANT_ID})
    try:
        yield client
    finally:
        client.close()


def _created(response, what):
    """Assert a setup POST succeeded, naming the status and body when it did not.

    This replaces a ``pytest.skip``. A skip here made the suite report success
    precisely when the thing under test was broken.
    """
    assert response.status_code in (200, 201), (
        f"setup: could not create {what} -> {response.status_code}: {response.text}"
    )
    return response.json()


@pytest.fixture
def requirement_data(req_client):
    """Create a test requirement, yield it, then clean up."""
    payload = {
        "title": f"Integration Test Requirement {_uid()}",
        "description": "Created by integration tests",
        "effort": 5.0,
        "links": {"business_objectives": ["test-objective"]},
        "acceptance": ["Given integration test, When run, Then passes"],
    }
    data = _created(req_client.post("/requirements", json=payload), "test requirement")
    yield data
    # Cleanup — ignore errors (may already be deleted by the test)
    req_client.delete(f"/requirements/{data['id']}")


@pytest.fixture
def principle_data(req_client):
    """Create a test principle, yield it, then clean up."""
    payload = {
        "title": f"Integration Test Principle {_uid()}",
        "statement": "Test statement for integration tests",
        "rationale": "Testing rationale",
        "implications": ["Test implication"],
        "tags": ["integration-test"],
    }
    data = _created(req_client.post("/principles", json=payload), "test principle")
    yield data
    req_client.delete(f"/principles/{data['id']}")


@pytest.fixture
def decision_data(req_client):
    """Create a test decision, yield it, then clean up."""
    payload = {
        "title": f"Integration Test Decision {_uid()}",
        "context": "Test context for integration",
        "decision": "Use integration tests",
        "consequences": "Better test coverage",
        "alternatives": ["Manual testing"],
        "status": "proposed",
    }
    data = _created(req_client.post("/decisions", json=payload), "test decision")
    yield data
    req_client.delete(f"/decisions/{data['id']}")
