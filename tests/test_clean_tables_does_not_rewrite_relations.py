"""The between-tests clean must not rewrite any relation.

Why this file exists
--------------------
``tests/conftest.py``'s autouse ``_clean_tables`` fixture used to run
``TRUNCATE <every table> CASCADE`` twice per test. ``TRUNCATE`` is a relation
*rewrite*: it assigns a brand new relfilenode to the table and to every one of
its indexes. PostgreSQL does not unlink the old main fork at commit —
``mdunlink()`` truncates it to zero bytes and defers the real ``unlink(2)`` to
the next checkpoint, so the relfilenumber cannot be recycled underneath WAL
replay. Every truncated relation therefore strands a live inode until a
checkpoint runs.

Measured on this repo 2026-09-10 against a clean local postgres 18.4, running
``tests/test_pm_crud.py`` (90 tests) serially, green, with no interrupt:

    before                    301 files under ``base/<dboid>``
    after                  52,792 files, 52,542 of them zero-length
    after one CHECKPOINT      304 files

That is ~580 stranded inodes per test. Over the ~2,700-test suite it is well
over a million, against a shared Docker VM with 1,048,576 inodes in total —
which is how a green test run took every other container on the box down with
``DiskFull`` at ~53% disk use, and with them every session's pre-push hook.

``DELETE`` does not rewrite the relation, so it creates no files at all. This
file pins that, and it is written against the relfilenode rather than against
the filesystem so it holds in CI, where the test process cannot see the
server's data directory.

⚠️ These assertions run against ``conftest.empty_all_tables`` — the exact
function the autouse fixture calls — not against a re-implementation of it. A
guard that grades a copy cannot fail for a mutation of the original.
"""

import pytest
from sqlalchemy import text

from db import Base
from tests import conftest as _conftest

# Vacuity floor. If the clean order ever comes back near-empty — the failure
# mode this suite has hit before, where a table set is snapshotted before the
# model modules are imported — every assertion below would pass over nothing.
MIN_TABLES = 20

# Tables owned by model modules that ``conftest`` imports *inside* the
# ``test_engine`` fixture, i.e. after ``tests/conftest.py`` itself is imported.
# Naming them explicitly is the point: a clean order computed at conftest
# import time silently omits exactly these, and the symptom is not an error but
# rows leaking into the next test.
LATE_IMPORTED_TABLES = {
    "architecture_elements",
    "principle_conflicts",
    "entity_version_history",
    "toc_nodes",
}


async def _relfilenodes(engine) -> dict[str, int]:
    """relfilenode of every mapped table in the schema this run writes to."""
    names = _conftest._clean_order()
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relfilenode "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relname = ANY(:names)"
                ),
                {"names": names},
            )
        ).all()
    return {name: node for name, node in rows}


def test_the_clean_order_includes_the_late_imported_models():
    """The clean order must be read at call time, not snapshotted at import."""
    order = _conftest._clean_order()

    assert len(order) >= MIN_TABLES, (
        f"only {len(order)} tables in the clean order; expected >= {MIN_TABLES}. "
        "A near-empty order makes every other assertion here vacuous."
    )

    missing = LATE_IMPORTED_TABLES - set(order)
    assert not missing, (
        f"{sorted(missing)} are missing from the between-tests clean order. These "
        "models are imported inside the test_engine fixture, so a clean order "
        "captured at conftest import time cannot see them — and the symptom is "
        "not an error, it is rows surviving into the next test."
    )

    assert set(order) == {tbl.name for tbl in Base.metadata.sorted_tables}


async def test_cleaning_between_tests_rewrites_no_relation(test_engine, test_session):
    """The whole point: emptying the tables must not churn relfilenodes.

    ``TRUNCATE`` moves every one of these numbers. ``DELETE`` moves none.
    """
    from models.requirements import Requirement
    from tests.tenants import T

    # Give the clean real work to do — an empty-table clean would be a weaker
    # test, and a no-op DELETE is not what the fixture faces in practice.
    test_session.add(
        Requirement(
            slug="relfilenode-churn-probe",
            title="Relfilenode churn probe",
            description="Seeded so the between-tests clean has rows to remove",
            tenant_id=T("user-123"),
            project_id="test-project",
            links={},
            acceptance=[],
            effort=1.0,
            reach=1.0,
            impact=1.0,
            confidence=1.0,
        )
    )
    await test_session.commit()

    before = await _relfilenodes(test_engine)
    assert len(before) >= MIN_TABLES, (
        f"only found {len(before)} of the mapped tables in current_schema(); "
        f"expected >= {MIN_TABLES}. Nothing below is being measured."
    )

    await _conftest.empty_all_tables(test_engine)

    after = await _relfilenodes(test_engine)

    assert set(after) == set(before), "the clean added or removed relations"

    rewritten = sorted(name for name in before if before[name] != after[name])
    assert not rewritten, (
        f"{len(rewritten)} relation(s) were rewritten by the between-tests clean: "
        f"{rewritten[:8]}{' …' if len(rewritten) > 8 else ''}. That is TRUNCATE "
        "behaviour. Every rewrite strands the old relfilenode's main fork as a "
        "zero-byte file until the next checkpoint, and this fixture runs twice "
        "per test — which is what exhausted the shared Docker VM's inode table. "
        "Use DELETE, not TRUNCATE. See this module's docstring."
    )


async def test_cleaning_between_tests_actually_empties_every_table(test_engine, test_session):
    """A clean that churns nothing but also removes nothing is not a fix."""
    from models.requirements import Requirement
    from tests.tenants import T

    test_session.add(
        Requirement(
            slug="clean-effectiveness-probe",
            title="Clean effectiveness probe",
            description="Seeded so the clean has something to remove",
            tenant_id=T("user-123"),
            project_id="test-project",
            links={},
            acceptance=[],
            effort=1.0,
            reach=1.0,
            impact=1.0,
            confidence=1.0,
        )
    )
    await test_session.commit()

    async with test_engine.begin() as conn:
        seeded = (await conn.execute(text('SELECT count(*) FROM "requirements"'))).scalar_one()
    assert seeded > 0, "the probe row was not written; the assertion below would be vacuous"

    await _conftest.empty_all_tables(test_engine)

    order = _conftest._clean_order()
    assert len(order) >= MIN_TABLES
    async with test_engine.begin() as conn:
        for name in order:
            remaining = (await conn.execute(text(f'SELECT count(*) FROM "{name}"'))).scalar_one()
            assert remaining == 0, f'"{name}" still holds {remaining} row(s) after the clean'


def test_the_worker_schema_lock_key_is_collision_free():
    """``test_gw1`` and ``test_gw10`` must not share an advisory-lock key.

    The reclaim in ``test_engine`` drops any ``test_gwN`` schema whose advisory
    lock nobody holds. If two worker schemas hashed to the same key, a live
    worker's lock would make its neighbour un-reclaimable — or worse, a dead
    worker's free key would authorise dropping a live schema.
    """
    schemas = [f"test_gw{i}" for i in range(64)]
    keys = [_conftest._schema_lock_key(s) for s in schemas]

    assert len(set(keys)) == len(schemas), "advisory-lock key collision across worker schemas"
    for key in keys:
        assert -(2**63) <= key < 2**63, f"{key} does not fit postgres bigint"


@pytest.mark.parametrize("schema", ["test_gw0", "test_gw7"])
def test_the_worker_schema_lock_key_is_stable(schema):
    """Two processes must derive the same key, or the lock protects nothing."""
    assert _conftest._schema_lock_key(schema) == _conftest._schema_lock_key(schema)
