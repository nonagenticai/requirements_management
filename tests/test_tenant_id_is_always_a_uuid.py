"""``tenant_id`` is a ``uuid`` column. A NAME is an alias, resolved before the write.

Phase 4 of the UUIDv7 plan that
``alembic/versions/2026_04_27_0019_add_tenant_name_column.py`` declared and never shipped.
A name is still a first-class thing to SEND — ``src/auth/tenant_resolution.py`` resolves it
against the licences registry (``tenants(id uuid, name varchar, keycloak_realm varchar)``;
the identifier is ``id``, ``name`` is an alias) and hands this service the UUID. What is
pinned here is what gets STORED: always the UUID, never the label.

Why, measured in production ``requirements_api``: the SAME tenant was split across BOTH
identifiers — 60 rows keyed ``n8n-dev`` and 157 rows keyed
``019de842-c92a-7e37-8fdd-eb0d8c5396f9``, in the same tables. Every ``WHERE tenant_id``
predicate, every uniqueness constraint and the RLS policy saw two tenants where there is
one. ``tenant_name`` already carried the original label on every affected row, so the
repair lost nothing.

RETRACTION — this file REPLACES ``tests/test_tenant_id_may_be_a_name.py`` (PR #129,
2026-08-24), which pinned the opposite. #129's facts are kept: licences resolves both
spellings at ``GET /tenants/{name_or_id}/byo-mode``, ``n8n-dev`` is a live tenant owning
principles here, and a UUID SHAPE test is wrong in both directions — it skips a live named
tenant and still admits a UUID-shaped orphan resolving to nothing
(``ad976f5b-f3a7-4441-ae87-c2090d038f9c`` -> 404). Only its conclusion was too wide: "do not
reject a name" is a claim about the REQUEST BOUNDARY, not about the COLUMN, and applying it
to the column is what produced the split above. #129's discriminator — RESOLVABILITY against
licences, not shape; a network call, not a regex — is preserved, and now runs once in
``tenant_resolution`` before the write. (Its control — the conflict reconciler still sweeping
the UUID ``n8n-dev`` resolves to — was removed with the reconciler, 2026-10-26.)

BEHAVIOURAL, not a source grep. #129 recorded that its OWN first version asserted on module
source (no ``uuid.UUID(``, no dashed-hex regex) and was vacuous: an aliased
``import uuid as _u`` walked straight past it. A source pattern can always be spelled
around. A column type read off live metadata, and a database that raises, cannot be.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
import sqlalchemy as sa
import sqlalchemy.exc

import models
from db import Base
from models.principles import Principle

# Import every model module so ``Base.metadata`` is fully populated before it is read —
# the same reason ``alembic/versions/2026_07_02_0030_rls_tenant_isolation_backstop.py``
# does this: ``env.py`` imports only a SUBSET (it omits ``models.toc``), so an ambient
# metadata read would silently skip whole tables and pass by looking at nothing.
for _m in pkgutil.iter_modules(models.__path__):
    importlib.import_module(f"models.{_m.name}")

# The measured resolution of the live tenant name ``n8n-dev`` against licences, and the
# UUID half of the production split. Hard-coded on purpose: it is a measurement, not a
# label.
N8N_DEV_UUID = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"

# The string half of the same split — a real value that really was in the column.
A_TENANT_NAME = "n8n-dev"

# The RLS backstop covered 19 tables at 2026_07_02_0030 and the estate has grown since.
# A floor makes the metadata sweep non-vacuous: an import loop that resolved nothing would
# otherwise iterate an empty set and pass.
_MIN_TENANT_TABLES = 19


async def _mk_principles(session, tenant: str, count: int) -> list[Principle]:
    rows = [
        Principle(
            slug=f"{tenant}-{i:05d}",
            title=f"Principle {i}",
            statement=f"Statement {i}",
            tenant_id=tenant,
            tenant_name=tenant,
            subject_id=f"subject-{tenant}",
            rationale="r",
            implications=[],
            tags=[],
            active=True,
        )
        for i in range(count)
    ]
    session.add_all(rows)
    await session.commit()
    return rows


class TestEveryTenantIdColumnIsUuid:
    """Read the column TYPE off live metadata, so a NEW table cannot opt out silently."""

    def test_every_table_carrying_tenant_id_declares_it_as_uuid(self):
        offenders: list[str] = []
        covered: list[str] = []

        for table in Base.metadata.sorted_tables:
            column = table.columns.get("tenant_id")
            if column is None:
                continue
            covered.append(table.name)
            if not isinstance(column.type, sa.Uuid):
                offenders.append(f"{table.name}.tenant_id is {column.type!r}, not sa.Uuid")

        assert len(covered) >= _MIN_TENANT_TABLES, (
            f"only {len(covered)} tables with a tenant_id were found "
            f"({sorted(covered)}) — fewer than the {_MIN_TENANT_TABLES} the RLS backstop "
            "already covered. Base.metadata is under-populated, so this whole sweep is "
            "looking at almost nothing; fix the model imports above before trusting a pass."
        )
        assert not offenders, (
            "a tenant_id column is not a uuid:\n  "
            + "\n  ".join(offenders)
            + "\n\nEvery owned table stores the RESOLVED tenant UUID. A String column cannot "
            "express that a name and its UUID are the same customer — in production that "
            f"split one tenant into 60 rows under {A_TENANT_NAME!r} and 157 under "
            f"{N8N_DEV_UUID!r}, invisible to each other through every WHERE tenant_id, every "
            "unique constraint and the RLS policy. A NAME belongs at the request boundary "
            "(src/auth/tenant_resolution.py resolves it against licences); it is never "
            "stored. Use sa.Uuid(as_uuid=False) — the Python side still speaks str."
        )


class TestTheDatabaseRejectsANonUuidTenantId:
    """The mutation-proof leg: this cannot be spelled around, only reverted.

    A type annotation can be widened and a docstring can be rewritten. A postgres column
    that raises ``invalid input syntax for type uuid`` cannot be talked out of it — which
    is also why tolerating a name in the column is no longer merely discouraged but
    impossible, GUC binding for RLS included.
    """

    async def test_orm_insert_of_a_named_tenant_raises(self, test_session_factory):
        async with test_session_factory() as session:
            session.add(
                Principle(
                    slug="n8n-dev-00001",
                    title="Principle",
                    statement="Statement",
                    tenant_id=A_TENANT_NAME,
                    tenant_name=A_TENANT_NAME,
                    subject_id="subject-n8n-dev",
                    rationale="r",
                    implications=[],
                    tags=[],
                    active=True,
                )
            )
            with pytest.raises((sa.exc.StatementError, sa.exc.DBAPIError, ValueError)) as exc:
                await session.flush()

        assert "uuid" in str(exc.value).lower(), (
            f"the write of tenant_id={A_TENANT_NAME!r} failed, but not because the value is "
            f"not a uuid: {exc.value!r}. A pass here must come from the column type, not "
            "from an unrelated constraint that might be relaxed tomorrow."
        )

    async def test_the_database_itself_rejects_it_not_just_the_orm(
        self, test_session, test_session_factory
    ):
        """Bypass SQLAlchemy's bind processor: the rejection must come from postgres.

        The ORM leg above could in principle be satisfied by a Python-side coercion that a
        raw write would walk past — a repair script, a bulk COPY or an ``op.execute`` in a
        migration all reach the table without going through ``sa.Uuid``.
        """
        (seeded,) = await _mk_principles(test_session, N8N_DEV_UUID, 1)

        async with test_session_factory() as session:
            with pytest.raises(sa.exc.DBAPIError) as exc:
                await session.execute(
                    sa.text(
                        "UPDATE principles SET tenant_id = 'n8n-dev' WHERE id = :id"
                    ).bindparams(id=seeded.id)
                )

        assert "uuid" in str(exc.value).lower(), (
            f"postgres refused the raw UPDATE for the wrong reason: {exc.value!r}"
        )

        await test_session.rollback()
        surviving = await test_session.scalar(
            sa.select(sa.func.count())
            .select_from(Principle)
            .where(Principle.tenant_name == N8N_DEV_UUID)
        )
        assert surviving == 1, (
            "the raw UPDATE raised but the row did not survive intact — the assertion above "
            "would then be proving a deletion rather than a rejected write"
        )
