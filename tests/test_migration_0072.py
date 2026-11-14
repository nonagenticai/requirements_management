"""Migration 0072 on the real alembic chain: folder trees + nullable viewpoint.

Upgrade: ``modeling_models.organizations`` (JSONB, nullable) exists, under the table's
existing RLS policy; ``architecture_views.viewpoint`` accepts NULL. Downgrade: views
without a viewpoint get ``layered``, NOT NULL returns, the trees go. Upgrade again keeps
existing values. Same private-schema machinery as ``test_migration_0069_modeling_models``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from tests.tenants import T
from tests.test_migration_0069_modeling_models import _alembic
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    BEFORE_FORWARD_ONLY,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
)

TENANT = T("migration-0072")
MODEL = uuid.UUID("01900000-0000-7000-8000-000000000072")
VIEW_NONE = uuid.UUID("01900000-0000-7000-8000-000000000073")
VIEW_SET = uuid.UUID("01900000-0000-7000-8000-000000000074")
TREE = [
    {"id": "f", "name": "Views", "documentation": None, "items": [str(VIEW_NONE)], "children": []}
]


def _state(conn: psycopg.Connection, schema: str) -> dict:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    cols = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND ((table_name = 'modeling_models' AND "
            "column_name = 'organizations') OR (table_name = 'architecture_views' AND "
            "column_name = 'viewpoint'))",
            (schema,),
        ).fetchall()
    }
    views = dict(
        conn.execute("SELECT id, viewpoint FROM architecture_views ORDER BY id").fetchall()
    )
    rls = conn.execute(
        "SELECT relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = 'modeling_models'",
        (schema,),
    ).fetchone()
    tree = None
    if ("modeling_models", "organizations") in cols:
        tree = conn.execute(
            "SELECT organizations FROM modeling_models WHERE id = %s", (MODEL,)
        ).fetchone()
    return {"cols": cols, "views": views, "rls": rls[0] if rls else None, "tree": tree}


def _seed(conn: psycopg.Connection, schema: str) -> None:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    conn.execute(
        "INSERT INTO modeling_models (id, tenant_id, visibility, subject_id, language, "
        "language_version, title, head, is_default, organizations) VALUES "
        "(%s, %s, 'tenant', 'alice', 'archimate', '3.2', 'm', 0, false, %s)",
        (MODEL, TENANT, json.dumps(TREE)),
    )
    for vid, viewpoint in ((VIEW_NONE, None), (VIEW_SET, "motivation")):
        conn.execute(
            "INSERT INTO architecture_views (id, slug, title, tenant_id, tenant_name, "
            "subject_id, visibility, project_id, viewpoint, version, version_status, model_id) "
            "VALUES (%s, %s, 'v', %s, 'n', 'alice', 'tenant', NULL, %s, '1.0.0', 'DRAFT', %s)",
            (vid, f"v-{vid}", TENANT, viewpoint, MODEL),
        )


@pytest.fixture(scope="module")
def states() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m72{tag}", f"rls_hide_m72{tag}"
    base = _base_url()
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")  # test-only private connection
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    out: dict = {}
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            _reclaim_leftovers(conn)
        try:
            _run_and_read(base, schema, hide, keep=True, target=BEFORE_FORWARD_ONLY)  # upgrade head
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                _seed(conn, schema)
                out["up"] = _state(conn, schema)
            _alembic(base, schema, "downgrade", "2026_10_27_0071")
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["down"] = _state(conn, schema)
            _alembic(base, schema, "upgrade", BEFORE_FORWARD_ONLY)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["again"] = _state(conn, schema)
        finally:
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                conn.execute("SET lock_timeout = '30s'")
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()
    yield out


def test_upgrade_adds_tree_and_nullable_viewpoint(states: dict) -> None:
    up = states["up"]
    assert up["cols"] == {
        ("modeling_models", "organizations"): "YES",
        ("architecture_views", "viewpoint"): "YES",
    }
    assert up["rls"] is True
    assert up["views"] == {VIEW_NONE: None, VIEW_SET: "motivation"}
    assert up["tree"] is not None and up["tree"][0] == TREE


def test_downgrade_restores_not_null_and_drops_trees(states: dict) -> None:
    down = states["down"]
    assert down["cols"] == {("architecture_views", "viewpoint"): "NO"}
    assert down["views"] == {VIEW_NONE: "layered", VIEW_SET: "motivation"}


def test_upgrade_again_keeps_existing_values(states: dict) -> None:
    again = states["again"]
    assert again["cols"][("architecture_views", "viewpoint")] == "YES"
    assert again["views"] == {VIEW_NONE: "layered", VIEW_SET: "motivation"}
    assert again["tree"] == (None,)
