"""Pytest configuration and fixtures for the requirements API tests.

Requires PostgreSQL with the ``unaccent``, ``pg_trgm`` and ``vector``
extensions (the search SQL calls ``unaccent()``; FTS columns are ``tsvector``).
The connection is resolved once, at conftest import (owner rule 2026-10-21):

1. an explicit ``TEST_DATABASE_URL`` / ``DATABASE_URL`` (CI: the archetype's
   per-job ephemeral pgvector/pgvector:pg18 sidecar), else
2. an ephemeral ``docker run --rm --tmpfs /var/lib/postgresql`` postgres 18
   container on a random 127.0.0.1 port, removed at interpreter exit, else
3. FAIL. There is no skip path: a suite that never reached a database must not
   read as a passing one.

The session-scoped ``test_engine`` fixture drops the model's own tables and
recreates them, then empties them between tests, so the installed extensions
(which live in ``public``) survive. ⛔ That between-tests clean is ``DELETE``,
never ``TRUNCATE`` — see ``_clean_tables`` for the inode exhaustion that
TRUNCATE caused across the whole shared Docker VM. No database is ever created from the test
process — the old ``_worker_database_url`` helper built a ``/postgres``
maintenance DSN that pgbouncer routed onto prod :5432 and leaked per-run
databases; it has been removed, and it stays removed.

pytest-xdist isolation (per-worker SCHEMA)
------------------------------------------
``test_engine`` is ``scope="session"``, but a session is *per worker process*.
Under ``-n auto`` every worker therefore builds its own engine and runs the same
DROP-all → ``create_all`` → teardown DROP-all against the same namespace. They
stomp each other: ``UniqueViolation`` on ``pg_extension_name_index``, then on
``pg_type_typname_nsp_index``. The repo's pre-push hook adds ``-n auto`` (CI does
not), so the local gate could never pass — the classic "a local gate stricter
than CI is a gate that cannot pass, so it teaches ``--no-verify``" failure.

Fix: when running under xdist, each worker gets its **own schema**
(``test_gw0``, ``test_gw1``, …) via a ``search_path`` pushed into the DSN, and
owns its whole DROP/CREATE/clean lifecycle inside it. Serial runs (i.e. CI)
keep ``public`` and the exact code path they had before — the DSN is not even
rewritten — so this changes nothing about what CI executes.

The earlier note here said schema isolation was impossible because it breaks the
unqualified ``unaccent()`` / pg_trgm / ``vector`` lookups. That is true only of
the *archetype's* ``postgres_isolation: schema`` mode, which sets
``search_path="ci_run_…"`` and drops ``public`` from it. We set
``search_path="test_gwN,public"`` — ``public`` is still on the path, so the
extensions (which live there, and stay there) resolve exactly as before, while
the model's own tables resolve to the worker's private schema. Verified: tables
land in ``test_gwN``; ``unaccent('éclair')`` and ``'[1,2,3]'::vector`` still
resolve out of ``public``.

The one genuinely database-wide step is ``CREATE EXTENSION``, which cannot be
per-worker. It is serialised across workers with a Postgres advisory lock and
pinned ``WITH SCHEMA public``.

Keeping ``public`` on the search_path has one sharp edge, and it is why the
worker-schema ``create_all`` below passes ``checkfirst=False``: create_all's
default existence check is search_path-VISIBILITY based, so tables sitting in
``public`` make it skip creating them in the worker schema, and all workers
silently share ``public`` again. See the comment at that call site.
"""

import atexit
import hashlib
import os
import shutil
import subprocess  # nosec B404 - starts the ephemeral test postgres container
import time
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# postgres 18 + pgvector: the model layer needs the ``vector`` extension, which
# plain postgres:18 does not ship. Same image the archetype's CI sidecar runs.
EPHEMERAL_PG_IMAGE = "pgvector/pgvector:pg18"


def _start_ephemeral_postgres() -> str:
    """Start a throwaway postgres 18 container and return its DSN.

    ``--rm`` plus an ``atexit`` ``docker rm -f`` removes it when the process that
    started it exits. Data lives on tmpfs, so nothing touches the Docker VM's disk.
    Raises (never skips) when docker is unavailable or the server never comes up.
    """
    if shutil.which("docker") is None:
        raise RuntimeError(
            "No TEST_DATABASE_URL/DATABASE_URL set and docker is not on PATH, so no "
            "test database can be provided. Export a DSN or start docker."
        )
    name = f"rm-test-pg-{os.getpid()}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "--tmpfs", "/var/lib/postgresql",
            "-e", "POSTGRES_PASSWORD=postgres",
            "-e", "POSTGRES_DB=requirements",
            "-p", "127.0.0.1::5432",
            EPHEMERAL_PG_IMAGE,
        ],
        check=True,
        capture_output=True,
        text=True,
    )  # fmt: skip
    atexit.register(subprocess.run, ["docker", "rm", "-f", name], capture_output=True)
    port_out = subprocess.run(
        ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
    ).stdout
    port = port_out.strip().splitlines()[0].rsplit(":", 1)[1]
    # TCP readiness, not the unix socket: the image's initdb phase runs a
    # temporary server with listen_addresses='' and then restarts, so only the
    # final server answers on 127.0.0.1.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
            capture_output=True,
        )
        if ready.returncode == 0:
            return f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/requirements"
        time.sleep(0.5)
    raise RuntimeError(f"ephemeral postgres container {name} not ready after 60s")


# An explicit DSN wins. Otherwise the xdist CONTROLLER (or a serial run) starts
# one container and writes its DSN into os.environ below; xdist workers are
# spawned after this import and inherit it, so they never start their own.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
if not TEST_DATABASE_URL:
    if os.getenv("PYTEST_XDIST_WORKER"):
        raise RuntimeError("xdist worker started without the controller's DATABASE_URL")
    TEST_DATABASE_URL = _start_ephemeral_postgres()

# ---------------------------------------------------------------------------
# pytest-xdist: one private schema per worker (see module docstring)
# ---------------------------------------------------------------------------
# xdist sets PYTEST_XDIST_WORKER in each worker process *before* conftest is
# imported (verified: gw0/gw1 at import time; unset in the controller and in a
# serial run). That ordering is load-bearing — the rewritten DSN has to be in
# os.environ before ``from db import …`` below, because src/db.py builds its
# module-level engine from DATABASE_URL at import time. Without that, the app's
# own engine would sit on ``public`` while the tests' engine sat on the worker
# schema, and every router test would query empty tables.
XDIST_WORKER = os.getenv("PYTEST_XDIST_WORKER", "")
TEST_SCHEMA = f"test_{XDIST_WORKER}" if XDIST_WORKER else "public"

if XDIST_WORKER:
    # Pin the math-library thread pools to 1 in xdist workers.
    #
    # spaCy / scikit-learn / numpy each spin up an OpenMP-or-BLAS thread pool
    # sized to the CPU count. Serially that is what you want. Under ``-n auto``
    # it means N worker processes x N threads on an N-core box, and the
    # oversubscription is brutal — measured here on the semantic-search tests:
    #
    #   test_semantic_search_with_query_returns_entities   6.17s -> 38.30s
    #   test_semantic_search_knowledge_keyword             0.05s -> 37.51s
    #
    # 38s is close enough to the pre-push hook's ``--timeout=60`` that the
    # timeout fires inside ``semantic_search.get_nlp``, and the failure reads as
    # a mysterious "No response returned." from the ASGI stack rather than as
    # "your box is oversubscribed". One thread per worker process gives the same
    # total parallelism without the thrash.
    #
    # setdefault, so an operator who deliberately exports a value keeps it. Must
    # run before numpy/spaCy are imported — hence module level, above the
    # ``from main import app`` below that pulls the search modules in.
    for _threads_var in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",  # macOS Accelerate
    ):
        os.environ.setdefault(_threads_var, "1")


def _schema_lock_key(schema: str) -> int:
    """Stable signed-64-bit advisory-lock key naming a worker schema.

    ``pg_advisory_lock`` takes a bigint, so the name is hashed rather than
    parsed — ``test_gw10`` and ``test_gw1`` must not collide.
    """
    return int.from_bytes(
        hashlib.blake2b(schema.encode(), digest_size=8).digest(), "big", signed=True
    )


def _with_search_path(dsn: str, schema: str) -> str:
    """Return ``dsn`` with libpq ``options=-csearch_path=<schema>,public``.

    ``public`` is kept on the path on purpose: unaccent / pg_trgm / vector are
    installed there and the SQL calls them unqualified.
    """
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit(parts._replace(query=urlencode(query)))


# Serial (CI) keeps the DSN byte-identical to what it always was.
if TEST_SCHEMA != "public":
    TEST_DATABASE_URL = _with_search_path(TEST_DATABASE_URL, TEST_SCHEMA)

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["TEST_DATABASE_URL"] = TEST_DATABASE_URL

# Service URL placeholders for tests. Real values come from gitops Deployment
# env in production; tests stay in non-production mode (ENVIRONMENT defaults
# to "development"), so the production-only validator does NOT fire. We set
# these BEFORE importing main/middleware so the cached Settings instance
# captures non-empty placeholders rather than ""s.
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("ENABLE_AUTH", "true")
os.environ.setdefault("WORKER_MANAGEMENT_URL", "http://worker-management-test.invalid:8080")
os.environ.setdefault(
    "LICENSE_INTROSPECT_URL", "http://licenses-test.invalid:8544/license/introspect"
)
os.environ.setdefault("OTLP_TRACES_ENDPOINT", "")
# Every test that drives ``lifespan()`` starts the dedicated metrics listener
# (src/metrics_server.py). Left at the production default 9921, xdist workers
# would race for one port (fail-soft, but noisy) and could collide with a real
# listener on a dev box. Give each test process its own free port instead.
if "METRICS_PORT" not in os.environ:
    import socket as _socket

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
        _s.bind(("127.0.0.1", 0))
        os.environ["METRICS_PORT"] = str(_s.getsockname()[1])
# Surface validation-error detail in the canonical envelope (`error.details`)
# so test assertions can grep specific reasons. In prod these are gated behind
# LEAK_EXCEPTION_DETAILS to avoid leaking internal exception state to clients.
os.environ.setdefault("LEAK_EXCEPTION_DETAILS", "true")

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db import Base, get_session
from main import app
from models.decisions import Decision
from models.principles import Principle
from models.requirements import Requirement

# ---------------------------------------------------------------------------
# Warm the spaCy pipeline HERE, at conftest import, not in a fixture
# ---------------------------------------------------------------------------
# ``semantic_search.get_nlp()`` lazily does ``import spacy`` (4.3s measured
# cold) plus ``spacy.load("en_core_web_sm")`` (1.0s). Left lazy, that ~5.3s
# lands on whichever test first touches semantic search *in each worker
# process*, and it is charged against the pre-push hook's ``--timeout=60``.
# Serial runs pay it once on a warm page cache and never notice; under
# ``-n auto`` every worker pays it at once on a box whose cores are already
# saturated, and it stops being 5s.
#
# This deliberately is NOT a session-scoped autouse fixture. That was tried and
# is WRONG: pytest-timeout reports ``timeout func_only: False``, i.e. the timer
# covers fixture SETUP as well as the test body, and a session fixture's setup
# is billed to the first test that requests it. The fixture version therefore
# did not move the cost out of the timed window at all — it concentrated it,
# turning the first test of every worker into a ``Failed: Timeout (>60.0s)``
# inside ``markupsafe``/``spacy``'s import chain. Measured: -n 2 -> both
# workers errored in ``_warm_nlp`` setup.
#
# conftest.py is imported during COLLECTION, before any test's timer starts, so
# doing it at module level is what actually escapes the timeout. It does not
# make anything faster overall; it stops a fixed cold-import cost from being
# misreported as a slow test.
from semantic_search import get_nlp as _get_nlp
from tests.tenants import T

_get_nlp()

# ---------------------------------------------------------------------------
# Test RSA key pair for JWT signing
# ---------------------------------------------------------------------------
TEST_PRIVATE_KEY = None
TEST_PUBLIC_KEY = None


def generate_test_keys():
    """Generate RSA key pair for testing."""
    global TEST_PRIVATE_KEY, TEST_PUBLIC_KEY

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )

    TEST_PRIVATE_KEY = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    TEST_PUBLIC_KEY = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


generate_test_keys()


# ---------------------------------------------------------------------------
# JWT fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def test_private_key() -> str | None:
    """Return test private key for JWT signing."""
    return TEST_PRIVATE_KEY


@pytest.fixture(scope="session")
def test_public_key() -> str | None:
    """Return test public key for JWT verification."""
    return TEST_PUBLIC_KEY


@pytest.fixture
def create_jwt_token(test_private_key):
    """Factory fixture to create JWT tokens with custom claims."""

    def _create_token(
        sub: str = "test-user-123",
        username: str = "testuser",
        email: str = "test@example.com",
        realm_roles: list | None = None,
        client_roles: dict | None = None,
        exp_minutes: int = 30,
        **extra_claims,
    ) -> str:
        now = int(time.time())

        payload = {
            "sub": sub,
            "preferred_username": username,
            "email": email,
            "email_verified": True,
            "iat": now,
            "exp": now + (exp_minutes * 60),
            "realm_access": {"roles": realm_roles or ["user"]},
            "resource_access": client_roles or {},
            **extra_claims,
        }

        return jwt.encode(payload, test_private_key, algorithm="RS256")

    return _create_token


@pytest.fixture
def valid_token(create_jwt_token) -> str:
    """Create a valid JWT token for testing."""
    return create_jwt_token(sub=T("user-123"), username="testuser", email="test@example.com")


@pytest.fixture
def expired_token(create_jwt_token) -> str:
    """Create an expired JWT token for testing."""
    return create_jwt_token(sub=T("user-123"), username="testuser", exp_minutes=-10)


@pytest.fixture
def token_without_creator_id(test_private_key) -> str:
    """Create a JWT token without tenant_id (sub) claim."""
    now = int(time.time())
    payload = {
        "preferred_username": "testuser",
        "email": "test@example.com",
        "iat": now,
        "exp": now + 1800,
    }
    return jwt.encode(payload, test_private_key, algorithm="RS256")


@pytest.fixture
def admin_token(create_jwt_token) -> str:
    """Create a JWT token with admin role."""
    return create_jwt_token(
        sub="admin-456",
        username="admin",
        email="admin@example.com",
        realm_roles=["admin", "user"],
    )


@pytest.fixture
def second_user_token(create_jwt_token) -> str:
    """Create a JWT token for a second user."""
    return create_jwt_token(sub=T("user-789"), username="seconduser", email="second@example.com")


@pytest.fixture
def third_user_token(create_jwt_token) -> str:
    """Create a JWT token for a third user."""
    return create_jwt_token(sub=T("user-999"), username="thirduser", email="third@example.com")


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Create async test engine backed by PostgreSQL (session-scoped).

    Connects to the DSN resolved at import (explicit URL, else the ephemeral
    docker container). Tables are created once
    per session and truncated between tests via the _clean_tables autouse fixture,
    avoiding ~29k DDL ops across the full 900+ test suite.

    A "session" is per *worker process*, so under xdist this runs once per worker.
    Each worker owns its own schema (``TEST_SCHEMA``) and does its whole
    DROP/CREATE lifecycle inside it; serial runs keep ``public`` and the original
    per-table DROP loop, which must NOT become a DROP SCHEMA — ``public`` is where
    the extensions live.
    """
    from sqlalchemy import text

    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        isolation_level="READ COMMITTED",
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        await engine.dispose()
        # Never a skip: a suite that never reached its database must not read as
        # a passing one (2026-08-19: an unreachable DB came back "12 skipped").
        pytest.fail(f"PostgreSQL is unreachable, so no DB-backed test can run: {e}", pytrace=False)

    # Ensure required extensions exist. ``vector`` requires superuser to
    # install; the shared CI DB is pre-provisioned by ops with all three
    # extensions, so these CREATE EXTENSION IF NOT EXISTS statements are
    # no-ops in the steady state. They remain here so a fresh local DB
    # still bootstraps unaccent+pg_trgm.
    #
    # Extensions are database-wide, so this is the one step per-worker schemas
    # cannot isolate: on a fresh DB every xdist worker races here and they
    # collide on pg_extension_name_index. pg_advisory_xact_lock serialises them
    # (auto-released at commit); each worker then re-evaluates IF NOT EXISTS
    # inside the lock, so the losers see the winner's extension and no-op.
    # WITH SCHEMA public pins them to public even though search_path now starts
    # with the worker schema — otherwise a fresh DB would install `vector` into
    # a worker schema that is dropped at teardown, taking the type with it.
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(3121977001)"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
        # vector is required by the model layer; if it's missing the worker
        # DB was not provisioned correctly — fail loud rather than silently
        # skip tests that exercise pgvector columns.
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))

    # ------------------------------------------------------------------
    # Reclaim worker schemas stranded by a run that was KILLED
    # ------------------------------------------------------------------
    # Each worker drops and recreates its OWN schema below, so a re-run at the
    # same parallelism is self-healing. What is not self-healing is a schema
    # whose worker id no other run reuses: kill a ``-n 10`` run and then run
    # ``-n 4``, and ``test_gw4`` … ``test_gw9`` are never dropped by anyone.
    # Measured on the shared ``req_mgmt_test_pg`` container 2026-09-10: eight
    # such schemas, 246 relations each, 1,968 relations of pure residue.
    #
    # This is deliberately reclaim-on-START, not cleanup-on-exit, because the
    # failure mode IS the process not getting to run its teardown: an atexit
    # hook, a pytest finalizer and a session-fixture teardown are all equally
    # absent under SIGKILL. The next run can always be trusted to start.
    #
    # Liveness is decided by a session-level ADVISORY LOCK, not by a timestamp
    # or a name, because the estate runs several lanes against one database at
    # once and dropping a peer's live schema mid-run would be far worse than
    # leaving residue. Postgres releases a session advisory lock when the
    # client's socket closes — which SIGKILL guarantees — so "nobody holds the
    # lock" means "no live run owns this schema". A lock we fail to take is
    # simply skipped: the fail-safe direction is to leave residue, never to
    # delete a schema in use.
    schema_lock_conn = None
    if TEST_SCHEMA != "public":
        schema_lock_conn = await engine.connect()
        await schema_lock_conn.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": _schema_lock_key(TEST_SCHEMA)}
        )
        # Commit to end the implicit transaction; a SESSION-level advisory lock
        # is not transactional and outlives it, but an idle-in-transaction
        # backend held for the whole run would not be harmless.
        await schema_lock_conn.commit()

    async with engine.begin() as conn:
        stranded = (
            (
                await conn.execute(
                    text("SELECT nspname FROM pg_namespace WHERE nspname ~ '^test_gw[0-9]+$'")
                )
            )
            .scalars()
            .all()
        )
        for schema in stranded:
            if schema == TEST_SCHEMA:
                continue  # ours; the DROP/CREATE below owns it
            key = _schema_lock_key(schema)
            held = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            ).scalar()
            if not held:
                continue  # a live worker owns it
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})

    # Import all ORM models so Base.metadata.create_all registers every table.
    # Without this, new tables (architecture, PM) won't be created.
    import models.architecture  # noqa: F401 — registers ArchitectureElement etc.
    import models.idempotency  # noqa: F401 — registers IdempotencyKey
    import models.modeling  # noqa: F401 — registers ModelingModel + view occurrences
    import models.pm  # noqa: F401 — registers Programme, Project, Risk etc.
    import models.pm_catalogue  # noqa: F401 — registers ProjectType, CardType etc.
    import models.principle_conflicts  # noqa: F401 — registers PrincipleConflictRecord
    import models.quality_evaluation  # noqa: F401 — registers QualityEvaluation
    import models.toc  # noqa: F401 — registers TocTree, TocNode, TocSettings
    import models.version_history  # noqa: F401 — registers EntityVersionHistory

    # Clean slate: drop all tables then recreate from metadata (once).
    async with engine.begin() as conn:
        if TEST_SCHEMA == "public":
            for tbl in reversed(Base.metadata.sorted_tables):
                await conn.execute(text(f'DROP TABLE IF EXISTS "{tbl.name}" CASCADE'))
        else:
            # Private to this worker — nothing else can be in here, so reset the
            # whole namespace in one statement. create_all then lands in it,
            # because it is first on search_path.
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
            await conn.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))

        if TEST_SCHEMA == "public":
            await conn.run_sync(Base.metadata.create_all)
        else:
            # checkfirst=False is REQUIRED here, and is the whole reason the
            # worker schema actually isolates anything.
            #
            # create_all's default checkfirst=True asks "does this table exist?"
            # via pg_table_is_visible, i.e. it is search_path-VISIBILITY based,
            # not schema-specific. Our search_path is "test_gwN,public", so any
            # table sitting in ``public`` answers yes and create_all creates
            # NOTHING in the worker schema. Every worker then silently falls
            # through to the shared ``public`` tables and they stomp each other
            # on _clean_tables' TRUNCATE — exactly the race the per-worker
            # schema exists to remove, but now invisible instead of loud.
            #
            # ``public`` is empty on a pristine DB, so this is order-dependent:
            # the suite passed in parallel until any run left tables in public
            # (a SIGINT'd/crashed serial run does exactly that, and a serial run
            # is what CI does), after which every parallel run failed. Measured
            # on the same two files, same command, public-state the only
            # variable: public empty -> 53 passed; public populated -> 17 failed,
            # 7 passed, 29 errors. Isolated repro of the mechanism itself:
            # create_all into a fresh schema with public empty -> 22 tables in
            # the worker schema; with public populated -> 0.
            #
            # We just created TEST_SCHEMA one statement ago, so it is guaranteed
            # empty and there is nothing for checkfirst to protect against.
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
            )

    yield engine

    # Session teardown: drop tables and dispose
    async with engine.begin() as conn:
        if TEST_SCHEMA == "public":
            for tbl in reversed(Base.metadata.sorted_tables):
                await conn.execute(text(f'DROP TABLE IF EXISTS "{tbl.name}" CASCADE'))
        else:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
    if schema_lock_conn is not None:
        # Releases the advisory lock by closing the backend. On a SIGKILL this
        # line never runs and the socket close does it instead — which is the
        # whole point of using a lock rather than a teardown.
        await schema_lock_conn.close()
    await engine.dispose()


def _clean_order() -> list[str]:
    """Table names to empty, children before parents.

    ⛔ Read ``Base.metadata`` HERE, at call time, never at conftest import time.
    Half the model modules (``models.architecture``, ``models.pm``,
    ``models.toc``, ``models.principle_conflicts`` …) are imported
    inside the ``test_engine`` fixture, i.e. *after* this module is imported.
    A module-level snapshot therefore captures only the three models imported
    at the top of this file and silently skips the rest — which does not fail
    loudly, it leaks rows into the next test. Measured while writing this fix:
    the snapshot version left a late-imported table populated and turned one
    test file into 5 order-dependent failures that all passed in isolation.

    SQLAlchemy's ``sorted_tables`` is parents-first and this metadata has no FK
    cycle, so reversing it satisfies every foreign key without needing
    TRUNCATE's CASCADE. A self-referential FK is fine: a whole-table DELETE
    leaves nothing for the end-of-statement RI check to find.
    """
    return [tbl.name for tbl in reversed(Base.metadata.sorted_tables)]


async def empty_all_tables(engine) -> None:
    """Empty every mapped table in one transaction. THE between-tests clean.

    Exposed (public name, no underscore) so
    ``tests/test_clean_tables_does_not_rewrite_relations.py`` can assert its
    properties against the real code rather than against a copy of it.
    """
    from sqlalchemy import text

    async with engine.begin() as conn:
        for name in _clean_order():
            await conn.execute(text(f'DELETE FROM "{name}"'))


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(test_engine):
    """Empty all tables before AND after each test for isolation.

    ⛔ This must NOT go back to ``TRUNCATE``. It used to, and that is what
    exhausted the shared Docker VM's inode table roughly every few hours,
    taking down the pre-push hook — and therefore every push — for every
    session on the box.

    Why TRUNCATE is the wrong statement here
    ----------------------------------------
    ``TRUNCATE`` is implemented as a *relation rewrite*: it assigns a brand new
    relfilenode to the table and to every one of its indexes, and schedules the
    old relfilenode for deletion. PostgreSQL does **not** unlink that old main
    fork at commit — ``mdunlink()`` truncates it to zero bytes and defers the
    real ``unlink(2)`` to the **next checkpoint**, so the relfilenumber cannot
    be recycled underneath WAL replay. Between checkpoints, every truncated
    relation therefore leaves a 0-byte file — i.e. a live inode — on disk.

    This fixture is ``autouse``, and it ran twice per test (pre + post) over
    all 23 tables and their indexes: ~580 dead inodes per test. Measured on
    this repo, 2026-09-10, against a clean local postgres 18.4, one file
    (``tests/test_pm_crud.py``, 90 tests) that **passed, serially, with no
    interrupt of any kind**:

        before: 301 files in base/<dboid>
        after:  52,792 files — 52,542 of them zero-length
        after a single manual CHECKPOINT: 304

    Extrapolated over the ~2,742-test suite that is ~1.6M inodes manufactured
    per run, against a Docker VM whose whole filesystem has 1,048,576. Default
    ``checkpoint_timeout`` is 5 minutes and TRUNCATE writes almost no WAL, so
    ``max_wal_size`` never triggers an early checkpoint: the checkpointer
    cannot keep up with the fixture, the VM hits 0 free inodes at ~53% disk
    use, and every container on the box — not just this one — starts failing
    ``CREATE TABLE``. Under ``-n auto`` it is multiplied by the worker count.

    Note what this is NOT: it is not an interrupt or crash artefact. The
    measurement above is a fully green, cleanly-exited run. Killing a run does
    not cause the leak, it just strands the peak until a checkpoint eventually
    runs — which is precisely what cannot happen once the volume is full.

    ``DELETE`` does not rewrite the relation. It marks tuples dead in the
    existing relfilenode and creates **zero** files. The tables hold single
    digit row counts in this suite, so the dead-tuple bloat is a page or two
    per table and autovacuum handles it.

    Isolation semantics are unchanged: every table is empty before and after
    every test, sequences are NOT reset (``TRUNCATE`` without
    ``RESTART IDENTITY`` did not reset them either), and it is still one
    transaction.

    The post-test clean stays best-effort: a lingering ASGI session can make it
    deadlock or time out, and we must not fail an otherwise-passing test for
    that. But a swallowed failure used to leak rows straight into the *next*
    test, silently coupling tests to declaration order. The pre-test clean
    closes that hole: every test starts from an empty database regardless of
    what its predecessor left behind.
    """

    async def _clean() -> None:
        await empty_all_tables(test_engine)

    # Pre-test: recover from any rows a previous test's failed teardown leaked.
    await _clean()

    yield

    # Post-test: best-effort; the pre-test clean is the real guarantee.
    try:
        await _clean()
    except Exception:
        pass  # stale session conflict; next test cleans before it runs anyway


@pytest_asyncio.fixture(scope="session")
async def test_session_factory(test_engine):
    """Create async session factory bound to test engine."""
    async_session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield async_session_factory


@pytest_asyncio.fixture
async def test_session(test_session_factory) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    async with test_session_factory() as session:
        yield session
        # Explicitly rollback to release any held locks before teardown
        try:
            await session.rollback()
        except SQLAlchemyError:
            # session may already be closed/broken — safe to swallow at teardown
            pass


@pytest_asyncio.fixture
async def test_client(
    test_session_factory, test_public_key, monkeypatch
) -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client with database."""

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def authenticated_client(test_client) -> AsyncClient:
    """A test client authenticated as user-123 (a test licence, see above)."""
    test_client.headers = {"X-Tenant-Id": T("user-123")}
    return test_client


# ---------------------------------------------------------------------------
# Sample entity fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def sample_requirement(test_session) -> Requirement:
    """Create a sample requirement for testing."""
    requirement = Requirement(
        slug="test-requirement-1",
        title="Test Requirement",
        description="This is a test requirement",
        tenant_id=T("user-123"),
        project_id="test-project",
        links={},
        acceptance=[],
        effort=5.0,
        reach=100.0,
        impact=3.0,
        confidence=0.8,
    )
    test_session.add(requirement)
    await test_session.commit()
    await test_session.refresh(requirement)
    return requirement


@pytest_asyncio.fixture
async def other_user_requirement(test_session) -> Requirement:
    """Create a requirement owned by a different user."""
    requirement = Requirement(
        slug="other-requirement-1",
        title="Other User's Requirement",
        description="This requirement belongs to another user",
        tenant_id=T("user-789"),
        project_id="test-project",
        links={},
        acceptance=[],
        effort=3.0,
        reach=50.0,
        impact=2.0,
        confidence=0.7,
    )
    test_session.add(requirement)
    await test_session.commit()
    await test_session.refresh(requirement)
    return requirement


@pytest_asyncio.fixture
async def sample_principle(test_session) -> Principle:
    """Create a sample principle for testing."""
    principle = Principle(
        slug="test-principle-1",
        title="Test Principle",
        statement="Everything that can be tested shall be tested",
        tenant_id=T("user-123"),
        rationale="Testing ensures quality",
        implications=["Write tests", "Automate testing"],
        tags=["testing", "quality"],
        active=True,
    )
    test_session.add(principle)
    await test_session.commit()
    await test_session.refresh(principle)
    return principle


@pytest_asyncio.fixture
async def sample_decision(test_session) -> Decision:
    """Create a sample decision for testing."""
    decision = Decision(
        slug="test-decision-1",
        title="Test Decision",
        context="We need to choose a testing framework",
        decision="Use pytest for testing",
        tenant_id=T("user-123"),
        project_id="test-project",
        status="accepted",
        consequences="Better test coverage",
        alternatives=["unittest", "nose"],
        related_principles=[],
    )
    test_session.add(decision)
    await test_session.commit()
    await test_session.refresh(decision)
    return decision


@pytest_asyncio.fixture
async def other_user_principle(test_session) -> Principle:
    """Create a principle owned by a different user."""
    principle = Principle(
        slug="other-principle-1",
        title="Other User's Principle",
        statement="This principle belongs to another user",
        tenant_id=T("user-789"),
        rationale="Testing access control",
        implications=["Should not be accessible by other users"],
        tags=["security"],
        active=True,
    )
    test_session.add(principle)
    await test_session.commit()
    await test_session.refresh(principle)
    return principle


@pytest_asyncio.fixture
async def other_user_decision(test_session) -> Decision:
    """Create a decision owned by a different user."""
    decision = Decision(
        slug="other-decision-1",
        title="Other User's Decision",
        context="This decision belongs to another user",
        decision="Testing access control",
        tenant_id=T("user-789"),
        project_id="test-project",
        status="proposed",
        consequences="Should not be accessible by other users",
        alternatives=["No alternatives"],
        related_principles=[],
    )
    test_session.add(decision)
    await test_session.commit()
    await test_session.refresh(decision)
    return decision


@pytest_asyncio.fixture
async def multiple_user_requirements(test_session) -> dict:
    """Create requirements for multiple users for concurrent testing."""
    requirements = {T("user-123"): [], T("user-789"): [], T("user-999"): []}

    for tenant_id in requirements.keys():
        for i in range(3):
            req = Requirement(
                slug=f"{tenant_id}-req-{i}",
                title=f"Requirement {i} for {tenant_id}",
                description=f"Test requirement {i}",
                tenant_id=tenant_id,
                project_id="test-project",
                links={},
                acceptance=[],
                effort=5.0,
                reach=100.0,
                impact=3.0,
                confidence=0.8,
            )
            test_session.add(req)
            requirements[tenant_id].append(req)

    await test_session.commit()

    for reqs in requirements.values():
        for req in reqs:
            await test_session.refresh(req)

    return requirements


# ---------------------------------------------------------------------------
# Tenant isolation test fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tenant_a_id() -> str:
    """Return tenant A ID for multi-tenant tests."""
    return T("tenant-a-uuid-1234")


@pytest.fixture
def tenant_b_id() -> str:
    """Return tenant B ID for multi-tenant tests."""
    return T("tenant-b-uuid-5678")


@pytest.fixture
def tenant_a_headers(tenant_a_id: str) -> dict:
    """Return headers with X-Tenant-Id for tenant A."""
    return {"X-Tenant-Id": tenant_a_id}


@pytest.fixture
def tenant_b_headers(tenant_b_id: str) -> dict:
    """Return headers with X-Tenant-Id for tenant B."""
    return {"X-Tenant-Id": tenant_b_id}


@pytest.fixture
def tenant_a_token(create_jwt_token, tenant_a_id: str) -> str:
    """Create a JWT token for tenant A."""
    return create_jwt_token(
        tenant_id=tenant_a_id,
        username="tenant_a_user",
        email="user@tenant-a.example.com",
    )


@pytest.fixture
def tenant_b_token(create_jwt_token, tenant_b_id: str) -> str:
    """Create a JWT token for tenant B."""
    return create_jwt_token(
        tenant_id=tenant_b_id,
        username="tenant_b_user",
        email="user@tenant-b.example.com",
    )


@pytest_asyncio.fixture
async def tenant_a_requirement(test_session, tenant_a_id: str) -> Requirement:
    """Create a requirement owned by tenant A."""
    requirement = Requirement(
        slug="tenant-a-requirement-1",
        title="Tenant A Requirement",
        description="This requirement belongs to tenant A",
        tenant_id=tenant_a_id,
        project_id="tenant-a-project",
        links={},
        acceptance=[],
        effort=5.0,
        reach=100.0,
        impact=3.0,
        confidence=0.8,
    )
    test_session.add(requirement)
    await test_session.commit()
    await test_session.refresh(requirement)
    return requirement


@pytest_asyncio.fixture
async def tenant_b_requirement(test_session, tenant_b_id: str) -> Requirement:
    """Create a requirement owned by tenant B."""
    requirement = Requirement(
        slug="tenant-b-requirement-1",
        title="Tenant B Requirement",
        description="This requirement belongs to tenant B",
        tenant_id=tenant_b_id,
        project_id="tenant-b-project",
        links={},
        acceptance=[],
        effort=3.0,
        reach=50.0,
        impact=2.0,
        confidence=0.7,
    )
    test_session.add(requirement)
    await test_session.commit()
    await test_session.refresh(requirement)
    return requirement


@pytest_asyncio.fixture
async def authenticated_tenant_a_client(test_client, tenant_a_id) -> AsyncClient:
    """A test client authenticated as tenant A (a test licence)."""
    test_client.headers = {"X-Tenant-Id": tenant_a_id}
    return test_client


@pytest_asyncio.fixture
async def authenticated_tenant_b_client(test_client, tenant_b_id) -> AsyncClient:
    """A test client authenticated as tenant B (a test licence)."""
    test_client.headers = {"X-Tenant-Id": tenant_b_id}
    return test_client


@pytest_asyncio.fixture
async def tenant_a_principle(test_session, tenant_a_id: str) -> Principle:
    """Create a principle owned by tenant A."""
    principle = Principle(
        slug="tenant-a-principle-1",
        title="Tenant A Principle",
        statement="This principle belongs to tenant A",
        tenant_id=tenant_a_id,
        rationale="Testing tenant isolation",
        implications=["Tenant A only"],
        tags=["tenant-a"],
        active=True,
    )
    test_session.add(principle)
    await test_session.commit()
    await test_session.refresh(principle)
    return principle


@pytest_asyncio.fixture
async def tenant_b_principle(test_session, tenant_b_id: str) -> Principle:
    """Create a principle owned by tenant B."""
    principle = Principle(
        slug="tenant-b-principle-1",
        title="Tenant B Principle",
        statement="This principle belongs to tenant B",
        tenant_id=tenant_b_id,
        rationale="Testing tenant isolation",
        implications=["Tenant B only"],
        tags=["tenant-b"],
        active=True,
    )
    test_session.add(principle)
    await test_session.commit()
    await test_session.refresh(principle)
    return principle


@pytest_asyncio.fixture
async def tenant_a_decision(test_session, tenant_a_id: str) -> Decision:
    """Create a decision owned by tenant A."""
    decision = Decision(
        slug="tenant-a-decision-1",
        title="Tenant A Decision",
        context="This decision belongs to tenant A",
        decision="Testing tenant isolation",
        tenant_id=tenant_a_id,
        project_id="tenant-a-project",
        status="proposed",
        consequences="Tenant A only",
        alternatives=["none"],
        related_principles=[],
    )
    test_session.add(decision)
    await test_session.commit()
    await test_session.refresh(decision)
    return decision


@pytest_asyncio.fixture
async def tenant_b_decision(test_session, tenant_b_id: str) -> Decision:
    """Create a decision owned by tenant B."""
    decision = Decision(
        slug="tenant-b-decision-1",
        title="Tenant B Decision",
        context="This decision belongs to tenant B",
        decision="Testing tenant isolation",
        tenant_id=tenant_b_id,
        project_id="tenant-b-project",
        status="proposed",
        consequences="Tenant B only",
        alternatives=["none"],
        related_principles=[],
    )
    test_session.add(decision)
    await test_session.commit()
    await test_session.refresh(decision)
    return decision


@pytest.fixture(autouse=True)
def _isolate_tenant_resolution_cache():
    """Drop the tenant name→UUID caches around every test.

    ``auth.tenant_resolution`` keeps module-level positive, negative and
    in-flight maps. They are process-wide, so without this a test that resolves
    a name leaves the answer sitting there for whatever runs next — which is
    exactly how a test passes in isolation, passes in its own file, and then
    fails (or, worse, passes vacuously) in the full suite. Clearing on both
    sides means a test cannot be rescued by a neighbour's cache entry either.
    """
    from auth.tenant_resolution import clear_tenant_resolution_cache

    clear_tenant_resolution_cache()
    yield
    clear_tenant_resolution_cache()


@pytest.fixture(autouse=True)
def _permissive_licences_registry_by_default():
    """Default the licences byo-mode lookup to "yes, registered" for any tenant.

    ``auth.middleware`` verifies a caller-supplied (delegated) tenant uuid
    against licences
    (``resolve_tenant_to_uuid(..., verify=True)`` — see
    ``tests/test_tenant_resolution_hardening.py`` and
    ``tests/test_tenant_is_resolved_to_a_uuid.py`` for why). The other ~150
    call sites across this suite send a plain ``X-Tenant-Id`` (often
    a ``tests.tenants.T(...)`` label, which nothing outside this repo has ever
    registered) purely to get A tenant, not to test registration — without a
    default they would all start issuing real outbound HTTP calls to whatever
    ``LICENSE_INTROSPECT_URL`` happens to resolve to in this environment.

    This installs a permissive fake transport that answers 200 for ANY
    ``/tenants/{id}/byo-mode`` path, so every such test keeps working exactly
    as before. A test that specifically exercises the registered/unregistered
    distinction (e.g. ``test_tenant_resolution_hardening.py``) installs its OWN
    stub via ``monkeypatch.setattr(tenant_resolution, "_client", ...)`` *inside*
    the test body, which runs after this fixture's setup and so overrides it.
    """
    import auth.tenant_resolution as tenant_resolution

    def _admit_anything(request: Request) -> Response:
        tenant_id = request.url.path.rsplit("/", 2)[-2]
        return Response(200, json={"tenant_id": tenant_id, "byo_mode": "platform"})

    original = tenant_resolution._client
    tenant_resolution._client = AsyncClient(transport=MockTransport(_admit_anything))
    yield
    tenant_resolution._client = original


# ---------------------------------------------------------------------------
# Test licences. RM admits NO tokenless caller (R351 (7)): every request outside
# ``auth.middleware.PUBLIC_PATHS`` must carry a licence that introspects active.
# The suite's ~150 call sites name their tenant with a plain ``X-Tenant-Id: <t>``
# (plus an optional ``X-Subject-Id``) purely to get A tenant. Rather than rewrite
# each, the in-process transport turns that plain header into a TEST LICENCE for
# exactly that tenant/subject, and introspection of a test licence answers
# ``active`` with that tenant/subject. The request then takes the real licence
# path through ``AuthMiddleware`` — nothing in ``src/`` knows about this.
#
# Not rewritten: a request that already carries a licence (``X-Tenant-Id:
# Bearer …`` or ``Authorization``), or one with no tenant header (⇒ 401), or any
# test marked ``tokenless`` — those assert what a real tokenless caller gets.
# ---------------------------------------------------------------------------
TEST_LICENCE_PREFIX = "rm-test-licence:"


def licence_for(tenant: str, subject: str = "") -> str:
    """A licence token the test introspection fake answers ``active`` for."""
    return f"{TEST_LICENCE_PREFIX}{tenant}|{subject}"


def _is_bearer(value: str) -> bool:
    low = value.strip().lower()
    return low == "bearer" or low.startswith("bearer ")


@pytest.fixture(autouse=True)
def _plain_tenant_header_is_a_test_licence(request):
    # Its own MonkeyPatch, so a test calling ``monkeypatch.undo()`` on the shared
    # fixture does not silently strip every later request of its licence.
    import auth.middleware as auth_middleware

    patch = pytest.MonkeyPatch()
    real_introspect = auth_middleware.introspect_license

    async def _introspect(token: str, *, url: str | None = None):
        if token.startswith(TEST_LICENCE_PREFIX):
            tenant, _, subject = token[len(TEST_LICENCE_PREFIX) :].partition("|")
            return {"active": True, "tenant_id": tenant, "subject_id": subject}
        return await real_introspect(token, url=url)

    patch.setattr(auth_middleware, "introspect_license", _introspect)
    if not request.node.get_closest_marker("tokenless"):
        real_handle = ASGITransport.handle_async_request

        async def _handle(self, req: Request) -> Response:
            tenant = (req.headers.get("X-Tenant-Id") or "").strip()
            if tenant and not _is_bearer(tenant) and "authorization" not in req.headers:
                subject = (req.headers.get("X-Subject-Id") or "").strip()
                req.headers["X-Tenant-Id"] = f"Bearer {licence_for(tenant, subject)}"
            return await real_handle(self, req)

        patch.setattr(ASGITransport, "handle_async_request", _handle)
    yield
    patch.undo()


@pytest.fixture
def licences_reject_unknown_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model what licences actually answers for a token it did not issue: a 200
    ``{"active": false}`` → 401. Tests asserting "an invalid licence is refused"
    must use this: without it they exercise an UNREACHABLE introspection
    endpoint, which is a 503 (no verdict), not a 401 (bad credential)."""
    import auth.middleware as auth_middleware

    test_aware = auth_middleware._introspect_license

    async def _inactive(token: str) -> dict | None:
        if token.startswith(TEST_LICENCE_PREFIX):
            return await test_aware(token)
        return {"active": False, "reason": "invalid_token"}

    monkeypatch.setattr(auth_middleware, "_introspect_license", _inactive)
