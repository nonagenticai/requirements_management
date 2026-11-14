"""DB-level assertions for the WS1 AA-intake partial unique indexes.

These pin the two halves of the contract directly at the ``INSERT`` boundary,
independent of the router/service layer:

* two rows sharing ``(tenant_id, project_id, title)`` with a **non-null**
  project scope collide → the second commit raises ``IntegrityError``; and
* two rows sharing the same title with a **null** project scope both succeed —
  the partial predicate (``project_id IS NOT NULL`` /
  ``origin_project_id IS NOT NULL``) excludes them, so tenant-wide rows that
  legitimately share a title stay unconstrained.

Distinct ``slug`` values are used on purpose so the per-tenant
``uq_<table>_tenant_slug`` constraint does NOT fire first and mask the identity
index under test.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy.exc
from sqlalchemy import func, select

from models.architecture import ArchitectureElement, ArchitectureView, Standard
from models.pm import Project, Risk
from models.principles import Principle
from models.requirements import Requirement
from models.toc import TocNode, TocTree
from tests.tenants import T


def _requirement(
    *, slug: str, project_id: str | None, visibility: str = "tenant", subject_id: str = ""
) -> Requirement:
    return Requirement(
        slug=slug,
        title="Users can reset their password",
        description="As a user I can reset my password via email.",
        tenant_id=T("user-123"),
        subject_id=subject_id,
        project_id=project_id,
        visibility=visibility,
        links={},
        acceptance=[],
        effort=3.0,
    )


def _element(*, slug: str, project_id: str | None, is_synthetic: bool) -> ArchitectureElement:
    return ArchitectureElement(
        slug=slug,
        title="Payment gateway",
        tenant_id=T("user-123"),
        tenant_name="acme",
        project_id=project_id,
        layer="application",
        element_type="component",
        is_synthetic=is_synthetic,
    )


def _principle(*, slug: str, origin_project_id) -> Principle:
    return Principle(
        slug=slug,
        title="Prefer managed services over self-hosting",
        statement="We use managed services unless a hard requirement forbids it.",
        tenant_id=T("user-123"),
        origin_project_id=origin_project_id,
        origin_kind="project" if origin_project_id is not None else "tenant",
    )


async def test_requirements_nonnull_project_title_collides(test_session_factory):
    async with test_session_factory() as session:
        session.add(_requirement(slug="req-a", project_id="proj-x"))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_requirement(slug="req-b", project_id="proj-x"))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


async def test_requirements_null_project_same_title_both_succeed(test_session_factory):
    async with test_session_factory() as session:
        session.add(_requirement(slug="req-null-a", project_id=None))
        session.add(_requirement(slug="req-null-b", project_id=None))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Requirement)
                .where(
                    Requirement.tenant_id == T("user-123"),
                    Requirement.project_id.is_(None),
                    Requirement.title == "Users can reset their password",
                )
            )
        ).scalar()
    assert count == 2


async def test_principles_nonnull_origin_project_title_collides(test_session_factory):
    origin = uuid4()
    async with test_session_factory() as session:
        session.add(_principle(slug="prin-a", origin_project_id=origin))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_principle(slug="prin-b", origin_project_id=origin))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


async def test_requirements_user_visible_same_key_different_subjects_coexist(
    test_session_factory,
):
    """The TENANT identity index is partial on ``visibility = 'tenant'`` and the
    USER identity index (rm#191, ``uq_requirements_user_project_title``) is keyed
    on ``subject_id``, so two user-visible rows sharing ``(tenant, project_id,
    title)`` under DIFFERENT subjects legitimately coexist — each subject keeps
    its own private copy.

    (Before rm#191 this test inserted both rows under the SAME subject and
    asserted they coexist: that is exactly the duplicate shape rm#191 removes, so
    it now lives in the collide test below.)
    """
    async with test_session_factory() as session:
        session.add(
            _requirement(
                slug="req-uv-a", project_id="proj-x", visibility="user", subject_id="alice"
            )
        )
        session.add(
            _requirement(slug="req-uv-b", project_id="proj-x", visibility="user", subject_id="bob")
        )
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Requirement)
                .where(
                    Requirement.tenant_id == T("user-123"),
                    Requirement.project_id == "proj-x",
                    Requirement.visibility == "user",
                )
            )
        ).scalar()
    assert count == 2


async def test_requirements_user_visible_same_subject_same_key_collides(test_session_factory):
    """rm#191: two user-visible rows of the SAME subject sharing ``(tenant,
    project_id, title)`` collide on ``uq_requirements_user_project_title`` even
    with distinct slugs — the slug constraint alone let a race winner that
    committed first push the loser to ``<slug>-1`` and land a duplicate."""
    async with test_session_factory() as session:
        session.add(
            _requirement(
                slug="req-uv-same-a", project_id="proj-y", visibility="user", subject_id="alice"
            )
        )
        await session.commit()
    async with test_session_factory() as session:
        session.add(
            _requirement(
                slug="req-uv-same-b", project_id="proj-y", visibility="user", subject_id="alice"
            )
        )
        with pytest.raises(
            sqlalchemy.exc.IntegrityError, match="uq_requirements_user_project_title"
        ):
            await session.commit()


async def test_arch_elements_same_project_title_type_coexist(test_session_factory):
    """A12: elements are identified by id. ``uq_arch_elements_tenant_project_title_type``
    is dropped (migration 0068), so two non-synthetic elements — and a synthetic —
    may share ``(tenant, project_id, title, element_type)``; the ArchiMate checker
    reports that as an ``AM-DUP-001`` warning, never a constraint violation.
    """
    async with test_session_factory() as session:
        session.add(_element(slug="el-real", project_id="proj-x", is_synthetic=False))
        session.add(_element(slug="el-synth", project_id="proj-x", is_synthetic=True))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_element(slug="el-real-2", project_id="proj-x", is_synthetic=False))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(ArchitectureElement)
                .where(
                    ArchitectureElement.tenant_id == T("user-123"),
                    ArchitectureElement.project_id == "proj-x",
                    ArchitectureElement.title == "Payment gateway",
                )
            )
        ).scalar()
    assert count == 3


async def test_principles_null_origin_project_same_title_both_succeed(test_session_factory):
    async with test_session_factory() as session:
        session.add(_principle(slug="prin-null-a", origin_project_id=None))
        session.add(_principle(slug="prin-null-b", origin_project_id=None))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Principle)
                .where(
                    Principle.tenant_id == T("user-123"),
                    Principle.origin_project_id.is_(None),
                    Principle.title == "Prefer managed services over self-hosting",
                )
            )
        ).scalar()
    assert count == 2


# ---------------------------------------------------------------------------
# Group A (WS1): risks, standards, views, toc trees, toc nodes, projects
# ---------------------------------------------------------------------------


def _risk(*, slug: str, project_id: str, visibility: str = "tenant") -> Risk:
    return Risk(
        slug=slug,
        title="Vendor API rate limits may throttle onboarding",
        tenant_id=T("user-123"),
        project_id=project_id,
        visibility=visibility,
        probability=0.4,
        impact_score=3.0,
        risk_score=1.2,
        category="technical",
        response_strategy="mitigate",
    )


def _standard(*, slug: str, project_id: str | None, visibility: str = "tenant") -> Standard:
    return Standard(
        slug=slug,
        title="Use OpenTelemetry for all tracing",
        tenant_id=T("user-123"),
        project_id=project_id,
        visibility=visibility,
        standard_type="technology_standard",
    )


def _view(*, slug: str, project_id: str | None, visibility: str = "tenant") -> ArchitectureView:
    return ArchitectureView(
        slug=slug,
        title="Application cooperation view",
        tenant_id=T("user-123"),
        project_id=project_id,
        visibility=visibility,
        viewpoint="application_cooperation",
    )


def _toc_tree(
    *, slug: str, project_id: str | None, tree_type: str = "crt", visibility: str = "tenant"
) -> TocTree:
    return TocTree(
        slug=slug,
        title="Delivery bottleneck analysis",
        tenant_id=T("user-123"),
        project_id=project_id,
        visibility=visibility,
        tree_type=tree_type,
    )


def _toc_node(*, slug: str, tree_id, visibility: str = "tenant") -> TocNode:
    return TocNode(
        slug=slug,
        tree_id=tree_id,
        title="Throughput is capped by the review queue",
        statement="Undesirable effect: PRs wait days for review.",
        node_kind="ude",
        tenant_id=T("user-123"),
        visibility=visibility,
    )


def _project(*, slug: str, origin_project_id, visibility: str = "tenant") -> Project:
    return Project(
        slug=slug,
        title="Customer onboarding platform",
        tenant_id=T("user-123"),
        visibility=visibility,
        origin_project_id=origin_project_id,
        origin_kind="project" if origin_project_id is not None else "tenant",
    )


async def test_risks_nonnull_project_title_collides(test_session_factory):
    async with test_session_factory() as session:
        session.add(_risk(slug="risk-a", project_id="proj-x"))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_risk(slug="risk-b", project_id="proj-x"))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


async def test_risks_distinct_project_same_title_both_succeed(test_session_factory):
    async with test_session_factory() as session:
        session.add(_risk(slug="risk-p1", project_id="proj-1"))
        session.add(_risk(slug="risk-p2", project_id="proj-2"))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Risk)
                .where(
                    Risk.tenant_id == T("user-123"),
                    Risk.title == "Vendor API rate limits may throttle onboarding",
                )
            )
        ).scalar()
    assert count == 2


async def test_standards_nonnull_project_same_title_coexist(test_session_factory):
    """A12: ``uq_arch_standards_tenant_project_title`` is dropped; id is the identity."""
    async with test_session_factory() as session:
        session.add(_standard(slug="std-a", project_id="proj-x"))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_standard(slug="std-b", project_id="proj-x"))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Standard)
                .where(Standard.tenant_id == T("user-123"), Standard.project_id == "proj-x")
            )
        ).scalar()
    assert count == 2


async def test_standards_null_project_same_title_both_succeed(test_session_factory):
    async with test_session_factory() as session:
        session.add(_standard(slug="std-null-a", project_id=None))
        session.add(_standard(slug="std-null-b", project_id=None))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Standard)
                .where(
                    Standard.tenant_id == T("user-123"),
                    Standard.project_id.is_(None),
                    Standard.title == "Use OpenTelemetry for all tracing",
                )
            )
        ).scalar()
    assert count == 2


async def test_views_nonnull_project_same_title_coexist(test_session_factory):
    """A12: ``uq_arch_views_tenant_project_title`` is dropped; id is the identity."""
    async with test_session_factory() as session:
        session.add(_view(slug="view-a", project_id="proj-x"))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_view(slug="view-b", project_id="proj-x"))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(ArchitectureView)
                .where(
                    ArchitectureView.tenant_id == T("user-123"),
                    ArchitectureView.project_id == "proj-x",
                )
            )
        ).scalar()
    assert count == 2


async def test_views_null_project_same_title_both_succeed(test_session_factory):
    async with test_session_factory() as session:
        session.add(_view(slug="view-null-a", project_id=None))
        session.add(_view(slug="view-null-b", project_id=None))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(ArchitectureView)
                .where(
                    ArchitectureView.tenant_id == T("user-123"),
                    ArchitectureView.project_id.is_(None),
                    ArchitectureView.title == "Application cooperation view",
                )
            )
        ).scalar()
    assert count == 2


async def test_toc_trees_nonnull_project_type_title_collides(test_session_factory):
    async with test_session_factory() as session:
        session.add(_toc_tree(slug="tree-a", project_id="proj-x", tree_type="crt"))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_toc_tree(slug="tree-b", project_id="proj-x", tree_type="crt"))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


async def test_toc_trees_distinct_type_same_title_both_succeed(test_session_factory):
    """tree_type is part of the identity — a project may hold one tree of each
    of the six types with the same title."""
    async with test_session_factory() as session:
        session.add(_toc_tree(slug="tree-crt", project_id="proj-x", tree_type="crt"))
        session.add(_toc_tree(slug="tree-frt", project_id="proj-x", tree_type="frt"))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(TocTree)
                .where(
                    TocTree.tenant_id == T("user-123"),
                    TocTree.project_id == "proj-x",
                    TocTree.title == "Delivery bottleneck analysis",
                )
            )
        ).scalar()
    assert count == 2


async def test_toc_trees_null_project_same_title_both_succeed(test_session_factory):
    async with test_session_factory() as session:
        session.add(_toc_tree(slug="tree-null-a", project_id=None))
        session.add(_toc_tree(slug="tree-null-b", project_id=None))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(TocTree)
                .where(
                    TocTree.tenant_id == T("user-123"),
                    TocTree.project_id.is_(None),
                    TocTree.title == "Delivery bottleneck analysis",
                )
            )
        ).scalar()
    assert count == 2


async def _make_tree(session, *, slug: str, tree_type: str = "crt"):
    """Insert a committed parent tree (toc_nodes.tree_id is a FK) and return its id."""
    tree = _toc_tree(slug=slug, project_id=None, tree_type=tree_type)
    session.add(tree)
    await session.commit()
    return tree.id


async def test_toc_nodes_same_tree_title_collides(test_session_factory):
    async with test_session_factory() as session:
        tree_id = await _make_tree(session, slug="tree-for-nodes-a")
        session.add(_toc_node(slug="node-a", tree_id=tree_id))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_toc_node(slug="node-b", tree_id=tree_id))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


async def test_toc_nodes_distinct_tree_same_title_both_succeed(test_session_factory):
    async with test_session_factory() as session:
        tree_1 = await _make_tree(session, slug="tree-for-nodes-1")
        tree_2 = await _make_tree(session, slug="tree-for-nodes-2")
        session.add(_toc_node(slug="node-t1", tree_id=tree_1))
        session.add(_toc_node(slug="node-t2", tree_id=tree_2))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(TocNode)
                .where(
                    TocNode.tenant_id == T("user-123"),
                    TocNode.title == "Throughput is capped by the review queue",
                )
            )
        ).scalar()
    assert count == 2


async def test_projects_nonnull_origin_project_title_collides(test_session_factory):
    origin = uuid4()
    async with test_session_factory() as session:
        session.add(_project(slug="proj-a", origin_project_id=origin))
        await session.commit()

    async with test_session_factory() as session:
        session.add(_project(slug="proj-b", origin_project_id=origin))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


async def test_projects_null_origin_project_same_title_both_succeed(test_session_factory):
    async with test_session_factory() as session:
        session.add(_project(slug="proj-null-a", origin_project_id=None))
        session.add(_project(slug="proj-null-b", origin_project_id=None))
        await session.commit()

    async with test_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Project)
                .where(
                    Project.tenant_id == T("user-123"),
                    Project.origin_project_id.is_(None),
                    Project.title == "Customer onboarding platform",
                )
            )
        ).scalar()
    assert count == 2
