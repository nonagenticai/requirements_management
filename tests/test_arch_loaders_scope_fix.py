"""Unit regression-lock for the P4.5 loader scope-model fix.

Locks the contract that ``_load_elements`` / ``_load_relationships`` /
``_load_views`` / ``_load_standards`` / ``_count_entities`` all surface
tenant-scope rows (``project_id IS NULL``) in a project's effective view —
the bug the proposals queue (P4.4) was hitting where tenant-scope
ApplicationComponents were invisible to the queue.

Pure-DB tests: seed the models directly, call the loaders, assert.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    Standard,
)
from models.decisions import Decision
from models.principles import Principle
from models.requirements import Requirement
from routers.architecture_tools import (
    _count_entities,
    _count_tenant_entities,
    _load_elements,
    _load_relationships,
    _load_standards,
    _load_views,
)
from tests.tenants import T

_TENANT = T("loader-scope-test-tenant")
_TENANT_NAME = "Loader Scope Test Tenant"
_PROJECT = "loader-scope-test-project"
_OTHER_PROJECT = "loader-scope-test-other-project"
_SUBJECT = "loader-scope-test-subject"


def _element(slug: str, *, project_id: str | None) -> ArchitectureElement:
    return ArchitectureElement(
        slug=slug,
        title=slug,
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=project_id,
        layer="application",
        element_type="application_component",
        properties={},
    )


def _view(slug: str, *, project_id: str | None) -> ArchitectureView:
    return ArchitectureView(
        slug=slug,
        title=slug,
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=project_id,
        viewpoint="application_cooperation",
    )


def _standard(slug: str, *, project_id: str | None) -> Standard:
    return Standard(
        slug=slug,
        title=slug,
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=project_id,
        standard_type="policy",
    )


def _requirement(slug: str, *, project_id: str | None) -> Requirement:
    return Requirement(
        slug=slug,
        title=slug,
        description="seed",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=project_id,
        effort=1.0,
    )


def _decision(slug: str, *, project_id: str | None) -> Decision:
    return Decision(
        slug=slug,
        title=slug,
        context="seed",
        decision="seed",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=project_id,
    )


def _principle(slug: str, *, tenant_id: str = _TENANT) -> Principle:
    """Tenant-scope only: ``Principle`` has no ``project_id`` column."""
    return Principle(
        slug=slug,
        title=slug,
        statement="seed",
        tenant_id=tenant_id,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Seed fixture: one tenant-scope, one project-scope, one other-project of each
# ─────────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def _seed(test_session: AsyncSession) -> None:
    elems = [
        _element("ac-tenant", project_id=None),
        _element("ac-project", project_id=_PROJECT),
        _element("ac-other-project", project_id=_OTHER_PROJECT),
    ]
    other = [
        _view("view-tenant", project_id=None),
        _view("view-project", project_id=_PROJECT),
        _standard("std-tenant", project_id=None),
        _standard("std-project", project_id=_PROJECT),
        _requirement("req-tenant", project_id=None),
        _requirement("req-project", project_id=_PROJECT),
        _decision("dec-tenant", project_id=None),
        _decision("dec-project", project_id=_PROJECT),
        # Principle is tenant-scope only — seed two under the test tenant +
        # one under a different tenant for the isolation assertion below.
        _principle("prin-tenant-a"),
        _principle("prin-tenant-b"),
        _principle("prin-other-tenant", tenant_id=T("different-tenant-entirely")),
    ]
    for row in [*elems, *other]:
        test_session.add(row)
    await test_session.commit()

    tenant_ac, project_ac, other_ac = elems
    rels = [
        ArchitectureRelationship(
            slug="rel-tenant",
            tenant_id=_TENANT,
            tenant_name=_TENANT_NAME,
            subject_id=_SUBJECT,
            project_id=None,
            source_entity_type="architecture_elements",
            source_id=tenant_ac.id,
            target_entity_type="architecture_elements",
            target_id=project_ac.id,
            relationship_type="composition",
            properties={},
        ),
        ArchitectureRelationship(
            slug="rel-project",
            tenant_id=_TENANT,
            tenant_name=_TENANT_NAME,
            subject_id=_SUBJECT,
            project_id=_PROJECT,
            source_entity_type="architecture_elements",
            source_id=project_ac.id,
            target_entity_type="architecture_elements",
            target_id=tenant_ac.id,
            relationship_type="realization",
            properties={},
        ),
        ArchitectureRelationship(
            slug="rel-other-project",
            tenant_id=_TENANT,
            tenant_name=_TENANT_NAME,
            subject_id=_SUBJECT,
            project_id=_OTHER_PROJECT,
            source_entity_type="architecture_elements",
            source_id=other_ac.id,
            target_entity_type="architecture_elements",
            target_id=tenant_ac.id,
            relationship_type="composition",
            properties={},
        ),
    ]
    for r in rels:
        test_session.add(r)
    await test_session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Loader tests — each asserts: tenant + project rows present, other-project absent
# ─────────────────────────────────────────────────────────────────────────────


async def test_load_elements_includes_tenant_scope(test_session: AsyncSession, _seed: None) -> None:
    rows = await _load_elements(test_session, _TENANT, _PROJECT)
    slugs = {r["slug"] for r in rows}
    assert "ac-tenant" in slugs  # the bug fix this lane locks
    assert "ac-project" in slugs
    assert "ac-other-project" not in slugs


async def test_load_relationships_includes_tenant_scope(
    test_session: AsyncSession, _seed: None
) -> None:
    rels = await _load_relationships(test_session, _TENANT, _PROJECT)
    types = sorted(r["relationship_type"] for r in rels)
    # One tenant-scope (composition) + one project-scope (realization) — NOT the other-project one.
    assert types == ["composition", "realization"]


async def test_load_views_includes_tenant_scope(test_session: AsyncSession, _seed: None) -> None:
    views = await _load_views(test_session, _TENANT, _PROJECT)
    titles = {v["title"] for v in views}
    assert {"view-tenant", "view-project"} <= titles


async def test_load_standards_includes_tenant_scope(
    test_session: AsyncSession, _seed: None
) -> None:
    standards = await _load_standards(test_session, _TENANT, _PROJECT)
    titles = {s["title"] for s in standards}
    assert {"std-tenant", "std-project"} <= titles


async def test_count_entities_includes_tenant_scope(
    test_session: AsyncSession, _seed: None
) -> None:
    # 1 tenant + 1 project = 2 each, for the models that carry ``project_id``.
    assert await _count_entities(test_session, Requirement, _TENANT, _PROJECT) == 2
    assert await _count_entities(test_session, Decision, _TENANT, _PROJECT) == 2


async def test_count_tenant_entities_principle(test_session: AsyncSession, _seed: None) -> None:
    """``Principle`` is tenant-scope only — uses the tenant-only counter.

    Locks the fix for the latent in ``adm_completeness_endpoint`` where
    ``_count_entities(Principle, …)`` raised ``AttributeError`` because
    ``Principle`` has no ``project_id`` column. Counts MUST cross all
    projects under the tenant and MUST NOT leak across tenants.
    """
    assert await _count_tenant_entities(test_session, Principle, _TENANT) == 2
    assert (
        await _count_tenant_entities(test_session, Principle, T("different-tenant-entirely")) == 1
    )
    assert await _count_tenant_entities(test_session, Principle, T("no-such-tenant")) == 0


async def test_tenant_isolation_unaffected(test_session: AsyncSession, _seed: None) -> None:
    """Tenant-scope inclusion MUST NOT leak across tenants."""
    rows = await _load_elements(test_session, T("different-tenant-entirely"), _PROJECT)
    assert rows == []
