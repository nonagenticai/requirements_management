"""Requirements: user-visibility identity unique indexes (rm#191).

``POST /requirements`` with a ``project_id`` is a get-or-create. For
``visibility='tenant'`` rows the WS1 partial unique indexes (0033 / 0038) are
the race backstop. For ``visibility='user'`` rows there was NO identity index:
dedup relied on the per-tenant slug constraint, which only fires when both
racers run ``EntityService._generate_unique_slug`` before either commits. When
the winner commits first, the loser derives ``<slug>-1``, nothing collides, and
both INSERTs land → a duplicate requirement row (rm#191).

These two indexes are the user-visibility twins of
``uq_requirements_tenant_project_title`` / ``uq_requirements_tenant_project_stable_key``,
additionally keyed on ``subject_id`` because a user-visible row is
subject-private. They cover row sets disjoint from the tenant indexes
(visibility) and from each other (stable_key partition).

FAIL-LOUD, NO RECONCILE: ``upgrade()`` first counts duplicate groups on each key
and raises with the counts if any exist. It never deletes or merges rows; repair
is a separate, human-reviewed step. (Prod had 0 user-visible requirement rows at
authoring time, 2026-10-02.)

Forward-only — pre-prod, no backwards-compat shims.

Chained after rm#193's 0048 (archive), which landed first.

Revision ID: 2026_10_02_0049
Revises: 2026_10_02_0048
Create Date: 2026-10-02
"""

from alembic import op

revision = "2026_10_02_0049"
down_revision = "2026_10_02_0048"
branch_labels = None
depends_on = None

# One (name, key columns, predicate) per index. The pre-check and the CREATE
# INDEX are both rendered from these, so they cannot drift apart.
_TITLE_NAME = "uq_requirements_user_project_title"
_TITLE_KEY = ("tenant_id", "subject_id", "project_id", "title")
_TITLE_PRED = "project_id IS NOT NULL AND stable_key IS NULL AND visibility = 'user'"

_STABLE_NAME = "uq_requirements_user_project_stable_key"
_STABLE_KEY = ("tenant_id", "subject_id", "project_id", "stable_key")
_STABLE_PRED = "stable_key IS NOT NULL AND visibility = 'user'"


def _index_sql(name: str, key: tuple[str, ...], pred: str) -> str:
    return f"CREATE UNIQUE INDEX {name} ON requirements ({', '.join(key)}) WHERE {pred}"


def _dup_groups_sql(key: tuple[str, ...], pred: str) -> str:
    """Exactly the row groups the index would reject.

    The rows the index covers are those matching ``pred``. Among those, a unique
    index is NULLS DISTINCT (the Postgres default): a row with ANY key column NULL
    never conflicts with anything. ``GROUP BY`` instead treats NULLs as equal, so
    without the ``IS NOT NULL`` terms the pre-check would raise on duplicates the
    index accepts (e.g. ``project_id IS NULL`` stable_key rows).
    """
    not_null = " AND ".join(f"{c} IS NOT NULL" for c in key)
    return (
        f"SELECT 1 FROM requirements WHERE ({pred}) AND {not_null} "
        f"GROUP BY {', '.join(key)} HAVING count(*) > 1"
    )


_PRECHECK = f"""
DO $$
DECLARE
    title_groups bigint;
    stable_groups bigint;
BEGIN
    SELECT count(*) INTO title_groups FROM ({_dup_groups_sql(_TITLE_KEY, _TITLE_PRED)}) d;
    SELECT count(*) INTO stable_groups FROM ({_dup_groups_sql(_STABLE_KEY, _STABLE_PRED)}) d;
    IF title_groups > 0 OR stable_groups > 0 THEN
        RAISE EXCEPTION
            'rm#191: duplicate user-visibility requirements exist '
            '(title-key groups=%, stable_key groups=%); dedupe needs a reviewed '
            'plan before this migration can run', title_groups, stable_groups;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(_PRECHECK)
    op.execute(_index_sql(_TITLE_NAME, _TITLE_KEY, _TITLE_PRED))
    op.execute(_index_sql(_STABLE_NAME, _STABLE_KEY, _STABLE_PRED))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_requirements_user_project_stable_key")
    op.execute("DROP INDEX IF EXISTS uq_requirements_user_project_title")
