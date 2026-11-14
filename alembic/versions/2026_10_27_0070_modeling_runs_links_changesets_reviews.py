"""Modeling lifecycle part 2: validation runs/findings, trace links, change sets, review/publish.

Target spec 2026-10-26 §7.3 (persisted validation runs and findings), §7.4 (trace links),
§9.5 (change sets); plan 2026-10-26 §7.3 (review and publication, with supersession
history in ``modeling_publications``).

All six tables carry ``tenant_id`` and join the RLS ``tenant_isolation`` backstop with the
strict uuid predicate (same as ``0068`` / ``0069``); none joins ``system_published_read``.
Everything model-scoped cascades with its model, so ``DELETE /modeling/models/{id}?force``
removes it.

Revision ID: 2026_10_27_0070
Revises: 2026_10_27_0069
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_27_0070"
down_revision = "2026_10_27_0069"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"
_TABLES = (
    "modeling_validation_runs",
    "modeling_validation_findings",
    "modeling_trace_links",
    "modeling_change_sets",
    "modeling_reviews",
    "modeling_publications",
)


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True, nullable=False)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False)


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"))


def _fk(name: str, target: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name, sa.Uuid(), sa.ForeignKey(f"{target}.id", ondelete="CASCADE"), nullable=nullable
    )


def upgrade() -> None:
    op.create_table(
        "modeling_validation_runs",
        _id(),
        _tenant(),
        _fk("model_id", "modeling_models"),
        sa.Column("target", sa.String(20), nullable=False),
        _fk("revision_id", "modeling_model_revisions", nullable=True),
        sa.Column("head", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("ruleset_id", sa.String(100), nullable=False),
        sa.Column("ruleset_version", sa.String(50), nullable=False),
        sa.Column("enabled_rules", postgresql.JSONB(), nullable=False),
        sa.Column("required_rules", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("examined", postgresql.JSONB(), nullable=False),
        sa.Column("expected", postgresql.JSONB(), nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
        sa.Column("clean", sa.Boolean(), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target IN ('revision', 'draft_head')", name="ck_modeling_validation_runs_target"
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'incomplete', 'failed')",
            name="ck_modeling_validation_runs_status",
        ),
    )
    op.create_table(
        "modeling_validation_findings",
        _id(),
        _tenant(),
        _fk("run_id", "modeling_validation_runs"),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.String(50), nullable=False),
        sa.Column("ruleset_id", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("severity_rank", sa.Integer(), nullable=False),
        sa.Column("targets", postgresql.JSONB(), nullable=False),
        sa.Column("target_ids", postgresql.JSONB(), nullable=False),
        sa.Column("first_target_id", sa.String(100), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_modeling_validation_findings_order",
        "modeling_validation_findings",
        ["run_id", "severity_rank", "rule_id", "first_target_id", "seq"],
    )
    op.create_table(
        "modeling_trace_links",
        _id(),
        _tenant(),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("visibility", sa.String(16), server_default="tenant", nullable=False),
        _fk("model_id", "modeling_models", nullable=True),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(30), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("target_code", postgresql.JSONB(), nullable=True),
        sa.Column("link_type", sa.String(20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        _now("created_at"),
        sa.CheckConstraint(
            "visibility IN ('tenant', 'user')", name="ck_modeling_trace_links_visibility"
        ),
        sa.CheckConstraint(
            "(target_id IS NOT NULL AND target_kind IS NOT NULL AND target_code IS NULL) "
            "OR (target_id IS NULL AND target_kind IS NULL AND target_code IS NOT NULL)",
            name="ck_modeling_trace_links_one_target",
        ),
        sa.CheckConstraint(
            "link_type IN ('realizes', 'refines', 'traces_to', 'satisfies', 'implements', "
            "'depends_on')",
            name="ck_modeling_trace_links_type",
        ),
    )
    for col in ("source_id", "target_id"):
        op.create_index(f"ix_modeling_trace_links_{col}", "modeling_trace_links", [col])
    op.create_table(
        "modeling_change_sets",
        _id(),
        _tenant(),
        _fk("model_id", "modeling_models"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("ops", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evaluator_ref", sa.String(300), nullable=True),
        sa.Column("base_head", sa.Integer(), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("blocking_findings", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        _now("created_at"),
        sa.Column("applied_by", sa.String(255), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_head", sa.Integer(), nullable=True),
        sa.Column("id_map", postgresql.JSONB(), nullable=True),
        sa.Column("rejected_by", sa.String(255), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposed', 'invalid', 'applied', 'rejected')",
            name="ck_modeling_change_sets_status",
        ),
    )
    op.create_table(
        "modeling_reviews",
        _id(),
        _tenant(),
        _fk("model_id", "modeling_models"),
        _fk("revision_id", "modeling_model_revisions"),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(255), nullable=False),
        _now("created_at"),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')", name="ck_modeling_reviews_decision"
        ),
    )
    op.create_table(
        "modeling_publications",
        _id(),
        _tenant(),
        _fk("model_id", "modeling_models"),
        _fk("revision_id", "modeling_model_revisions"),
        sa.Column("supersedes_revision_id", sa.Uuid(), nullable=True),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("validation_run_id", sa.Uuid(), nullable=False),
        sa.Column("published_by", sa.String(255), nullable=False),
        _now("published_at"),
    )
    for table, cols in (
        ("modeling_validation_runs", ("tenant_id", "model_id", "revision_id", "content_hash")),
        ("modeling_validation_findings", ("tenant_id", "run_id")),
        ("modeling_trace_links", ("tenant_id", "model_id")),
        ("modeling_change_sets", ("tenant_id", "model_id")),
        ("modeling_reviews", ("tenant_id", "model_id", "revision_id")),
        ("modeling_publications", ("tenant_id", "model_id")),
    ):
        for col in cols:
            op.create_index(f"ix_{table}_{col}", table, [col])

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
