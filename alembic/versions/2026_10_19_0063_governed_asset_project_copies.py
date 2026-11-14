"""Governed assets: one live project copy per (original, project) (G8 / R313).

``uq_<table>_tenant_supersedes_origin_project`` on ``principles``, ``requirements`` and
``decisions``: UNIQUE ``(tenant_id, supersedes, origin_project_id)`` WHERE ``supersedes IS NOT
NULL AND origin_project_id IS NOT NULL`` — one project copy per (original, project), for life
(a promoted copy is revived by the next project-copy, never duplicated). It is the race guard
behind ``POST /{kind}/{id}/project-copy`` (``GovernedAssetService.project_copy``): two
concurrent copies of one original for one project collide, and the loser updates the winner.

Measured on prod 2026-10-19 (worker4, read-only): ``supersedes`` is NULL on every row of all
three tables, so no existing row is covered. The pre-check still refuses to build the index
over duplicates rather than failing half-way with an opaque error.

Forward-only — pre-prod, no rollback shims (the downgrade only drops what this adds).

Revision ID: 2026_10_19_0063
Revises: 2026_10_19_0062
Create Date: 2026-10-19
"""

from alembic import op

revision = "2026_10_19_0063"
down_revision = "2026_10_19_0062"
branch_labels = None
depends_on = None

_TABLES = ("principles", "requirements", "decisions")
_KEY = "tenant_id, supersedes, origin_project_id"
_PRED = "supersedes IS NOT NULL AND origin_project_id IS NOT NULL"


def _name(table: str) -> str:
    return f"uq_{table}_tenant_supersedes_origin_project"


def _precheck(table: str) -> str:
    return f"""
DO $$
DECLARE
    groups bigint;
BEGIN
    SELECT count(*) INTO groups FROM (
        SELECT 1 FROM {table} WHERE {_PRED} AND tenant_id IS NOT NULL
        GROUP BY {_KEY} HAVING count(*) > 1
    ) d;
    IF groups > 0 THEN
        RAISE EXCEPTION 'G8: % duplicate live project copies in {table}; resolve before migrating',
            groups;
    END IF;
END
$$;
"""


def upgrade() -> None:
    for table in _TABLES:
        op.execute(_precheck(table))
        op.execute(f"CREATE UNIQUE INDEX {_name(table)} ON {table} ({_KEY}) WHERE {_PRED}")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS {_name(table)}")
