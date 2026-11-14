"""Tests for PM FK integrity (Task 4.5).

Validates the FK-driven cleanup behaviours added in
2026_05_06_0023_add_pm_integrity_constraints.py:
- Project.programme_id → ON DELETE SET NULL
"""

from __future__ import annotations

from models.pm import Programme, Project
from tests.tenants import T


class TestPMFKConstraints:
    async def test_delete_programme_nulls_project_programme_id(self, test_session):
        # Create programme + project linked to it.
        prog = Programme(
            slug="prog-1",
            title="P1",
            tenant_id=T("t-fk"),
            tenant_name="t-fk",
            subject_id="t-fk",
        )
        test_session.add(prog)
        await test_session.flush()

        proj = Project(
            slug="proj-1",
            title="Proj1",
            tenant_id=T("t-fk"),
            tenant_name="t-fk",
            subject_id="t-fk",
            programme_id=prog.id,
        )
        test_session.add(proj)
        await test_session.commit()

        proj_id = proj.id

        # Drop the programme — project's programme_id should NULL out.
        await test_session.delete(prog)
        await test_session.commit()

        # Bypass session cache — the FK cascade fires at the DB layer,
        # so query the value directly via a column-only SELECT.
        from sqlalchemy import text

        result = await test_session.execute(
            text("SELECT programme_id FROM pm_projects WHERE id = :pid"),
            {"pid": proj_id},
        )
        row = result.first()
        assert row is not None
        assert row[0] is None
