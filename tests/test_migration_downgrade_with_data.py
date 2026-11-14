"""The modeling migrations (0069-0073) on the real alembic chain, WITH data present.

One private schema (machinery of ``test_rls_db_state_pinned.py``):

1. ``upgrade head``; the ORM metadata is compared with the migrated schema
   (``alembic.autogenerate.compare_metadata``) for every table and column these
   migrations own — drift between ``src/models`` and the migrations fails here;
2. data in every modeling shape is seeded — an ArchiMate model with a folder tree,
   elements, a relationship, a view with an element occurrence, a note, a group and a
   line, a UML model with an element, diagram and object, a revision, a validation run
   with a finding, a change set, a review, a publication and trace links (one from an
   ArchiMate element to a UML element);
3. ``downgrade 2026_10_27_0068`` succeeds and keeps the pre-lifecycle rows;
4. ``upgrade head`` succeeds again, and the metadata comparison is still clean.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine

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

TENANT = T("migration-downgrade-data")

# Tables these migrations create, and the columns they add to older tables.
OWNED_TABLES = frozenset(
    {
        "modeling_models",
        "modeling_model_revisions",
        "architecture_view_objects",
        "architecture_view_connections",
        "modeling_validation_runs",
        "modeling_validation_findings",
        "modeling_trace_links",
        "modeling_change_sets",
        "modeling_reviews",
        "modeling_publications",
        "uml_elements",
        "uml_relationships",
        "uml_interactions",
        "uml_diagrams",
        "uml_diagram_objects",
        "uml_diagram_connections",
    }
)
OWNED_COLUMNS = frozenset(
    {
        ("architecture_elements", "model_id"),
        ("architecture_relationships", "model_id"),
        ("architecture_views", "model_id"),
        ("architecture_views", "viewpoint"),
    }
)
# A functional unique index: alembic reflects its expressions as text and cannot compare
# them to the model's (a known autogenerate limitation), so it would always "differ".
EXPRESSION_INDEXES = frozenset({"uq_modeling_models_default_scope"})


def _owned(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table":
        return name in OWNED_TABLES
    if type_ == "index" and name in EXPRESSION_INDEXES:
        return False
    table = getattr(getattr(obj, "table", None), "name", None)
    if type_ == "column":
        return table in OWNED_TABLES or (table, name) in OWNED_COLUMNS
    return table in OWNED_TABLES


def _drift(base: str, schema: str) -> list[Any]:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import main  # noqa: F401  (registers every model on Base.metadata)
    from db import Base

    engine = create_engine(base)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f'SET search_path = "{schema}", public')
            ctx = MigrationContext.configure(
                conn,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": _owned,
                },
            )
            return list(compare_metadata(ctx, Base.metadata))
    finally:
        engine.dispose()


IDS = {
    k: uuid.uuid4() for k in "M U E1 E2 R V O1 ON OG L UE UD UO REV RUN F CS RV PUB TL1 TL2".split()
}


def _seed(conn: psycopg.Connection) -> None:
    i = IDS
    tree = [
        {"id": "f", "name": "Views", "documentation": None, "items": [str(i["V"])], "children": []}
    ]
    conn.execute(
        "INSERT INTO modeling_models (id, tenant_id, visibility, subject_id, language, "
        "language_version, title, head, is_default, organizations) VALUES "
        "(%s, %s, 'tenant', 'alice', 'archimate', '3.2', 'a', 1, false, %s), "
        "(%s, %s, 'tenant', 'alice', 'uml', '2.5.1-p1', 'u', 1, false, NULL)",
        (i["M"], TENANT, json.dumps(tree), i["U"], TENANT),
    )
    for eid in (i["E1"], i["E2"]):
        conn.execute(
            "INSERT INTO architecture_elements (id, slug, title, tenant_id, tenant_name, "
            "subject_id, visibility, layer, element_type, status, version, version_status, "
            "model_id) VALUES (%s, %s, 't', %s, 'n', 'alice', 'tenant', 'business', "
            "'business_actor', 'draft', '1.0.0', 'DRAFT', %s)",
            (eid, f"s-{eid}", TENANT, i["M"]),
        )
    conn.execute(
        "INSERT INTO architecture_relationships (id, slug, tenant_id, tenant_name, subject_id, "
        "visibility, source_entity_type, source_id, target_entity_type, target_id, "
        "relationship_type, model_id) VALUES (%s, 'r', %s, 'n', 'alice', 'tenant', "
        "'architecture_elements', %s, 'architecture_elements', %s, 'association', %s)",
        (i["R"], TENANT, i["E1"], i["E2"], i["M"]),
    )
    conn.execute(
        "INSERT INTO architecture_views (id, slug, title, tenant_id, tenant_name, subject_id, "
        "visibility, viewpoint, version, version_status, model_id) VALUES "
        "(%s, 'v', 'v', %s, 'n', 'alice', 'tenant', NULL, '1.0.0', 'DRAFT', %s)",
        (i["V"], TENANT, i["M"]),
    )
    conn.execute(
        "INSERT INTO architecture_view_objects (id, tenant_id, model_id, view_id, kind, "
        "concept_ref, parent_object_id, name, text, position) VALUES "
        "(%s, %s, %s, %s, 'group', NULL, NULL, 'G', NULL, 0), "
        "(%s, %s, %s, %s, 'element', %s, %s, NULL, NULL, 1), "
        "(%s, %s, %s, %s, 'note', NULL, NULL, NULL, 'a note', 2)",
        (
            *(i["OG"], TENANT, i["M"], i["V"]),
            *(i["O1"], TENANT, i["M"], i["V"], i["E1"], i["OG"]),
            *(i["ON"], TENANT, i["M"], i["V"]),
        ),
    )
    conn.execute(
        "INSERT INTO architecture_view_connections (id, tenant_id, model_id, view_id, kind, "
        "relationship_ref, source_object_id, target_object_id) VALUES "
        "(%s, %s, %s, %s, 'line', NULL, %s, %s)",
        (i["L"], TENANT, i["M"], i["V"], i["ON"], i["O1"]),
    )
    conn.execute(
        "INSERT INTO uml_elements (id, tenant_id, subject_id, visibility, model_id, kind, "
        "name, attributes) VALUES (%s, %s, 'alice', 'tenant', %s, 'class', 'C', '{}')",
        (i["UE"], TENANT, i["U"]),
    )
    conn.execute(
        "INSERT INTO uml_diagrams (id, tenant_id, subject_id, visibility, model_id, kind, name) "
        "VALUES (%s, %s, 'alice', 'tenant', %s, 'class', 'D')",
        (i["UD"], TENANT, i["U"]),
    )
    conn.execute(
        "INSERT INTO uml_diagram_objects (id, tenant_id, model_id, diagram_id, element_ref, "
        "position) VALUES (%s, %s, %s, %s, %s, 0)",
        (i["UO"], TENANT, i["U"], i["UD"], i["UE"]),
    )
    conn.execute(
        "INSERT INTO modeling_model_revisions (id, tenant_id, model_id, seq, manifest, "
        "content_hash, head, sealed_by) VALUES (%s, %s, %s, 1, '{}', 'h', 1, 'alice')",
        (i["REV"], TENANT, i["U"]),
    )
    conn.execute(
        "INSERT INTO modeling_validation_runs (id, tenant_id, model_id, target, revision_id, "
        "head, content_hash, ruleset_id, ruleset_version, enabled_rules, required_rules, "
        "status, examined, expected, counts, clean, requested_by) VALUES "
        "(%s, %s, %s, 'revision', %s, 1, 'h', 'uml', '1', '[]', '[]', 'complete', '{}', "
        "'{}', '{}', true, 'alice')",
        (i["RUN"], TENANT, i["U"], i["REV"]),
    )
    conn.execute(
        "INSERT INTO modeling_validation_findings (id, tenant_id, run_id, seq, rule_id, "
        "ruleset_id, severity, severity_rank, targets, target_ids, first_target_id, message) "
        "VALUES (%s, %s, %s, 0, 'X', 'uml', 'warning', 1, '[]', '{}', '', 'm')",
        (i["F"], TENANT, i["RUN"]),
    )
    conn.execute(
        "INSERT INTO modeling_change_sets (id, tenant_id, model_id, status, ops, rationale, "
        "base_head, findings, blocking_findings, created_by) VALUES "
        "(%s, %s, %s, 'proposed', '[]', 'r', 1, '[]', '[]', 'alice')",
        (i["CS"], TENANT, i["U"]),
    )
    conn.execute(
        "INSERT INTO modeling_reviews (id, tenant_id, model_id, revision_id, decision, actor) "
        "VALUES (%s, %s, %s, %s, 'approve', 'bob')",
        (i["RV"], TENANT, i["U"], i["REV"]),
    )
    conn.execute(
        "INSERT INTO modeling_publications (id, tenant_id, model_id, revision_id, review_id, "
        "validation_run_id, published_by) VALUES (%s, %s, %s, %s, %s, %s, 'bob')",
        (i["PUB"], TENANT, i["U"], i["REV"], i["RV"], i["RUN"]),
    )
    for tl, model, skind, sid, tkind, tid in (
        (i["TL1"], i["M"], "element", i["E1"], "uml_element", i["UE"]),
        (i["TL2"], i["M"], "element", i["E1"], "element", i["E2"]),
    ):
        conn.execute(
            "INSERT INTO modeling_trace_links (id, tenant_id, subject_id, visibility, model_id, "
            "source_kind, source_id, target_kind, target_id, link_type, created_by) VALUES "
            "(%s, %s, 'alice', 'tenant', %s, %s, %s, %s, %s, 'traces_to', 'alice')",
            (tl, TENANT, model, skind, sid, tkind, tid),
        )


def _count(conn: psycopg.Connection, schema: str, table: str) -> int | None:
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchone()
    if exists is None:
        return None
    row = conn.execute(
        sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    ).fetchone()
    assert row is not None
    return int(row[0])


def _snapshot(conn: psycopg.Connection, schema: str) -> dict[str, int | None]:
    tables = ("architecture_elements", "architecture_relationships", "architecture_views")
    out = {t: _count(conn, schema, t) for t in (*tables, *sorted(OWNED_TABLES))}
    out["alembic_version"] = None
    version = conn.execute(
        sql.SQL("SELECT version_num FROM {}.alembic_version").format(sql.Identifier(schema))
    ).fetchone()
    out["_version"] = version[0] if version else None  # type: ignore[assignment]
    return out


@pytest.fixture(scope="module")
def chain() -> Iterator[dict[str, Any]]:
    tag = uuid.uuid4().hex[:12]
    schema, hide = f"rls_state_mdd{tag}", f"rls_hide_mdd{tag}"
    base = _base_url()
    lock = psycopg.connect(_libpq(base), autocommit=True)
    lock.execute("SET lock_timeout = '15min'")  # test-only private connection
    lock.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    out: dict[str, Any] = {}
    try:
        with psycopg.connect(_libpq(base), autocommit=True) as conn:
            conn.execute("SET lock_timeout = '30s'")
            _reclaim_leftovers(conn)
        try:
            _run_and_read(base, schema, hide, keep=True, target=BEFORE_FORWARD_ONLY)  # upgrade head
            out["drift_head"] = _drift(base, schema)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
                _seed(conn)
                out["seeded"] = _snapshot(conn, schema)
            _alembic(base, schema, "downgrade", "2026_10_27_0072")
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                conn.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(schema)))
                out["at_0072"] = {
                    "languages": sorted(
                        r[0] for r in conn.execute("SELECT language FROM modeling_models")
                    ),
                    "links": sorted(
                        str(r[0]) for r in conn.execute("SELECT id FROM modeling_trace_links")
                    ),
                    **{
                        t: _count(conn, schema, t)
                        for t in (
                            "modeling_model_revisions",
                            "modeling_validation_runs",
                            "modeling_validation_findings",
                            "modeling_change_sets",
                            "modeling_reviews",
                            "modeling_publications",
                        )
                    },
                }
            _alembic(base, schema, "downgrade", "2026_10_27_0068")
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["down"] = _snapshot(conn, schema)
            _alembic(base, schema, "upgrade", BEFORE_FORWARD_ONLY)
            with psycopg.connect(_libpq(base), autocommit=True) as conn:
                out["again"] = _snapshot(conn, schema)
            out["drift_again"] = _drift(base, schema)
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


def test_models_match_the_migrations(chain: dict[str, Any]) -> None:
    assert chain["drift_head"] == []


def test_seed_reached_every_modeling_table(chain: dict[str, Any]) -> None:
    seeded = chain["seeded"]
    for table in (
        "modeling_models",
        "architecture_view_objects",
        "architecture_view_connections",
        "uml_elements",
        "uml_diagrams",
        "uml_diagram_objects",
        "modeling_model_revisions",
        "modeling_validation_runs",
        "modeling_validation_findings",
        "modeling_change_sets",
        "modeling_reviews",
        "modeling_publications",
        "modeling_trace_links",
    ):
        assert seeded[table], table


def test_downgrade_0073_removes_uml_models_and_what_belongs_to_them(
    chain: dict[str, Any],
) -> None:
    at = chain["at_0072"]
    assert at["languages"] == ["archimate"]
    # The ArchiMate-to-ArchiMate link stays; the one ending on a UML element goes.
    assert at["links"] == [str(IDS["TL2"])]
    for table in (
        "modeling_model_revisions",
        "modeling_validation_runs",
        "modeling_validation_findings",
        "modeling_change_sets",
        "modeling_reviews",
        "modeling_publications",
    ):
        assert at[table] == 0, table


def test_downgrade_with_data_keeps_the_pre_lifecycle_rows(chain: dict[str, Any]) -> None:
    down = chain["down"]
    assert down["_version"] == "2026_10_27_0068"
    assert all(down[t] is None for t in OWNED_TABLES), down
    assert down["architecture_elements"] == chain["seeded"]["architecture_elements"] == 2
    assert down["architecture_relationships"] == 1
    assert down["architecture_views"] == 1


def test_upgrade_again_is_clean(chain: dict[str, Any]) -> None:
    again = chain["again"]
    assert again["_version"] == chain["seeded"]["_version"]
    assert again["architecture_elements"] == 2 and again["architecture_views"] == 1
    assert again["uml_elements"] == 0 and again["modeling_change_sets"] == 0
    assert chain["drift_again"] == []
