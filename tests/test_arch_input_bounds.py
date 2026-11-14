"""The architecture-tools loaders are bounded, and they REFUSE rather than truncate.

Follow-up to #147, which bounded the *response* of these tools and explicitly
left the *input* unbounded. ``_load_elements`` / ``_load_relationships`` /
``_load_views`` / ``_load_standards`` selected every row for the whole tenant
with no ``LIMIT`` — 2,703 elements and 11,674 relationships for the largest live
tenant, into a pod with ``memory: 512Mi`` and ``replicas: 1`` that an unbounded
load on this service has already OOM-killed once.

Four things are pinned here, and they are deliberately four separate tests
because they fail for four different reasons:

1. ``test_under_the_cap_the_response_is_unchanged`` — the CONTROL. Without it,
   "bounded" is indistinguishable from "broken": a loader that returned nothing
   at all would pass every other test in this file. This one asserts the tools
   still produce their real, populated report for an ordinary tenant.
2. ``test_over_the_cap_refuses_with_422_naming_total_and_limit`` — over the
   bound the caller gets a refusal that names both numbers, not a 200 carrying
   a silently partial analysis.
3. ``test_element_dict_shape_is_exactly_the_pre_change_key_set`` — the column
   selects that replaced the full-entity loads must not have dropped a key. The
   key sets are written out literally so a dropped column fails HERE, loudly,
   instead of downstream in a conformance report that quietly lost a field.
4. ``test_order_by_id_is_deterministic`` — ``LIMIT`` without ``ORDER BY`` is a
   non-deterministic subset in Postgres. The ordering is asserted against
   explicitly-assigned ids that are in the REVERSE of insertion order, so a
   loader that merely happened to return insertion order would fail.

Real Postgres, ``asyncio_mode=auto``; seeds the models directly and drives both
the loaders and the HTTP endpoints, in the shape of ``test_arch_loaders_scope_fix.py``.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    Standard,
)
from repositories.bounded import InputTooLarge
from routers import architecture_tools
from routers.architecture_tools import (
    _load_elements,
    _load_relationships,
    _load_standards,
    _load_views,
)
from tests.tenants import T

# ``authenticated_client`` authenticates as ``T("user-123")`` over the
# trusted-network X-Tenant-Id path, so the seed has to live in that tenant for
# the HTTP half of this file to see it at all.
_TENANT = T("user-123")
_TENANT_NAME = "Input Bounds Test Tenant"
_SUBJECT = "input-bounds-test-subject"
_PROJECT = "input-bounds-test-project"


# ---------------------------------------------------------------------------
# The pre-change key sets — the actual contract under test
# ---------------------------------------------------------------------------
#
# Copied from the dict literals the loaders returned BEFORE the full-entity
# selects became column selects. They are spelled out rather than derived from
# the loader, because a set derived from the code under test agrees with any
# mistake that code makes.
_ELEMENT_KEYS = {
    "id",
    "slug",
    "title",
    "description",
    "layer",
    "element_type",
    "is_synthetic",
    "properties",
    "code_bindings",
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
_VIEW_KEYS = {"id", "title", "viewpoint"}
_STANDARD_KEYS = {"id", "title", "standard_type", "status", "compliance_level"}


def _element(
    slug: str,
    *,
    title: str | None = None,
    state: str | None = None,
    element_id: uuid.UUID | None = None,
) -> ArchitectureElement:
    kwargs: dict = {
        "slug": slug,
        "title": title or slug,
        "description": f"description of {slug}",
        "tenant_id": _TENANT,
        "tenant_name": _TENANT_NAME,
        "subject_id": _SUBJECT,
        "project_id": _PROJECT,
        "layer": "application",
        "element_type": "application_component",
        "properties": {"seeded_by": "test_arch_input_bounds"},
        "architecture_state": state,
    }
    if element_id is not None:
        kwargs["id"] = element_id
    return ArchitectureElement(**kwargs)


def _relationship(
    slug: str, source_id: uuid.UUID, target_id: uuid.UUID
) -> ArchitectureRelationship:
    return ArchitectureRelationship(
        slug=slug,
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=_PROJECT,
        source_entity_type="architecture_element",
        source_id=source_id,
        target_entity_type="architecture_element",
        target_id=target_id,
        relationship_type="realization",
        properties={"seeded_by": "test_arch_input_bounds"},
    )


@pytest_asyncio.fixture
async def small_model(test_session: AsyncSession) -> dict:
    """An ordinary, well-under-cap tenant: 2 elements, 1 relationship, 1 view, 1 standard.

    The two elements are deliberately unmatched by ``gap_analysis._fuzzy_match``
    (different slug AND different title), so one lands in ``eliminated`` and the
    other in ``new`` — a populated report the control test can assert real
    content against. (Matched pairs would need the same title — legal since A12,
    when ``uq_arch_elements_tenant_project_title_type`` was dropped — but this
    fixture wants one row in each bucket.)
    """
    baseline = _element("legacy-billing", title="Legacy Billing", state="baseline")
    target = _element("cloud-billing", title="Cloud Billing", state="target")
    test_session.add_all([baseline, target])
    await test_session.commit()
    await test_session.refresh(baseline)
    await test_session.refresh(target)

    rel = _relationship("legacy-to-cloud", baseline.id, target.id)
    view = ArchitectureView(
        slug="input-bounds-view",
        title="Input Bounds View",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=_PROJECT,
        viewpoint="application_cooperation",
    )
    standard = Standard(
        slug="input-bounds-standard",
        title="Input Bounds Standard",
        tenant_id=_TENANT,
        tenant_name=_TENANT_NAME,
        subject_id=_SUBJECT,
        project_id=_PROJECT,
        standard_type="policy",
    )
    test_session.add_all([rel, view, standard])
    await test_session.commit()

    return {"baseline_id": str(baseline.id), "target_id": str(target.id)}


# ---------------------------------------------------------------------------
# 1. CONTROL — under the cap nothing changed
# ---------------------------------------------------------------------------


async def test_under_the_cap_the_response_is_unchanged(
    authenticated_client: AsyncClient, small_model: dict
) -> None:
    """A normal tenant still gets its full, populated report.

    This is the control that stops "bounded" from being indistinguishable from
    "broken". Every other test in this file would still pass against a loader
    that returned an empty list; this one would not.
    """
    resp = await authenticated_client.post(
        "/architecture/tools/gap-analysis", json={"project_id": _PROJECT}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Real content, not an empty-but-well-formed envelope: the seeded baseline
    # element is reported as eliminated and the seeded target as new.
    assert [e["element"]["id"] for e in body["eliminated"]] == [small_model["baseline_id"]]
    assert [e["element"]["id"] for e in body["new"]] == [small_model["target_id"]]
    assert body["eliminated"][0]["element"]["slug"] == "legacy-billing"
    assert body["new"][0]["element"]["slug"] == "cloud-billing"

    # ADM completeness reaches all FOUR loaders (elements, relationships, views,
    # standards), so it is the cheapest single proof that none of them came back
    # empty after the column-select conversion.
    adm = await authenticated_client.post(
        "/architecture/tools/adm-completeness",
        json={"project_id": _PROJECT, "phase": "A"},
    )
    assert adm.status_code == 200, adm.text
    assert adm.json()["phase"] == "A"


# ---------------------------------------------------------------------------
# 2. OVER THE CAP — refuse, and say by how much
# ---------------------------------------------------------------------------


async def test_over_the_cap_refuses_with_422_naming_total_and_limit(
    authenticated_client: AsyncClient,
    test_session: AsyncSession,
    small_model: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the bound the caller gets 422, and the body names the real numbers.

    ⚠️ The CAP is monkeypatched down to 1, rather than seeding 6,001 elements to
    cross the real ``DEFAULT_MAX_INPUT_ELEMENTS``. Seeding the real number would
    add minutes of INSERTs to every CI run to exercise a branch that a two-row
    fixture exercises identically — the code path does not know or care how the
    row count got above the cap. The cap is read from the module global at call
    time precisely so this is patchable without reaching into the helper.
    """
    monkeypatch.setattr(architecture_tools, "DEFAULT_MAX_INPUT_ELEMENTS", 1)

    resp = await authenticated_client.post(
        "/architecture/tools/gap-analysis", json={"project_id": _PROJECT}
    )
    assert resp.status_code == 422, resp.text

    error = resp.json()["error"]
    assert error["code"] == "input_too_large"
    assert error["status"] == 422
    # The numbers must survive to the WIRE. `main._http_exception_to_response`
    # flattens a dict detail to `code` + `message` and drops every sibling key,
    # so the message text is the only place an operator actually reads them.
    assert "2" in error["message"], error["message"]  # the true total
    assert "1" in error["message"], error["message"]  # the limit that fired
    assert "architecture elements" in error["message"]

    # And the loader itself raises the domain exception with exact fields —
    # other routers import these loaders, so the exception, not the 422, is the
    # shared contract.
    with pytest.raises(InputTooLarge) as excinfo:
        await _load_elements(test_session, _TENANT, _PROJECT)
    assert excinfo.value.total == 2
    assert excinfo.value.limit == 1
    assert excinfo.value.label == "architecture elements"


async def test_relationships_use_their_own_cap(
    test_session: AsyncSession,
    small_model: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relationships refuse against ``DEFAULT_MAX_INPUT_RELATIONSHIPS``, not the element cap.

    The element cap is left at its real value and only the relationship cap is
    lowered, so a loader wired to the wrong constant reads as "no refusal at all"
    here rather than passing on the other ceiling's coattails.
    """
    monkeypatch.setattr(architecture_tools, "DEFAULT_MAX_INPUT_RELATIONSHIPS", 1)

    # One relationship seeded, cap 1 — at the cap, not over it.
    assert len(await _load_relationships(test_session, _TENANT, _PROJECT)) == 1

    extra = _relationship(
        "second-edge",
        uuid.UUID(small_model["target_id"]),
        uuid.UUID(small_model["baseline_id"]),
    )
    test_session.add(extra)
    await test_session.commit()

    with pytest.raises(InputTooLarge) as excinfo:
        await _load_relationships(test_session, _TENANT, _PROJECT)
    assert excinfo.value.label == "architecture relationships"
    assert (excinfo.value.total, excinfo.value.limit) == (2, 1)


# ---------------------------------------------------------------------------
# 3. The projected dict shape did not change
# ---------------------------------------------------------------------------


async def test_element_dict_shape_is_exactly_the_pre_change_key_set(
    test_session: AsyncSession, small_model: dict
) -> None:
    """Every loader's dict carries exactly the keys it carried before.

    The loaders now select COLUMNS instead of whole ORM entities, which is the
    memory win but is also the one change that can silently drop a field: omit a
    column from the select and the projection quietly stops emitting its key.
    Downstream (``tools/architecture/*``, ``_bound_containers``) reads these dicts with ``.get()``, so a
    missing key degrades to ``None`` rather than raising — it would surface as a
    wrong report, not as an error. Hence: pinned literally, here.
    """
    elements = await _load_elements(test_session, _TENANT, _PROJECT)
    assert len(elements) == 2
    for elem in elements:
        assert set(elem) == _ELEMENT_KEYS

    known = next(e for e in elements if e["slug"] == "legacy-billing")
    assert known == {
        "id": small_model["baseline_id"],
        "slug": "legacy-billing",
        "title": "Legacy Billing",
        "description": "description of legacy-billing",
        "layer": "application",
        "element_type": "application_component",
        "is_synthetic": False,
        "properties": {"seeded_by": "test_arch_input_bounds"},
        # No structured realisation binding seeded, so the sub-query that
        # resolves it must still run and still stamp an explicit empty list.
        "code_bindings": [],
        "status": "draft",
        "architecture_state": "baseline",
        "parent_id": None,
    }

    relationships = await _load_relationships(test_session, _TENANT, _PROJECT)
    assert len(relationships) == 1
    assert set(relationships[0]) == _RELATIONSHIP_KEYS
    # ids are stringified, not left as UUID objects — the pre-change behaviour
    # every downstream index (``element_index``, ``rel_index``) depends on.
    assert relationships[0]["source_id"] == small_model["baseline_id"]
    assert relationships[0]["target_id"] == small_model["target_id"]
    assert relationships[0]["relationship_type"] == "realization"

    views = await _load_views(test_session, _TENANT, _PROJECT)
    assert len(views) == 1
    assert set(views[0]) == _VIEW_KEYS
    assert views[0]["viewpoint"] == "application_cooperation"

    standards = await _load_standards(test_session, _TENANT, _PROJECT)
    assert len(standards) == 1
    assert set(standards[0]) == _STANDARD_KEYS
    assert standards[0] == {
        "id": standards[0]["id"],
        "title": "Input Bounds Standard",
        "standard_type": "policy",
        "status": "assess",
        "compliance_level": "recommended",
    }


async def test_element_code_binding_still_resolves_from_the_structured_table(
    test_session: AsyncSession,
) -> None:
    """The ``element_ids`` sub-query survived the column-select conversion.

    ``_load_elements`` builds its binding index from a second query keyed on the
    ids of the rows it just loaded. Selecting columns instead of entities changes
    what ``r.id`` IS (a ``Row`` attribute rather than an ORM attribute), so this
    asserts the ``.in_(element_ids)`` still matches.
    """
    from models.architecture import ArchitectureElementRealisationBinding

    elem = _element("bound-artifact", title="Bound Artifact")
    test_session.add(elem)
    await test_session.commit()
    await test_session.refresh(elem)

    test_session.add(
        ArchitectureElementRealisationBinding(
            element_id=elem.id,
            tenant_id=_TENANT,
            binding_kind="coding_component",
            container="coding-analysis",
            component_id="component-42",
        )
    )
    # A09: a SECOND binding on the same element, inserted later but sorting
    # first. The loader used to keep one arbitrary survivor per element.
    test_session.add(
        ArchitectureElementRealisationBinding(
            element_id=elem.id,
            tenant_id=_TENANT,
            binding_kind="coding_component",
            container="a-first",
            component_id="component-7",
        )
    )
    await test_session.commit()

    loaded = await _load_elements(test_session, _TENANT, _PROJECT)
    assert [e["code_bindings"] for e in loaded] == [
        [
            {"container": "a-first", "component_id": "component-7"},
            {"container": "coding-analysis", "component_id": "component-42"},
        ]
    ]


# ---------------------------------------------------------------------------
# 4. ORDER BY id — the bound returns a REPRODUCIBLE prefix
# ---------------------------------------------------------------------------


async def test_order_by_id_is_deterministic(test_session: AsyncSession) -> None:
    """The same call twice returns the same order, and that order is by id.

    ``LIMIT`` without ``ORDER BY`` licenses Postgres to return a different subset
    per execution, which would make the bound answer differently on two identical
    requests — and make the ``total`` in a refusal name a population the caller
    cannot reproduce.

    The seeded ids are assigned explicitly in the REVERSE of insertion order, so
    a loader that merely happened to return physical/insertion order would fail
    this rather than pass it by luck.
    """
    ids = [uuid.UUID(f"0000000{n}-0000-7000-8000-00000000000{n}") for n in (1, 2, 3, 4)]
    for position, element_id in enumerate(reversed(ids)):
        test_session.add(_element(f"ordered-{position}", element_id=element_id))
    await test_session.commit()

    first = await _load_elements(test_session, _TENANT, _PROJECT)
    second = await _load_elements(test_session, _TENANT, _PROJECT)

    assert [e["id"] for e in first] == [e["id"] for e in second]
    assert [e["id"] for e in first] == [str(i) for i in ids]
    # Insertion order was the reverse, so this test could not have passed by
    # accident on an unordered query that happened to return rows as written.
    assert [e["slug"] for e in first] == ["ordered-3", "ordered-2", "ordered-1", "ordered-0"]
