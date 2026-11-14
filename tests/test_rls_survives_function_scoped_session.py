"""A function-scoped ``get_session`` must still bind the RLS tenant GUC.

⛔ THE FAILURE THIS GUARDS AGAINST PRODUCES NO ERROR, NO EXCEPTION AND NO LOG LINE.

``src/db.py`` binds ``app.current_tenant`` with
``register_tenant_guc(engine, lambda: tenant_id_var.get(None))`` — a ``begin``
listener on the ENGINE, so the GUC is bound once per transaction, on the
connection that transaction holds. The policy shipped by
``2026_07_02_0030_rls_tenant_isolation_backstop`` (whose DDL is frozen inline
in that migration, copied from the former ``kag_common.database.rls``) is

    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid

and production connects as ``requirements_app``, a NON-OWNER role with
``rolbypassrls = false``. So an unbound GUC does not raise: the predicate
compares ``tenant_id`` against NULL and **every table reads as empty** — HTTP
200, zero rows, nothing in the logs. That is the one way narrowing the session
dependency's lifetime can break production silently, which is why this is a
behavioural test against a real Postgres rather than a paragraph of reasoning.

**What is changing and why it needs a test.** ``get_session`` moves from the
request-scoped default to ``Depends(get_session, scope="function")``, so the
session's ``__aexit__`` runs when the path-operation function RETURNS instead of
after the response is sent. That moves teardown earlier. It must not move the
GUC *binding* — the binding happens at transaction-begin, long before either
teardown point — and the whole question is whether the earlier release disturbs
the per-connection state the policy reads.

**The negative control is the load-bearing half.** This suite builds its schema
with ``Base.metadata.create_all`` (see ``tests/conftest.py``), NOT with alembic,
and it connects as a superuser. Neither the policy nor the enforcement is
present by default — so a test that merely asserted "rows came back" would pass
identically whether the fix works or whether RLS simply is not switched on. This
file therefore installs the real policy DDL, switches the query onto a
non-superuser role, and asserts BOTH directions:

    * GUC bound   -> the tenant's rows, a NON-ZERO count, and only that tenant's;
    * GUC unset   -> exactly zero, on a table the same test has just proved is
                     non-empty.

Only the second assertion can distinguish "RLS is enforcing and the binding
survived" from "RLS was never enforcing here, so of course nothing broke".

Uses the ``monkeypatch.setenv("TENANT_RLS_MODE", "enforce")`` idiom
(``register_tenant_guc`` is registered with ``mode=None``, so it re-reads the
env at every transaction-begin; no engine rebuild, nothing left behind). It
asserts the ROWS, which is the thing the GUC exists to control.

⚠️ Marker: ``tenant_isolation`` + ``security``. NOT ``integration`` — the default
``addopts`` is ``-m 'not integration'``, so marking it integration would remove
it from every ordinary run and from CI, and an unexercised claim is worse than
no claim. The markers here are for selection (``-m tenant_isolation``), never
for exclusion. Nothing in this file skips: if RLS cannot be exercised in the
local/CI database, it FAILS and says why.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from db import Base, get_session, tenant_id_var
from fleet_auth import downgrade_statements, tenant_table_names, upgrade_statements
from models.principles import Principle
from tests.tenants import T

# tokenless: the hermetic app reads X-Tenant-Id itself (no AuthMiddleware): send it as-is.
pytestmark = [pytest.mark.tenant_isolation, pytest.mark.security, pytest.mark.tokenless]


TENANT_A = T("rls-function-scope-tenant-a")
TENANT_B = T("rls-function-scope-tenant-b")

TABLE = "principles"

# One probe role per xdist worker, matching the per-worker schema that
# ``tests/conftest.py`` builds. Two workers must not race on one role.
_WORKER = os.getenv("PYTEST_XDIST_WORKER", "") or "serial"
PROBE_ROLE = f"rls_probe_{_WORKER}"
PROBE_SCHEMA = f"test_{_WORKER}" if os.getenv("PYTEST_XDIST_WORKER") else "public"


# ── the dependency under test ─────────────────────────────────────────────────
#
# Constructed at import so an unsupported ``scope=`` is a LOUD collection error
# naming the parameter, rather than a confusing failure inside a fixture. This
# whole file exists because of this one keyword argument; if the installed
# FastAPI does not accept it, that is the finding.
try:
    FUNCTION_SCOPED_SESSION = Depends(get_session, scope="function")
except TypeError as exc:  # pragma: no cover - only on an unsupported FastAPI
    raise AssertionError(
        "fastapi.Depends does not accept scope='function' in this environment "
        f"({exc!r}). The change this test guards — releasing the DB session when "
        "the path-operation function returns rather than after the response is "
        "sent — cannot be expressed with the installed FastAPI version."
    ) from exc


class _SetTenantMiddleware:
    """Pure-ASGI mirror of ``AuthMiddleware._apply_principal``'s one relevant line.

    Deliberately NOT ``BaseHTTPMiddleware``: that runs the downstream app in a
    separate task, and a ``ContextVar`` set in its ``dispatch`` is not reliably
    visible to the endpoint. A pure-ASGI middleware runs in the SAME task, so
    the contextvar the ``begin`` listener reads is the one this sets — which is
    the mechanism under test, not an artefact of the test harness.

    The tenant comes from a header only because this app is hermetic; in
    production ``_apply_principal`` sets it from the RESOLVED principal and never
    from a client-asserted value.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        tenant = headers.get("x-tenant-id") or None
        token = tenant_id_var.set(tenant)
        try:
            await self.app(scope, receive, send)
        finally:
            tenant_id_var.reset(token)


def _build_app() -> FastAPI:
    """A hermetic app with ONE route on the function-scoped session dependency."""
    app = FastAPI()

    @app.get("/probe")
    async def probe(session: AsyncSession = FUNCTION_SCOPED_SESSION) -> dict:
        # The first statement is what opens the transaction, which is what fires
        # ``register_tenant_guc``'s ``begin`` listener. ``SET LOCAL ROLE`` then
        # drops this transaction onto a NON-SUPERUSER, NON-OWNER role for the
        # rest of it — which is the only way RLS can apply at all here, since
        # the test DSN connects as a superuser and superusers bypass RLS
        # unconditionally. It reverts at transaction end, so nothing leaks onto
        # the pooled connection.
        await session.execute(text(f'SET LOCAL ROLE "{PROBE_ROLE}"'))
        bound = await session.scalar(text("SELECT current_setting('app.current_tenant', true)"))
        visible = await session.scalar(text(f"SELECT count(*) FROM {TABLE}"))
        return {"guc": bound, "visible": int(visible or 0)}

    app.add_middleware(_SetTenantMiddleware)
    return app


APP = _build_app()


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def rls_enforced(test_engine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Install the REAL policy on ``principles`` and make the runtime engine bind the GUC.

    The DDL is rendered by the vendored ``fleet_auth`` RLS helpers rather than
    hand-written, so the predicate this test proves is the predicate production
    runs — including the ``::uuid`` cast, which ``fleet_auth._predicate`` derives
    from the column type and which a hand-written ``tenant_id = NULLIF(...)``
    would silently get wrong now that ``tenant_id`` is a ``uuid`` column.

    Scoped to ONE table. Every other tenant table is excluded so this fixture
    cannot perturb the ~1185 tests that share this database.
    """
    covered = set(tenant_table_names(Base.metadata))
    if TABLE not in covered:
        pytest.fail(
            f"{TABLE!r} is not among the tenant tables the RLS helper would put "
            f"under RLS ({sorted(covered)}); this test would be measuring a table "
            "the production policy does not cover.",
            pytrace=False,
        )
    others = covered - {TABLE}

    async with test_engine.begin() as conn:
        # A role, not a second DSN: ``SET LOCAL ROLE`` needs no password, no
        # pg_hba entry and no extra engine, and RLS is evaluated against the
        # CURRENT role, so it enforces exactly as a separate login would.
        # NOLOGIN for the same reason. Created idempotently and deliberately NOT
        # dropped at teardown: a NOLOGIN role owning nothing is inert, and
        # dropping it would race the other xdist workers' grants.
        try:
            await conn.execute(
                text(
                    "DO $$ BEGIN "
                    f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PROBE_ROLE}') "
                    f'THEN CREATE ROLE "{PROBE_ROLE}" NOLOGIN NOBYPASSRLS; END IF; END $$'
                )
            )
        except Exception as exc:  # noqa: BLE001 - the message is the point
            pytest.fail(
                "could not create the non-superuser probe role, so Postgres RLS cannot be "
                f"exercised in this database at all: {exc!r}. Refusing to skip — a skipped "
                "RLS test is indistinguishable from a passing one, and this file's entire "
                "value is the negative control it can no longer run.",
                pytrace=False,
            )
        await conn.execute(text(f'GRANT USAGE ON SCHEMA "{PROBE_SCHEMA}" TO "{PROBE_ROLE}"'))
        await conn.execute(text(f'GRANT SELECT ON {TABLE} TO "{PROBE_ROLE}"'))
        for stmt in upgrade_statements(Base.metadata, exclude=others):
            await conn.execute(text(stmt))

    # ``register_tenant_guc`` was registered with ``mode=None``, so it re-reads
    # TENANT_RLS_MODE at every transaction-begin. Same idiom as
    # tests/test_eval_pool_partition.py — no engine rebuild, nothing persisted.
    monkeypatch.setenv("TENANT_RLS_MODE", "enforce")

    yield

    async with test_engine.begin() as conn:
        for stmt in downgrade_statements(Base.metadata, exclude=others):
            await conn.execute(text(stmt))


@pytest_asyncio.fixture
async def seeded(test_session: AsyncSession) -> AsyncGenerator[dict[str, int], None]:
    """Two principles for tenant A, one for tenant B.

    Seeded through the session-scoped test engine (a superuser, no policy in its
    way), so the rows exist regardless of what RLS does to the request path.
    """
    counts = {TENANT_A: 2, TENANT_B: 1}
    for tenant, n in counts.items():
        for i in range(n):
            test_session.add(
                Principle(
                    slug=f"rls-fn-scope-{tenant[:8]}-{i}",
                    title=f"Function-scoped session probe {i}",
                    statement="A row that must remain visible to its own tenant.",
                    tenant_id=tenant,
                    tenant_name="rls-probe",
                    subject_id="rls-probe-subject",
                    visibility="tenant",
                    rationale=None,
                    implications=[],
                    tags=[],
                    active=True,
                )
            )
    await test_session.commit()
    yield {"a": counts[TENANT_A], "b": counts[TENANT_B]}


@pytest_asyncio.fixture
async def probe_client() -> AsyncGenerator[AsyncClient, None]:
    """A client for the hermetic app. NO ``get_session`` override — that is the point.

    ``tests/conftest.py``'s ``test_client`` overrides ``get_session`` with its own
    factory; that would replace the very dependency under test and take the
    engine-level GUC listener out of the picture.
    """
    transport = ASGITransport(app=APP)
    async with AsyncClient(transport=transport, base_url="http://rls-probe") as client:
        yield client


# ── the guard ─────────────────────────────────────────────────────────────────


async def test_function_scoped_session_still_reads_its_own_tenants_rows(
    rls_enforced: None, seeded: dict[str, int], probe_client: AsyncClient
) -> None:
    """Kills: any change that leaves ``app.current_tenant`` unbound on the request path.

    The count assertion is the whole test. Under the defect the route answers
    200 with ``visible == 0`` — no error, no exception, no log line — so the only
    honest evidence is a NON-ZERO count of rows that are known to exist.
    """
    response = await probe_client.get("/probe", headers={"X-Tenant-Id": TENANT_A})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["guc"] == TENANT_A, (
        f"app.current_tenant is {body['guc']!r} inside a function-scoped "
        f"get_session transaction, not {TENANT_A!r}. Releasing the session when the "
        "path-operation function returns has cost the request its RLS tenant "
        "binding; under TENANT_RLS_MODE=enforce every query then reads zero rows "
        "with no error at all."
    )
    assert body["visible"] > 0, (
        f"the route saw {body['visible']} rows for tenant {TENANT_A} while "
        f"{seeded['a']} of its principles exist in this database. This is the "
        "silent failure mode: HTTP 200, an empty result set, and nothing anywhere "
        "saying the tenant GUC was never bound."
    )
    assert body["visible"] == seeded["a"], (
        f"the route saw {body['visible']} rows where tenant {TENANT_A} owns exactly "
        f"{seeded['a']}; the policy is admitting or hiding rows it should not "
        f"(tenant B holds {seeded['b']} further rows in the same table)."
    )


async def test_the_same_route_reads_zero_rows_when_no_tenant_is_bound(
    rls_enforced: None,
    seeded: dict[str, int],
    probe_client: AsyncClient,
    test_session: AsyncSession,
) -> None:
    """THE NEGATIVE CONTROL. Without it the test above is not evidence of anything.

    A passing "tenant sees its rows" assertion has two possible causes that look
    identical from the outside:

        1. RLS is enforcing and the function-scoped session still binds the GUC
           (the property we want), or
        2. RLS is not actually enforcing in this database — no policy, or a
           superuser connection — so nothing was ever being filtered and the
           dependency's scope could not have mattered either way.

    This test separates them. It drives the SAME route on the SAME engine with
    ``tenant_id_var`` left unset, so ``register_tenant_guc`` binds nothing, and
    requires the answer to be exactly zero on a table it has just proved is not
    empty. Cause 2 yields all three rows here and fails loudly.
    """
    total = await test_session.scalar(text(f"SELECT count(*) FROM {TABLE}"))
    assert int(total or 0) == seeded["a"] + seeded["b"], (
        f"the fixture seeded {seeded['a'] + seeded['b']} principles but the table holds "
        f"{total} — this control cannot tell an enforced zero from an empty table."
    )

    response = await probe_client.get("/probe")  # no X-Tenant-Id => no tenant bound
    assert response.status_code == 200, response.text
    body = response.json()

    assert not body["guc"], (
        f"app.current_tenant is {body['guc']!r} on a request that carries no tenant. "
        "Something is binding a tenant that no principal resolved to, which is the "
        "one thing register_tenant_guc must never do."
    )
    assert body["visible"] == 0, (
        f"with no tenant bound the route still read {body['visible']} of "
        f"{seeded['a'] + seeded['b']} rows. Postgres RLS is NOT enforcing on "
        f"{TABLE} in this database, so the companion test above proves nothing: it "
        "would pass unchanged whether or not the function-scoped session preserves "
        "the tenant GUC. Fix the enforcement (policy installed? query running as a "
        "non-superuser, non-owner role? TENANT_RLS_MODE=enforce?) before trusting "
        "either result."
    )


def test_the_route_under_test_really_uses_a_function_scoped_dependency() -> None:
    """Kills: a rebase that quietly restores the default request scope in this file.

    Both tests above pass under request scope too — correctly, since request
    scope is the shape that works today. Their value is entirely conditional on
    the dependency they exercise actually being the NEW one, and that is one
    keyword argument in one ``Depends`` call.
    """
    scope = getattr(FUNCTION_SCOPED_SESSION, "scope", None)
    assert scope == "function", (
        f"the session dependency in this file has scope {scope!r}, not 'function'. "
        "This file is then testing the pre-change behaviour and cannot say anything "
        "about releasing the connection when the path operation returns."
    )
    assert FUNCTION_SCOPED_SESSION.dependency is get_session, (
        "the dependency under test is not db.get_session, so nothing here exercises "
        "the engine-level register_tenant_guc listener that db.py installs on "
        "AsyncSessionLocal's engine."
    )
