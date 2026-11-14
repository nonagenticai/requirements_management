"""The TENANCY §4.4 visibility matrix, one test id per cell.

Spec: n8n_custom_nodes ``AA_IMPLEMENTATION/TENANCY_AND_IMPROVEMENT.md`` §4.4.
George's rule, restated so a test can check it: PUBLISHED system assets are
readable by every PLATFORM tenant (``settings.platform_tenant_ids``) and by NO
customer tenant (owner rule 2026-09-30 — system + improvement data must never
be visible to a customer; this cell was ``R`` for ``customer`` until then);
every tenant-owned asset is private to its tenant.
Lifecycle states apply only to rows owned by the system tenant (``…0001``).
A hidden row answers **404, never 403**, so its existence does not leak.

Readers (the matrix rows):

* ``system``       — ``…0001`` (own rows + published inheritance ONLY; NOT a super-admin)
* ``stream_own``   — a stream tenant of the asset's OWN kind (``…0005``)
* ``stream_other`` — a stream tenant of ANOTHER kind (``…0002``)
* ``execution``    — ``…0003``
* ``customer``     — ``…0100``

Rows (the matrix columns): three system rows (DRAFT / PUBLISHED / the spec's
DISCARDED) plus one row owned by each non-system tenant. The tenant rows are
seeded ``PUBLISHED`` on purpose: a non-system row has no visibility state, so
PUBLISHED on it must grant nothing to anyone else.

⚠️ The spec's DISCARDED is this service's ``DEPRECATED``. ``VERSION_STATUSES``
in ``src/schemas/common.py`` is exactly {DRAFT, PUBLISHED, DEPRECATED}, and every
``*Out`` schema enforces it on the READ path — a row stored as ``DISCARDED``
would 500 the owner's own GET instead of exercising visibility.

Not modelled, and why:

* CANARY — principles/requirements/decisions never enter CANARY (§4.1).
* SYSTEM writing its own PUBLISHED / DEPRECATED rows — that is the ``P``
  (lifecycle transition) cell, not a plain write; it needs its own contract.

Cells the spec requires but no code implements yet are ``xfail(strict=True)``,
so the day one is built its XPASS fails and forces this table to be updated.

Layer: the APP layer (``repositories/visibility.py`` ``scope`` / ``system_clause``
/ ``can_mutate``), driven over HTTP exactly like
``tests/test_system_tenant_inheritance.py``. The test DSN is a superuser, so RLS
is not in play here; the RLS half is covered by that file.

The mutant test at the bottom replaces ``system_clause`` with one that ignores
the row's state and requires at least one state-hidden cell to leak — proof
that this table can go red.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

import repositories.visibility as visibility_mod
from models.principles import Principle
from settings import get_settings

pytestmark = [pytest.mark.tenant_isolation, pytest.mark.security]

SYSTEM = get_settings().system_tenant_id


def _sibling(n: int) -> str:
    """The ``…000n`` tenant in the same uuid family as the system tenant."""
    return f"{SYSTEM.rsplit('-', 1)[0]}-{n:012d}"


READERS: dict[str, str] = {
    "system": SYSTEM,
    "stream_own": _sibling(5),
    "stream_other": _sibling(2),
    "execution": _sibling(3),
    "customer": _sibling(100),
}

# row key -> (owning reader key, version_status)
ROWS: dict[str, tuple[str, str]] = {
    "sys_published": ("system", "PUBLISHED"),
    "sys_draft": ("system", "DRAFT"),
    "sys_deprecated": ("system", "DEPRECATED"),  # the spec's DISCARDED
    "stream_own_row": ("stream_own", "PUBLISHED"),
    "stream_other_row": ("stream_other", "PUBLISHED"),
    "execution_row": ("execution", "PUBLISHED"),
    "customer_row": ("customer", "PUBLISHED"),
}

R, HIDDEN = 200, 404
W_OK, REFUSED = 200, 404

STREAM_NOT_BUILT = "stream write-back not built (TENANCY §4.4)"


def _tenant_row_read(owner: str) -> dict[str, int]:
    # Owner R; EVERY other tenant — INCLUDING system (user ruling 2026-10-02:
    # the system tenant never reads another tenant's data).
    return {r: (R if r == owner else HIDDEN) for r in READERS}


# READ matrix: row -> reader -> expected status of GET /principles/{id}.
READ_MATRIX: dict[str, dict[str, int]] = {
    # Platform readers inherit; the customer is fenced off (2026-09-30).
    "sys_published": {r: (HIDDEN if r == "customer" else R) for r in READERS},
    "sys_draft": {
        "system": R,
        "stream_own": R,
        "stream_other": HIDDEN,
        "execution": HIDDEN,
        "customer": HIDDEN,
    },
    "sys_deprecated": {
        "system": R,
        "stream_own": R,  # "its own evidence"
        "stream_other": HIDDEN,
        "execution": HIDDEN,
        "customer": HIDDEN,
    },
    "stream_own_row": _tenant_row_read("stream_own"),
    "stream_other_row": _tenant_row_read("stream_other"),
    "execution_row": _tenant_row_read("execution"),
    "customer_row": _tenant_row_read("customer"),
}

READ_NOT_BUILT: dict[tuple[str, str], str] = {
    ("stream_own", "sys_draft"): STREAM_NOT_BUILT,
    ("stream_own", "sys_deprecated"): STREAM_NOT_BUILT,
    # (system reading another tenant's row is deliberately NOT a feature: 2026-10-02.)
}


def _tenant_row_write(owner: str) -> dict[str, int]:
    # Owner W. SYSTEM writes X's row only as an explicit super-admin action while
    # X's workspace is active (§2.5), so a plain PATCH is refused. Others refused.
    return {r: (W_OK if r == owner else REFUSED) for r in READERS}


# WRITE matrix: row -> reader -> expected status of PATCH /principles/{id}.
# SYSTEM on sys_published / sys_deprecated is the P cell and deliberately absent.
WRITE_MATRIX: dict[str, dict[str, int]] = {
    "sys_draft": {
        "system": W_OK,
        "stream_own": W_OK,  # the cross-tenant write into …0001 (§4.1 exception)
        "stream_other": REFUSED,
        "execution": REFUSED,
        "customer": REFUSED,
    },
    "sys_published": {r: REFUSED for r in READERS if r != "system"},
    "sys_deprecated": {r: REFUSED for r in READERS if r != "system"},
    "stream_own_row": _tenant_row_write("stream_own"),
    "stream_other_row": _tenant_row_write("stream_other"),
    "execution_row": _tenant_row_write("execution"),
    "customer_row": _tenant_row_write("customer"),
}

WRITE_NOT_BUILT: dict[tuple[str, str], str] = {
    ("stream_own", "sys_draft"): STREAM_NOT_BUILT,
}


def _cells(matrix: dict[str, dict[str, int]], not_built: dict[tuple[str, str], str]) -> list:
    params = []
    for row, by_reader in matrix.items():
        for reader, expected in by_reader.items():
            reason = not_built.get((reader, row))
            marks = [pytest.mark.xfail(reason=reason, strict=True)] if reason else []
            cell_id = f"{reader}->{row}={expected}"
            params.append(pytest.param(reader, row, expected, marks=marks, id=cell_id))
    return params


READ_CELLS = _cells(READ_MATRIX, READ_NOT_BUILT)
WRITE_CELLS = _cells(WRITE_MATRIX, WRITE_NOT_BUILT)
REFUSED_WRITE_CELLS = [
    pytest.param(reader, row, id=f"{reader}->{row}")
    for row, by_reader in WRITE_MATRIX.items()
    for reader, expected in by_reader.items()
    if expected == REFUSED and (reader, row) not in WRITE_NOT_BUILT
]
# The cells the mutant must break: system rows that are NOT published and are
# hidden from a reader today. Derived from the table, so they cannot drift apart.
STATE_HIDDEN_CELLS = [
    (reader, row)
    for row, (owner, state) in ROWS.items()
    if owner == "system" and state != "PUBLISHED"
    for reader, expected in READ_MATRIX[row].items()
    if expected == HIDDEN and (reader, row) not in READ_NOT_BUILT
]


# ── fixtures and helpers ────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def seeded(test_session: AsyncSession) -> dict[str, SimpleNamespace]:
    objs: dict[str, Principle] = {}
    for key, (owner, state) in ROWS.items():
        tenant = READERS[owner]
        objs[key] = Principle(
            slug=f"matrix-{key.replace('_', '-')}",
            title=f"Matrix {key}",
            statement=f"Statement for {key}.",
            tenant_id=tenant,
            tenant_name="visibility-matrix-probe",
            subject_id=tenant,
            visibility="tenant",
            rationale=None,
            implications=[],
            tags=[],
            active=True,
            version_status=state,
        )
    test_session.add_all(objs.values())
    await test_session.commit()
    for obj in objs.values():
        await test_session.refresh(obj)
    await test_session.commit()
    # Plain values only: after expire_all() an ORM attribute access would
    # lazy-load outside a greenlet (MissingGreenlet).
    return {k: SimpleNamespace(id=str(o.id), title=o.title) for k, o in objs.items()}


def _as(client, reader: str):
    client.headers = {"X-Tenant-Id": READERS[reader]}
    return client


async def _read(client, reader: str, row_id: str) -> tuple[int, bool]:
    """(GET-by-id status, whether the id appears in the reader's list)."""
    c = _as(client, reader)
    one = await c.get(f"/principles/{row_id}")
    listed = await c.get("/principles", params={"limit": 1000})
    assert listed.status_code == 200, listed.text
    return one.status_code, row_id in {item["id"] for item in listed.json()}


async def _stored_title(session: AsyncSession, row_id: str) -> str:
    session.expire_all()
    stmt = select(Principle.title).where(Principle.id == UUID(row_id))
    title = (await session.execute(stmt)).scalar_one()  # also proves the row exists
    await session.commit()
    return title


def _stateless_system_clause(model, *, tenant_id, subject_id, system_tenant_id):
    """The mutant: ``system_clause`` with the ``version_status == PUBLISHED`` term removed."""
    return and_(model.tenant_id == system_tenant_id, model.visibility == "tenant")


# ── table integrity ─────────────────────────────────────────────────────────


def test_matrix_covers_every_reader_row_pair() -> None:
    assert len(set(READERS.values())) == len(READERS), READERS
    assert READERS["system"] == SYSTEM
    # The matrix's reader roles must match the configured platform set, or the
    # customer/platform split above is testing the wrong tenants.
    platform = get_settings().platform_tenant_id_set
    assert READERS["customer"] not in platform
    assert {READERS[r] for r in READERS if r != "customer"} <= platform
    assert set(READ_MATRIX) == set(ROWS)
    for row, by_reader in READ_MATRIX.items():
        assert set(by_reader) == set(READERS), row
    assert len(READ_CELLS) == len(READERS) * len(ROWS)
    # Only the two system P cells are absent from the write table.
    assert len(WRITE_CELLS) == len(READERS) * len(ROWS) - 2
    assert STATE_HIDDEN_CELLS, "no state-hidden cell: the mutant would prove nothing"


# ── READ cells ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("reader", "row", "expected"), READ_CELLS)
async def test_read_cell(test_client, seeded, reader, row, expected) -> None:
    status, listed = await _read(test_client, reader, seeded[row].id)
    assert status != 403, f"{reader} GET {row}: a hidden row must answer 404, never 403"
    assert status == expected, f"{reader} GET {row}: got {status}, want {expected}"
    # The list endpoint must agree with GET-by-id: a hidden row is never listed.
    assert listed is (expected == R), f"{reader} list membership of {row}: {listed}"


# ── WRITE cells ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("reader", "row", "expected"), WRITE_CELLS)
async def test_write_cell(test_client, test_session, seeded, reader, row, expected) -> None:
    target = seeded[row]
    new_title = f"written by {reader}"
    client = _as(test_client, reader)
    resp = await client.patch(f"/principles/{target.id}", json={"title": new_title})
    assert resp.status_code != 403, f"{reader} PATCH {row}: refused must be 404, never 403"
    assert resp.status_code == expected, f"{reader} PATCH {row}: {resp.status_code} {resp.text}"
    stored = await _stored_title(test_session, target.id)
    assert stored == (new_title if expected == W_OK else target.title), stored


@pytest.mark.parametrize(("reader", "row"), REFUSED_WRITE_CELLS)
async def test_refused_delete_cell(test_client, test_session, seeded, reader, row) -> None:
    target = seeded[row]
    resp = await _as(test_client, reader).delete(f"/principles/{target.id}")
    assert resp.status_code == REFUSED, f"{reader} DELETE {row}: {resp.status_code} {resp.text}"
    assert await _stored_title(test_session, target.id) == target.title


# ── explicit scenario anchors ───────────────────────────────────────────────
#
# The matrix above already covers each of these as a cell (now that the
# system-tenant super-admin read-widen is built). They are restated here,
# named and standalone, because the super-admin widen is new behaviour with
# no prior test naming it directly.
#
# The customer-vs-system-PUBLISHED cell is NOT restated here — it is the
# one behaviour a stale brief could plausibly ask to reverse (owner ruling
# 2026-09-30, PR #186 "fix(tenancy): only platform tenants inherit
# system-tenant rows": a customer tenant — netcare — could read system
# procedures/assets; `is_platform_tenant` must keep gating it). It is
# already covered twice over without adding a third copy here:
# ``READ_MATRIX["sys_published"]``'s ``customer`` cell (above, unchanged)
# and ``test_system_tenant_inheritance.py::test_customer_tenant_never_sees_a_published_system_row``
# (PR #186 itself). Do not revert the gate, and do not re-add a duplicate
# anchor for this cell — reuse those two.

SECOND_CUSTOMER = _sibling(101)


async def _seed_principle(
    session: AsyncSession, *, slug: str, tenant: str, state: str
) -> SimpleNamespace:
    obj = Principle(
        slug=slug,
        title=f"Scenario {slug}",
        statement=f"Statement for {slug}.",
        tenant_id=tenant,
        tenant_name="visibility-matrix-probe",
        subject_id=tenant,
        visibility="tenant",
        rationale=None,
        implications=[],
        tags=[],
        active=True,
        version_status=state,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return SimpleNamespace(id=str(obj.id), title=obj.title)


async def test_system_cannot_read_a_customers_draft_row(test_client, test_session) -> None:
    """User ruling 2026-10-02: the system tenant never reads another tenant's
    data, whatever the row's state (G-B cancelled)."""
    row = await _seed_principle(
        test_session, slug="matrix-customer-draft", tenant=READERS["customer"], state="DRAFT"
    )
    status, listed = await _read(test_client, "system", row.id)
    assert status == HIDDEN, status
    assert not listed


async def test_customer_cannot_read_another_customers_row(test_client, test_session) -> None:
    """A customer tenant (…0100) must not read a DIFFERENT customer's (…0101)
    row, PUBLISHED or not — visibility is per-tenant, not "any non-system"."""
    row = await _seed_principle(
        test_session, slug="matrix-second-customer", tenant=SECOND_CUSTOMER, state="PUBLISHED"
    )
    status, listed = await _read(test_client, "customer", row.id)
    assert status == HIDDEN, status
    assert not listed


async def test_stream_tenant_cannot_read_customer_rows(test_client, seeded) -> None:
    """The stream tenant …0005 (``stream_own``) is a PLATFORM tenant but has
    no relation to a customer's own data — named anchor for
    ``READ_MATRIX["customer_row"]``'s ``stream_own`` cell."""
    status, listed = await _read(test_client, "stream_own", seeded["customer_row"].id)
    assert status == HIDDEN, status
    assert not listed


async def test_customer_cannot_read_system_draft_row(test_client, seeded) -> None:
    """Named anchor for ``READ_MATRIX["sys_draft"]``'s ``customer`` cell.

    CANARY is deliberately NOT modelled here (module docstring, "Not
    modelled"): principles/requirements/decisions never enter CANARY
    (§4.1) and ``VERSION_STATUSES`` in ``schemas/common.py`` is exactly
    {DRAFT, PUBLISHED, DEPRECATED} — seeding CANARY on a ``Principle``
    would not exercise a real reachable state.
    """
    status, listed = await _read(test_client, "customer", seeded["sys_draft"].id)
    assert status == HIDDEN, status
    assert not listed


# ── mutant: include_system ignores state ────────────────────────────────────


async def test_state_ignoring_mutant_goes_red(test_client, seeded, monkeypatch) -> None:
    # The mutant only bites if scope() resolves system_clause through the module
    # globals at call time; prove that before relying on it.
    scope_globals = visibility_mod.scope.__globals__
    assert scope_globals["system_clause"] is visibility_mod.system_clause

    before = [await _read(test_client, r, seeded[row].id) for r, row in STATE_HIDDEN_CELLS]
    assert before == [(HIDDEN, False)] * len(STATE_HIDDEN_CELLS), before

    monkeypatch.setattr(visibility_mod, "system_clause", _stateless_system_clause)
    after = [await _read(test_client, r, seeded[row].id) for r, row in STATE_HIDDEN_CELLS]
    leaks = [
        cell
        for cell, (status, listed) in zip(STATE_HIDDEN_CELLS, after, strict=True)
        if status == R or listed
    ]

    assert leaks, (
        "the state-ignoring mutant leaked NO DRAFT/DEPRECATED system row, so the read "
        f"cells cannot go red. Cells: {STATE_HIDDEN_CELLS}; results: {after}"
    )
