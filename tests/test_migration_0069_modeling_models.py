"""Migrations 0069/0070: modeling tables + ``model_id`` backfill, on the real alembic chain.

The suite builds its schema with ``Base.metadata.create_all``, so it never exercises the
migration. This runs ``alembic upgrade head`` into a private schema (same machinery and
advisory lock as ``test_rls_db_state_pinned.py``), downgrades to ``0068``, seeds
architecture rows as they existed before the lifecycle, upgrades again and reads what the
backfill produced (target spec §7.1: one default ArchiMate model per (tenant, project,
visibility, subject); ids and links preserved).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from tests.tenants import T
from tests.test_rls_db_state_pinned import (
    _ADVISORY_LOCK_KEY,
    BEFORE_FORWARD_ONLY,
    REPO,
    _base_url,
    _libpq,
    _reclaim_leftovers,
    _run_and_read,
    _with_search_path,
)

TENANT = T("migration-0069")
NEW_TABLES = {
    "modeling_models",
    "modeling_model_revisions",
    "architecture_view_objects",
    "architecture_view_connections",
    # 0070
    "modeling_validation_runs",
    "modeling_validation_findings",
    "modeling_trace_links",
    "modeling_change_sets",
    "modeling_reviews",
    "modeling_publications",
    # 0073
    "uml_elements",
    "uml_relationships",
    "uml_interactions",
    "uml_diagrams",
    "uml_diagram_objects",
    "uml_diagram_connections",
}


def _alembic(base: str, schema: str, *args: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": _with_search_path(base, schema),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO / "src"), os.getenv("PYTHONPATH")])),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"alembic {args} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    )


E_TENANT_P1 = uuid.UUID("01900000-0000-7000-8000-000000000001")
E_TENANT_P1_B = uuid.UUID("01900000-0000-7000-8000-000000000002")
E_PRIVATE_P1 = uuid.UUID("01900000-0000-7000-8000-000000000003")
E_TENANT_NOPROJ = uuid.UUID("01900000-0000-7000-8000-000000000004")
V_TENANT_P1 = uuid.UUID("01900000-0000-7000-8000-000000000005")
R_EDGE = uuid.UUID("01900000-0000-7000-8000-000000000006")
R_ON_REL = uuid.UUID("01900000-0000-7000-8000-000000000007")
R_PM = uuid.UUID("01900000-0000-7000-8000-000000000008")


def _seed(conn: psycopg.Connection, schema: str) -> None:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    for eid, project, visibility, subject in (
        (E_TENANT_P1, "p1", "tenant", "alice"),
        (E_TENANT_P1_B, "p1", "tenant", "bob"),
        (E_PRIVATE_P1, "p1", "user", "carol"),
        (E_TENANT_NOPROJ, None, "tenant", "alice"),
    ):
        conn.execute(
            "INSERT INTO architecture_elements (id, slug, title, tenant_id, tenant_name, "
            "subject_id, visibility, project_id, layer, element_type, status, version, "
            "version_status) VALUES (%s, %s, 't', %s, 'n', %s, %s, %s, 'business', "
            "'business_actor', 'draft', '1.0.0', 'DRAFT')",
            (eid, f"s-{eid}", TENANT, subject, visibility, project),
        )
    conn.execute(
        "INSERT INTO architecture_views (id, slug, title, tenant_id, tenant_name, subject_id, "
        "visibility, project_id, viewpoint, version, version_status) VALUES "
        "(%s, 'v', 'v', %s, 'n', 'bob', 'tenant', 'p1', 'layered', '1.0.0', 'DRAFT')",
        (V_TENANT_P1, TENANT),
    )
    for rid, stype, sid, ttype, tid, rtype in (
        (
            R_EDGE,
            "architecture_elements",
            E_TENANT_P1,
            "architecture_elements",
            E_TENANT_P1_B,
            "association",
        ),
        (
            R_ON_REL,
            "architecture_relationships",
            R_EDGE,
            "architecture_elements",
            E_TENANT_NOPROJ,
            "association",
        ),
        (R_PM, "pm_activities", uuid.uuid4(), "pm_activities", uuid.uuid4(), "finish_to_start"),
    ):
        conn.execute(
            "INSERT INTO architecture_relationships (id, slug, tenant_id, tenant_name, subject_id, "
            "visibility, project_id, source_entity_type, source_id, target_entity_type, target_id, "
            "relationship_type) VALUES (%s, %s, %s, 'n', 'alice', 'tenant', NULL, %s, %s, %s, %s, %s)",
            (rid, f"r-{rid}", TENANT, stype, sid, ttype, tid, rtype),
        )


def _read(conn: psycopg.Connection, schema: str) -> dict:
    conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
    model_of = {
        r[0]: r[1]
        for t in ("architecture_elements", "architecture_views", "architecture_relationships")
        for r in conn.execute(
            sql.SQL("SELECT id, model_id FROM {}").format(sql.Identifier(t))
        ).fetchall()
    }
    models = {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT id, project_id, visibility, subject_id, is_default, language FROM modeling_models"
        ).fetchall()
    }
    nullable = dict(
        conn.execute(
            "SELECT table_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND column_name = 'model_id' AND table_name IN "
            "('architecture_elements', 'architecture_views', 'architecture_relationships')",
            (schema,),
        ).fetchall()
    )
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r' AND c.relrowsecurity",
            (schema,),
        ).fetchall()
    }
    occ_cols = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name IN "
            "('architecture_view_objects', 'architecture_view_connections')",
            (schema,),
        ).fetchall()
    }
    checks = {
        r[0]
        for r in conn.execute(
            "SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = %s AND c.contype = 'c'",
            (schema,),
        ).fetchall()
    }
    return {
        "model_of": model_of,
        "models": models,
        "nullable": nullable,
        "rls_tables": tables,
        "occ_cols": occ_cols,
        "checks": checks,
    }


@pytest.fixture(scope="module")
def backfilled() -> Iterator[dict]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_m69{tag}", f"rls_hide_m69{tag}"
    base = _base_url()
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")  # test-only private connection
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            _reclaim_leftovers(conn)
        try:
            _run_and_read(base, schema, hide, keep=True, target=BEFORE_FORWARD_ONLY)
            _alembic(base, schema, "downgrade", "2026_10_27_0068")
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                _seed(conn, schema)
            _alembic(base, schema, "upgrade", BEFORE_FORWARD_ONLY)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                state = _read(conn, schema)
        finally:
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                conn.execute("SET lock_timeout = '30s'")
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
    finally:
        lock.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        lock.close()
    yield state


def test_new_tables_are_under_rls(backfilled: dict) -> None:
    assert NEW_TABLES <= backfilled["rls_tables"], NEW_TABLES - backfilled["rls_tables"]


def test_model_id_is_not_null_on_elements_and_views_only(backfilled: dict) -> None:
    assert backfilled["nullable"] == {
        "architecture_elements": "NO",
        "architecture_views": "NO",
        "architecture_relationships": "YES",
    }


def test_one_default_model_per_scope(backfilled: dict) -> None:
    m = backfilled["model_of"]
    models = backfilled["models"]
    # Tenant-visible rows of one project share a model whoever created them…
    assert m[E_TENANT_P1] == m[E_TENANT_P1_B] == m[V_TENANT_P1]
    # …a private row and another project get their own.
    assert len({m[E_TENANT_P1], m[E_PRIVATE_P1], m[E_TENANT_NOPROJ]}) == 3
    assert models[m[E_PRIVATE_P1]] == ("p1", "user", "carol", True, "archimate")
    assert models[m[E_TENANT_NOPROJ]][:2] == (None, "tenant")
    assert len(models) == 3 and all(v[3] for v in models.values())


def test_relationships_inherit_their_source_model(backfilled: dict) -> None:
    m = backfilled["model_of"]
    assert m[R_EDGE] == m[E_TENANT_P1]
    assert m[R_ON_REL] == m[R_EDGE]  # relationship-on-relationship source
    assert m[R_PM] is None  # PM edges belong to no model


def test_0071_view_objects_gain_kinds(backfilled: dict) -> None:
    cols = backfilled["occ_cols"]
    obj, conn = "architecture_view_objects", "architecture_view_connections"
    for col in ("kind", "name", "text", "label", "border_type", "documentation"):
        assert (obj, col) in cols, col
    assert cols[(obj, "concept_ref")] == "YES" and cols[(obj, "kind")] == "NO"
    assert cols[(conn, "relationship_ref")] == "YES" and (conn, "label") in cols
    assert {
        "ck_architecture_view_objects_kind",
        "ck_architecture_view_objects_concept_iff_element",
        "ck_architecture_view_objects_documentation_not_element",
        "ck_architecture_view_connections_kind",
        "ck_architecture_view_connections_relationship_iff_kind",
    } <= backfilled["checks"]
