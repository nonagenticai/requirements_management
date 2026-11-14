"""The SIBLING router's whole-graph loaders are bounded, and refuse rather than truncate.

``routers/architecture_tools`` was not the only place carrying an unbounded
``select(Entity)`` over a whole project. ``routers/integration_tools`` kept its
own copy (as did the since-removed ``routers/llm_tools``), reached by
``/integration/tools/gap-to-workpackages`` and
``/integration/tools/requirement-trace``.
Live worst case is 2,703 architecture elements + 11,674 relationships for one
tenant against a ``memory: 512Mi``, ``replicas: 1`` pod that an unbounded load
has already OOM-killed once (#147).

Three things are pinned here, and the first is the one that is easy to leave
out:

1. **The control.** A normal tenant still gets 200 with the SAME body. Without
   it, "bounded" and "broken" are indistinguishable — a loader that returns
   nothing, or drops a key, also never trips a cap. The expected body is
   computed by calling the pure tool function on the dicts the loader is
   supposed to have produced, so this asserts the DB path and the pure path
   agree.
2. **The refusal.** Over the cap is HTTP 422 naming the total and the limit —
   never a silent truncation. A truncated input to a trace / gap analysis does
   not yield a partial answer, it yields a confidently wrong one: every dropped
   work package becomes a gap that does not exist, and every dropped edge
   becomes an untraced requirement.
3. **The projected key sets**, pinned literally. The loaders were converted
   from ``select(Entity)`` to column selects; a column dropped from a select
   list is a key silently missing from the dict, and the consumers use
   ``dict.get``-shaped access in enough places that it would not necessarily
   raise.

The caps are monkeypatched small (5) rather than seeded past. Seeding thousands
of rows would test the cap by making the suite pay the exact cost the cap
exists to avoid, on every CI run; the constant is the thing under test, not the
row count.
"""

from __future__ import annotations

from uuid import uuid7

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureElement, ArchitectureRelationship
from models.requirements import Requirement
from routers import integration_tools
from tests.tenants import T

# The tenant `authenticated_client` presents (conftest: X-Tenant-Id = T("user-123")).
_TENANT = T("user-123")
_TENANT_NAME = "Bounded Input Test Tenant"
_SUBJECT = "user-123"
_PROJECT = "bounded-input-project"


def _element(
    slug: str,
    *,
    element_type: str = "capability",
    layer: str = "strategy",
    status: str = "draft",
    architecture_state: str | None = "target",
    project_id: str = _PROJECT,
) -> ArchitectureElement:
    return ArchitectureElement(
        id=uuid7(),
        slug=slug,
        title=slug.replace("-", " ").title(),
        description=f"description of {slug}",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=project_id,
        layer=layer,
        element_type=element_type,
        properties={},
        status=status,
        architecture_state=architecture_state,
    )


# ---------------------------------------------------------------------------
# 1. Control — a normal tenant is unaffected
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def control_seed(test_session: AsyncSession) -> dict:
    """A small, ordinary project: 2 elements, 1 relationship, 1 requirement.

    ``id`` is assigned explicitly with ``uuid7`` in creation order so the
    loaders' ``ORDER BY id`` is the order asserted below — the bound is applied
    with a deterministic ordering, and this fixture is what makes that visible.
    """
    target = _element("new-capability", status="target", architecture_state="target")
    wp = _element(
        "wp-implement-capability",
        element_type="work_package",
        layer="implementation_migration",
        status="draft",
        architecture_state="target",
    )
    requirement = Requirement(
        id=uuid7(),
        slug="req-capability",
        title="The system shall provide the new capability",
        description="seed",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=_PROJECT,
        effort=1.0,
    )
    for row in (target, wp, requirement):
        test_session.add(row)
    await test_session.commit()

    relationship = ArchitectureRelationship(
        id=uuid7(),
        slug="rel-wp-realizes-capability",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=_PROJECT,
        source_entity_type="architecture_elements",
        source_id=wp.id,
        target_entity_type="architecture_elements",
        target_id=target.id,
        relationship_type="realization",
        properties={},
    )
    test_session.add(relationship)
    await test_session.commit()

    ordered_elements = sorted((target, wp), key=lambda e: e.id)
    return {
        # What `integration_tools._load_elements` must return, key for key.
        "integration_elements": [
            {
                "id": str(e.id),
                "slug": e.slug,
                "title": e.title,
                "description": e.description,
                "layer": e.layer,
                "element_type": e.element_type,
                "properties": {},
                "status": e.status,
                "architecture_state": e.architecture_state,
                "parent_id": None,
            }
            for e in ordered_elements
        ],
        "relationships": [
            {
                "id": str(relationship.id),
                "source_entity_type": "architecture_elements",
                "source_id": str(wp.id),
                "target_entity_type": "architecture_elements",
                "target_id": str(target.id),
                "relationship_type": "realization",
                "properties": {},
            }
        ],
        "requirements": [
            {
                "id": str(requirement.id),
                "slug": requirement.slug,
                "title": requirement.title,
            }
        ],
    }


async def test_loaders_return_exactly_the_rows_they_did_before(
    test_session: AsyncSession, control_seed: dict
) -> None:
    """Every loader's output, dict for dict — the column-select control."""
    int_elements = await integration_tools._load_elements(test_session, _TENANT, _PROJECT)
    int_rels = await integration_tools._load_relationships(test_session, _TENANT, _PROJECT)
    int_reqs = await integration_tools._load_requirements(test_session, _TENANT, _PROJECT)

    assert int_elements == control_seed["integration_elements"]
    assert int_rels == control_seed["relationships"]
    assert int_reqs == control_seed["requirements"]


async def test_gap_to_workpackages_body_is_unchanged(
    authenticated_client: AsyncClient, control_seed: dict
) -> None:
    """200, and the body equals the pure tool run on the same dicts."""
    from tools.integration.gap_to_wp import generate_workpackage_proposals

    expected = generate_workpackage_proposals(
        control_seed["integration_elements"], control_seed["relationships"]
    )

    resp = await authenticated_client.post(
        "/integration/tools/gap-to-workpackages", json={"project_id": _PROJECT}
    )
    assert resp.status_code == 200
    assert resp.json() == expected


async def test_requirement_trace_body_is_unchanged(
    authenticated_client: AsyncClient, control_seed: dict
) -> None:
    from tools.integration.req_arch_trace import trace_requirements

    expected = trace_requirements(
        control_seed["requirements"],
        control_seed["integration_elements"],
        control_seed["relationships"],
    )

    resp = await authenticated_client.post(
        "/integration/tools/requirement-trace", json={"project_id": _PROJECT}
    )
    assert resp.status_code == 200
    assert resp.json() == expected


# ---------------------------------------------------------------------------
# 2. Over the cap — 422 naming the total and the limit
# ---------------------------------------------------------------------------

_SMALL_CAP = 5


@pytest_asyncio.fixture
async def over_cap_seed(test_session: AsyncSession) -> None:
    """Six elements against a cap of five.

    Six, not six thousand: the cap constant is monkeypatched down in each test
    rather than the row count being seeded up. Seeding past the real
    ``DEFAULT_MAX_INPUT_ELEMENTS`` would make this suite pay, on every CI run,
    exactly the load cost the cap exists to prevent — and it would still be
    testing the same one comparison.
    """
    for i in range(_SMALL_CAP + 1):
        test_session.add(_element(f"over-cap-element-{i:02d}"))
    await test_session.commit()


def _assert_refused(resp, *, total: int, limit: int) -> None:
    """422, with the two numbers the caller must act on present in the envelope.

    Asserted against the RENDERED body, not against ``exc.detail``:
    ``main._http_exception_to_response`` flattens a dict detail to ``code`` +
    ``message`` and drops every sibling key, so a ``total``/``limit`` that lives
    only in the detail dict never reaches the caller. That flattening is why the
    routers put both numbers in the message text too.
    """
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "input_too_large"
    assert str(total) in error["message"]
    assert str(limit) in error["message"]


async def test_gap_to_workpackages_refuses_over_cap(
    authenticated_client: AsyncClient, over_cap_seed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integration_tools, "DEFAULT_MAX_INPUT_ELEMENTS", _SMALL_CAP)
    resp = await authenticated_client.post(
        "/integration/tools/gap-to-workpackages", json={"project_id": _PROJECT}
    )
    _assert_refused(resp, total=_SMALL_CAP + 1, limit=_SMALL_CAP)


async def test_requirement_trace_refuses_over_cap(
    authenticated_client: AsyncClient, over_cap_seed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integration_tools, "DEFAULT_MAX_INPUT_ELEMENTS", _SMALL_CAP)
    resp = await authenticated_client.post(
        "/integration/tools/requirement-trace", json={"project_id": _PROJECT}
    )
    _assert_refused(resp, total=_SMALL_CAP + 1, limit=_SMALL_CAP)


@pytest_asyncio.fixture
async def over_cap_relationships(test_session: AsyncSession) -> None:
    """Six relationships against a relationship cap of five."""
    # One source, a DISTINCT target per edge. `uq_arch_rels_tenant_edge` is a
    # unique constraint over (tenant, project, source_type, source, target_type,
    # target, relationship_type), so N edges between the SAME pair is not a
    # seedable state — the database rejects it before the cap is ever consulted.
    a = _element("rel-endpoint-a")
    targets = [_element(f"rel-endpoint-t{i:02d}") for i in range(_SMALL_CAP + 1)]
    test_session.add_all([a, *targets])
    await test_session.commit()
    for i, target in enumerate(targets):
        test_session.add(
            ArchitectureRelationship(
                id=uuid7(),
                slug=f"over-cap-rel-{i:02d}",
                tenant_id=_TENANT,
                tenant_name=_TENANT_NAME,
                subject_id=_SUBJECT,
                project_id=_PROJECT,
                source_entity_type="architecture_elements",
                source_id=a.id,
                target_entity_type="architecture_elements",
                target_id=target.id,
                relationship_type="association",
                properties={},
            )
        )
    await test_session.commit()


async def test_relationship_cap_is_enforced_separately(
    authenticated_client: AsyncClient,
    over_cap_relationships: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edge population has its own cap — elements under, relationships over.

    Two constants, not one: the element cap is left at its real value here, so a
    green result cannot come from the element loader tripping first.
    """
    monkeypatch.setattr(integration_tools, "DEFAULT_MAX_INPUT_RELATIONSHIPS", _SMALL_CAP)
    resp = await authenticated_client.post(
        "/integration/tools/gap-to-workpackages", json={"project_id": _PROJECT}
    )
    _assert_refused(resp, total=_SMALL_CAP + 1, limit=_SMALL_CAP)


# ---------------------------------------------------------------------------
# 3. The projected key sets, pinned literally
# ---------------------------------------------------------------------------

_INTEGRATION_ELEMENT_KEYS = {
    "id",
    "slug",
    "title",
    "description",
    "layer",
    "element_type",
    "properties",
    "status",
    "architecture_state",
    "parent_id",
}

_RELATIONSHIP_KEYS = {
    "id",
    "source_entity_type",
    "source_id",
    "target_entity_type",
    "target_id",
    "relationship_type",
    "properties",
}

_REQUIREMENT_KEYS = {"id", "slug", "title"}


async def test_projected_key_sets_are_pinned(
    test_session: AsyncSession, control_seed: dict
) -> None:
    """A column dropped from a select list fails HERE, loudly, not downstream.

    Equality on the whole set, not ``issubset``: an EXTRA key is a defect too —
    it means a projection drifted apart from its sibling and the two routers no
    longer feed their tools the same shape.
    """
    elements = await integration_tools._load_elements(test_session, _TENANT, _PROJECT)
    assert elements, "control_seed must produce rows, or this test cannot fail"
    for row in elements:
        assert set(row) == _INTEGRATION_ELEMENT_KEYS

    rows = await integration_tools._load_relationships(test_session, _TENANT, _PROJECT)
    assert rows
    for row in rows:
        assert set(row) == _RELATIONSHIP_KEYS

    requirements = await integration_tools._load_requirements(test_session, _TENANT, _PROJECT)
    assert requirements
    for row in requirements:
        assert set(row) == _REQUIREMENT_KEYS
