"""PM projects: the full fr project intake + versioned briefs (George 2026-10-13).

George's flow: the user creates a project in fr → fr stores it in RM ``pm_projects`` → fr
calls the conveyor webhook with ONLY the RM project id → n8n reads everything the user
provided from RM in one ``GET /pm/projects/{eid}``. And: RM stores the project BRIEF, the
conveyor's enhancements to it are stored in RM too, and fr shows the brief WITH VERSIONS.

``pm_projects`` gains nullable intake columns (legacy rows stay NULL; shapes are validated
by ``schemas.pm_intake``, which mirrors fr's zod contract):

* ``repositories`` jsonb, ``repository_id`` uuid, ``coordination_mode`` / ``scope_level`` /
  ``shape`` / ``question_mode`` / ``lifecycle_class`` varchar(32), ``expected_size``
  varchar(1) (``ck_pm_projects_expected_size``: S, M, L), ``requested_by`` varchar(255),
  ``github_token_secret_id`` uuid (an id only — never a token), ``fixture`` bool,
  ``it_inputs`` / ``attachments`` / ``bound_assets`` jsonb;
* ``priority_level_id`` → ``pm_priority_levels``, ON DELETE RESTRICT (the catalogue API
  refuses deleting a level in use with 409 first);
* ``current_brief_version`` int (``ck_pm_projects_current_brief_version``: ≥ 1) and
  ``intake_updated_at``.

``pm_project_brief_versions`` — append-only, one FULL brief snapshot per version, NOT NULL
uuid ``tenant_id``, ``project_id`` CASCADE, UNIQUE ``(project_id, version)``, CHECKs on
``version >= 1``, ``author_kind IN ('user', 'conveyor')``, ``author_kind_source IN
('delegated', 'conveyor_subject', 'declared')`` (how ``author_kind`` was established: fr
delegation, an ``AUTH_CONVEYOR_SUBJECTS`` principal, or the body alone) and ``note`` ≤ 2000. It joins the
RLS tenant-isolation backstop with the strict uuid predicate (same as ``2026_10_13_0053``);
it does not join ``system_published_read``.

No backfill: there is nothing to backfill (no brief was stored before).

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_10_13_0056
Revises: 2026_10_13_0055
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2026_10_13_0056"
down_revision = "2026_10_13_0055"
branch_labels = None
depends_on = None

_GUC = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_PREDICATE = f"tenant_id = {_GUC}"

_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("repositories", postgresql.JSONB()),
    ("repository_id", sa.Uuid()),
    ("coordination_mode", sa.String(length=32)),
    ("scope_level", sa.String(length=32)),
    ("shape", sa.String(length=32)),
    ("priority_level_id", sa.Uuid()),
    ("expected_size", sa.String(length=1)),
    ("requested_by", sa.String(length=255)),
    ("github_token_secret_id", sa.Uuid()),
    ("question_mode", sa.String(length=32)),
    ("lifecycle_class", sa.String(length=32)),
    ("fixture", sa.Boolean()),
    ("it_inputs", postgresql.JSONB()),
    ("attachments", postgresql.JSONB()),
    ("bound_assets", postgresql.JSONB()),
    ("current_brief_version", sa.Integer()),
    ("intake_updated_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("pm_projects", sa.Column(name, type_, nullable=True))
    op.create_foreign_key(
        "pm_projects_priority_level_id_fkey",
        "pm_projects",
        "pm_priority_levels",
        ["priority_level_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_pm_projects_priority_level_id", "pm_projects", ["priority_level_id"])
    op.create_check_constraint(
        "ck_pm_projects_expected_size", "pm_projects", "expected_size IN ('S', 'M', 'L')"
    )
    op.create_check_constraint(
        "ck_pm_projects_current_brief_version", "pm_projects", "current_brief_version >= 1"
    )

    op.create_table(
        "pm_project_brief_versions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("pm_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("brief", postgresql.JSONB(), nullable=False),
        sa.Column("author_kind", sa.String(length=16), nullable=False),
        sa.Column("author_kind_source", sa.String(length=16), nullable=False),
        sa.Column("author_subject_id", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "version", name="uq_pm_project_brief_versions_project_version"
        ),
        sa.CheckConstraint("version >= 1", name="ck_pm_project_brief_versions_version"),
        sa.CheckConstraint(
            "author_kind IN ('user', 'conveyor')",
            name="ck_pm_project_brief_versions_author_kind",
        ),
        sa.CheckConstraint(
            "author_kind_source IN ('delegated', 'conveyor_subject', 'declared')",
            name="ck_pm_project_brief_versions_author_kind_source",
        ),
        sa.CheckConstraint(
            "note IS NULL OR char_length(note) <= 2000",
            name="ck_pm_project_brief_versions_note_len",
        ),
    )
    op.create_index(
        "ix_pm_project_brief_versions_tenant_id", "pm_project_brief_versions", ["tenant_id"]
    )
    op.execute("ALTER TABLE pm_project_brief_versions ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON pm_project_brief_versions")
    op.execute(
        "CREATE POLICY tenant_isolation ON pm_project_brief_versions "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON pm_project_brief_versions")
    op.drop_table("pm_project_brief_versions")
    op.drop_constraint("ck_pm_projects_current_brief_version", "pm_projects", type_="check")
    op.drop_constraint("ck_pm_projects_expected_size", "pm_projects", type_="check")
    op.drop_index("ix_pm_projects_priority_level_id", table_name="pm_projects")
    op.drop_constraint("pm_projects_priority_level_id_fkey", "pm_projects", type_="foreignkey")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("pm_projects", name)
