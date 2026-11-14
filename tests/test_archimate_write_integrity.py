"""ArchiMate write-time integrity over HTTP, against real Postgres.

Each test pins one finding of the 2026-10-26 ArchiMate plan at the API
boundary: the rule must hold for the route a caller actually uses, and a
refusal must arrive as ``422 model_integrity`` with structured findings — not
as a stringified dict (target spec §6.11.1).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.tenants import T

TENANT = T("archimate-integrity")
ALICE, BOB = "alice-archimate", "bob-archimate"


def hdrs(subject: str = ALICE, visibility: str | None = None) -> dict[str, str]:
    h = {"X-Tenant-Id": TENANT, "X-Subject-Id": subject}
    if visibility:
        h["X-Visibility"] = visibility
    return h


async def element(
    client: AsyncClient, element_type: str, *, subject: str = ALICE, **extra: Any
) -> dict[str, Any]:
    visibility = extra.pop("_visibility", None)
    body = {
        "title": f"{element_type}-{uuid.uuid4().hex[:8]}",
        "element_type": element_type,
        **extra,
    }
    resp = await client.post("/architecture/elements", json=body, headers=hdrs(subject, visibility))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def edge(
    client: AsyncClient,
    source: dict,
    rel: str,
    target: dict,
    *,
    source_type: str = "architecture_elements",
    target_type: str = "architecture_elements",
    subject: str = ALICE,
    **extra: Any,
):
    return await client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": source_type,
            "source_id": source["id"],
            "target_entity_type": target_type,
            "target_id": target["id"],
            "relationship_type": rel,
            **extra,
        },
        headers=hdrs(subject),
    )


def integrity(resp) -> list[dict]:
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "model_integrity", err
    assert err["findings"], err
    return err["findings"]


# --- elements ---------------------------------------------------------------


async def test_layer_is_derived_from_the_type_when_omitted(test_client: AsyncClient):
    el = await element(test_client, "requirement")
    assert el["layer"] == "motivation"


async def test_a_layer_that_disagrees_with_the_type_is_refused(test_client: AsyncClient):
    """A01: business + application_component used to be accepted."""
    resp = await test_client.post(
        "/architecture/elements",
        json={"title": "x", "layer": "business", "element_type": "application_component"},
        headers=hdrs(),
    )
    (finding,) = integrity(resp)
    assert finding["rule_id"] == "AM-TYP-001"
    assert finding["evidence"]["expected_layer"] == "application"


@pytest.mark.parametrize("etype", ["grouping", "location", "junction", "requirement"])
async def test_the_four_previously_missing_concepts_can_be_created(test_client, etype):
    """A10."""
    el = await element(test_client, etype)
    assert el["element_type"] == etype
    if etype == "junction":
        assert el["properties"]["junction_type"] == "and"


async def test_an_invalid_junction_type_is_refused(test_client: AsyncClient):
    resp = await test_client.post(
        "/architecture/elements",
        json={"title": "j", "element_type": "junction", "properties": {"junction_type": "xor"}},
        headers=hdrs(),
    )
    assert integrity(resp)[0]["rule_id"] == "AM-REL-003"


async def test_an_unresolved_parent_is_refused(test_client: AsyncClient):
    """A06: the cycle guard treated an unresolved parent as 'not our problem'."""
    resp = await test_client.post(
        "/architecture/elements",
        json={"title": "c", "element_type": "business_actor", "parent_id": str(uuid.uuid4())},
        headers=hdrs(),
    )
    assert integrity(resp)[0]["rule_id"] == "AM-REF-001"


async def test_another_subjects_private_parent_is_refused(test_client: AsyncClient):
    private = await element(test_client, "business_actor", subject=BOB, _visibility="user")
    resp = await test_client.post(
        "/architecture/elements",
        json={"title": "c", "element_type": "business_actor", "parent_id": private["id"]},
        headers=hdrs(ALICE),
    )
    assert integrity(resp)[0]["rule_id"] == "AM-REF-001"


async def test_a_retype_that_breaks_an_incident_edge_is_refused(test_client: AsyncClient):
    """A06: element_type changed through PUT without revalidating its edges."""
    actor = await element(test_client, "business_actor")
    role = await element(test_client, "business_role")
    assert (await edge(test_client, actor, "assignment", role)).status_code in (200, 201)
    resp = await test_client.put(
        f"/architecture/elements/{actor['id']}",
        json={"title": actor["title"], "element_type": "data_object"},
        headers=hdrs(),
    )
    assert integrity(resp)[0]["rule_id"] == "AM-REL-001"


async def test_a_retype_that_keeps_edges_valid_is_accepted(test_client: AsyncClient):
    actor = await element(test_client, "business_actor")
    role = await element(test_client, "business_role")
    assert (await edge(test_client, actor, "association", role)).status_code in (200, 201)
    resp = await test_client.put(
        f"/architecture/elements/{actor['id']}",
        json={"title": actor["title"], "element_type": "business_collaboration"},
        headers=hdrs(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["layer"] == "business"


# --- relationships ------------------------------------------------------------


async def test_a_triple_the_matrix_forbids_is_refused_with_evidence(test_client: AsyncClient):
    """A02: application_component assignment data_object was accepted (554 prod rows)."""
    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    (finding,) = integrity(await edge(test_client, comp, "assignment", data))
    assert finding["rule_id"] == "AM-REL-001"
    assert finding["evidence"]["allowed"] == ["access", "association"]


async def test_the_matrix_permitted_alternative_is_accepted(test_client: AsyncClient):
    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    resp = await edge(test_client, comp, "access", data, properties={"access_type": "read"})
    assert resp.status_code in (200, 201), resp.text


async def test_toc_and_governance_types_are_refused_between_archimate_elements(
    test_client: AsyncClient,
):
    """A03: toc_conflict between two architecture elements returned valid=true."""
    a = await element(test_client, "business_actor")
    b = await element(test_client, "business_actor")
    for rel in ("toc_conflict", "traceability"):
        assert integrity(await edge(test_client, a, rel, b))[0]["rule_id"] == "AM-TYP-002", rel


async def test_a_relationship_attribute_on_the_wrong_type_is_refused(test_client: AsyncClient):
    actor = await element(test_client, "business_actor")
    role = await element(test_client, "business_role")
    resp = await edge(test_client, actor, "assignment", role, properties={"access_type": "read"})
    assert integrity(resp)[0]["rule_id"] == "AM-REL-003"


async def test_a_relationship_may_end_on_a_relationship(test_client: AsyncClient):
    """A10: Grouping --aggregation--> relationship is in the matrix (``cgo``)."""
    actor = await element(test_client, "business_actor")
    role = await element(test_client, "business_role")
    r1 = (await edge(test_client, actor, "assignment", role)).json()
    group = await element(test_client, "grouping")
    resp = await edge(
        test_client, group, "aggregation", r1, target_type="architecture_relationships"
    )
    assert resp.status_code in (200, 201), resp.text


async def test_a_relationship_to_its_own_end_is_refused(test_client: AsyncClient):
    actor = await element(test_client, "business_actor")
    role = await element(test_client, "business_role")
    r1 = (await edge(test_client, actor, "assignment", role)).json()
    resp = await edge(
        test_client, actor, "association", r1, target_type="architecture_relationships"
    )
    assert integrity(resp)[0]["rule_id"] == "AM-REL-002"


async def test_another_subjects_private_element_is_not_a_valid_endpoint(test_client: AsyncClient):
    """A04: endpoint checks were tenant-only; a private row of another subject passed."""
    mine = await element(test_client, "business_actor")
    theirs = await element(test_client, "business_role", subject=BOB, _visibility="user")
    resp = await edge(test_client, mine, "assignment", theirs)
    # Same answer as a missing end (spec §7.3: unresolved OR invisible), so
    # the private row's existence does not leak.
    (finding,) = integrity(resp)
    assert finding["rule_id"] == "AM-REF-001"
    missing = await edge(test_client, mine, "assignment", {"id": str(uuid.uuid4())})
    (gone,) = integrity(missing)
    assert gone["rule_id"] == finding["rule_id"] == "AM-REF-001"


async def test_a_junction_cannot_mix_relationship_types(test_client: AsyncClient):
    actor = await element(test_client, "business_actor")
    junction = await element(test_client, "junction")
    proc = await element(test_client, "business_process")
    assert (await edge(test_client, actor, "assignment", junction)).status_code in (200, 201)
    findings = integrity(await edge(test_client, junction, "serving", proc))
    assert "AM-JCT-001" in {f["rule_id"] for f in findings}


async def test_explicit_null_identity_on_update_is_refused(test_client: AsyncClient):
    """A07: ``{"source_id": null}`` survived exclude_unset into the service."""
    actor = await element(test_client, "business_actor")
    role = await element(test_client, "business_role")
    r1 = (await edge(test_client, actor, "assignment", role)).json()
    resp = await test_client.put(
        f"/architecture/relationships/{r1['id']}", json={"source_id": None}, headers=hdrs()
    )
    assert resp.status_code == 422, resp.text


# --- reference routes ------------------------------------------------------------


async def test_registry_routes(test_client: AsyncClient):
    concepts = (await test_client.get("/architecture/metamodel/concepts", headers=hdrs())).json()
    assert len(concepts["concepts"]) == 61
    assert concepts["reference"]["version"] == "5.10.0"

    cells = (
        await test_client.get(
            "/architecture/metamodel/relationships",
            params={"source_type": "application_component", "target_type": "data_object"},
            headers=hdrs(),
        )
    ).json()
    assert cells == [
        {
            "source_type": "application_component",
            "target_type": "data_object",
            "allowed": ["access", "association"],
        }
    ]

    vps = (await test_client.get("/architecture/viewpoints", headers=hdrs())).json()
    assert sum(v["source"] == "archi" for v in vps) == 25
    one = await test_client.get("/architecture/viewpoints/outcome_realization", headers=hdrs())
    assert one.status_code == 200 and "outcome" in one.json()["allowed_concepts"]
    assert (
        await test_client.get("/architecture/viewpoints/nope", headers=hdrs())
    ).status_code == 404

    rulesets = (await test_client.get("/modeling/rulesets", headers=hdrs())).json()
    ids = {r["ruleset_id"] for r in rulesets}
    assert ids == {"archimate-3.2/archi-5.10.0", "uml-2.5.1-p1"}
    full = await test_client.get("/modeling/rulesets/archimate-3.2/archi-5.10.0", headers=hdrs())
    assert full.status_code == 200 and full.json()["rules"]


async def test_stateless_validate_route(test_client: AsyncClient):
    body = {
        "concepts": [
            {"id": "a", "type": "application_component", "name": "A"},
            {"id": "d", "type": "data_object", "name": "D"},
        ],
        "relationships": [{"id": "r", "type": "assignment", "source": "a", "target": "d"}],
    }
    resp = await test_client.post("/architecture/tools/validate", json=body, headers=hdrs())
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["clean"] is False
    assert [f["rule_id"] for f in report["findings"]] == ["AM-REL-001"]

    resp = await test_client.post(
        "/architecture/tools/validate", json={**body, "rules": ["AM-NOPE"]}, headers=hdrs()
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unknown_rule"


async def test_stateless_validate_refuses_oversize_input(test_client: AsyncClient, monkeypatch):
    from settings import get_settings

    monkeypatch.setattr(get_settings(), "model_sync_max_concepts", 1)
    body = {"concepts": [{"id": "a", "type": "node"}, {"id": "b", "type": "node"}]}
    resp = await test_client.post("/architecture/tools/validate", json=body, headers=hdrs())
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "input_too_large"


async def test_uml_routes(test_client: AsyncClient):
    meta = await test_client.get("/uml/metamodel", headers=hdrs())
    assert meta.status_code == 200, meta.text
    assert "class" in meta.json()["element_kinds"]
    resp = await test_client.post(
        "/uml/tools/validate",
        json={"elements": [{"id": "x", "kind": "state_machine", "name": "S"}]},
        headers=hdrs(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clean"] is False
    assert "UML-UNSUP-001" in {f["rule_id"] for f in resp.json()["findings"]}


async def test_tool_loaders_hide_another_subjects_private_elements(test_client: AsyncClient):
    """A04: the /architecture/tools loaders filtered tenant + project only, so a
    subject's private element fed every other subject's analysis."""
    project = f"vis-{uuid.uuid4().hex[:8]}"
    shared = await element(test_client, "application_component", project_id=project)
    private = await element(
        test_client, "application_component", subject=BOB, _visibility="user", project_id=project
    )

    async def ids(subject: str) -> set[str]:
        resp = await test_client.post(
            "/architecture/tools/generate-view",
            json={"project_id": project, "viewpoint": "application_cooperation"},
            headers=hdrs(subject),
        )
        assert resp.status_code == 200, resp.text
        return set(resp.json()["element_ids"])

    assert await ids(ALICE) == {shared["id"]}
    assert await ids(BOB) == {shared["id"], private["id"]}


# --- review findings (independent review, 2026-10-26) -------------------------------


async def test_a_retype_cannot_break_an_edge_through_a_junction(test_client: AsyncClient):
    """actor -access-> junction -access-> business_object is valid; retyping the
    object to deliverable makes the indirect access illegal (AM-JCT-002)."""
    actor = await element(test_client, "business_actor")
    junction = await element(test_client, "junction")
    obj = await element(test_client, "business_object")
    assert (await edge(test_client, actor, "access", junction)).status_code in (200, 201)
    assert (await edge(test_client, junction, "access", obj)).status_code in (200, 201)
    resp = await test_client.put(
        f"/architecture/elements/{obj['id']}",
        json={"title": obj["title"], "element_type": "deliverable"},
        headers=hdrs(),
    )
    assert "AM-JCT-002" in {f["rule_id"] for f in integrity(resp)}


async def test_a_retype_cannot_break_an_edge_to_a_relationship(test_client: AsyncClient):
    """plateau -aggregation-> (relationship) is legal; work_package -aggregation->
    (relationship) is not."""
    actor = await element(test_client, "business_actor")
    role = await element(test_client, "business_role")
    r1 = (await edge(test_client, actor, "assignment", role)).json()
    plateau = await element(test_client, "plateau")
    resp = await edge(
        test_client, plateau, "aggregation", r1, target_type="architecture_relationships"
    )
    assert resp.status_code in (200, 201), resp.text
    resp = await test_client.put(
        f"/architecture/elements/{plateau['id']}",
        json={"title": plateau["title"], "element_type": "work_package"},
        headers=hdrs(),
    )
    assert integrity(resp)[0]["rule_id"] == "AM-REL-001"


async def test_a_specialization_cycle_cannot_be_written(test_client: AsyncClient):
    a = await element(test_client, "business_actor")
    b = await element(test_client, "business_actor")
    c = await element(test_client, "business_actor")
    assert (await edge(test_client, a, "specialization", b)).status_code in (200, 201)
    assert (await edge(test_client, b, "specialization", c)).status_code in (200, 201)
    assert integrity(await edge(test_client, c, "specialization", a))[0]["rule_id"] == "AM-CYC-002"


async def test_an_archimate_edge_cannot_end_on_a_pm_link(test_client: AsyncClient):
    pm_link = await test_client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "pm_resources",
            "source_id": str(uuid.uuid4()),
            "target_entity_type": "requirements",
            "target_id": str(uuid.uuid4()),
            "relationship_type": "association",
        },
        headers=hdrs(),
    )
    if pm_link.status_code not in (200, 201):
        pytest.fail(f"precondition: could not create a PM link: {pm_link.text}")
    actor = await element(test_client, "business_actor")
    resp = await edge(
        test_client, actor, "association", pm_link.json(), target_type="architecture_relationships"
    )
    assert integrity(resp)[0]["rule_id"] == "AM-TYP-002"


async def test_a_legacy_mixed_junction_does_not_block_an_edge_of_a_type_it_has(
    test_client: AsyncClient, test_session
):
    """A write is refused for what it BREAKS. A junction that is already mixed
    (a pre-gate row) may still gain an edge of a type it already has; adding a
    NEW type is refused."""
    from models.architecture import ArchitectureRelationship

    actor = await element(test_client, "business_actor")
    junction = await element(test_client, "junction")
    proc = await element(test_client, "business_process")
    role = await element(test_client, "business_role")
    assert (await edge(test_client, actor, "assignment", junction)).status_code in (200, 201)
    # Seed the legacy row directly: the API would refuse it.
    test_session.add(
        ArchitectureRelationship(
            slug=f"legacy-{uuid.uuid4().hex[:8]}",
            tenant_id=TENANT,
            tenant_name="t",
            subject_id=ALICE,
            visibility="tenant",
            source_entity_type="architecture_elements",
            source_id=uuid.UUID(junction["id"]),
            target_entity_type="architecture_elements",
            target_id=uuid.UUID(proc["id"]),
            relationship_type="serving",
        )
    )
    await test_session.commit()
    resp = await edge(test_client, junction, "assignment", role)
    assert resp.status_code in (200, 201), resp.text
    resp = await edge(test_client, junction, "triggering", proc)
    assert "AM-JCT-001" in {f["rule_id"] for f in integrity(resp)}


async def test_the_synthetic_upsert_cannot_retype_past_its_edges(test_client: AsyncClient):
    """The code-binding upsert is keyed by binding, not type, so a re-accept
    can change element_type. It must hit the same retype check as PUT."""
    binding = {"code_binding": {"container": f"c-{uuid.uuid4().hex[:6]}", "component_id": "x"}}
    first = await test_client.post(
        "/architecture/elements",
        json={
            "title": "svc",
            "element_type": "application_component",
            "is_synthetic": True,
            "properties": binding,
        },
        headers=hdrs(),
    )
    assert first.status_code in (200, 201), first.text
    comp = first.json()
    # component -assignment-> function is legal; role -assignment-> an
    # application function is not (Archi 5.10.0). Access to a data_object
    # would NOT do here: business_role may access it too.
    fn = await element(test_client, "application_function")
    assert (await edge(test_client, comp, "assignment", fn)).status_code in (200, 201)
    again = await test_client.post(
        "/architecture/elements",
        json={
            "title": "svc",
            "element_type": "business_role",
            "is_synthetic": True,
            "properties": binding,
        },
        headers=hdrs(),
    )
    assert integrity(again)[0]["rule_id"] == "AM-REL-001"


async def test_the_upsert_race_recovery_cannot_retype_past_its_edges(
    test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """A racer that loses the insert recovers the winner and updates it; that
    update can change element_type too, so it takes the same retype check."""
    import services.architecture_upsert as upsert

    binding = {"code_binding": {"container": f"c-{uuid.uuid4().hex[:6]}", "component_id": "x"}}
    body = {"title": "svc", "is_synthetic": True, "properties": binding}
    first = await test_client.post(
        "/architecture/elements",
        json={**body, "element_type": "application_component"},
        headers=hdrs(),
    )
    assert first.status_code in (200, 201), first.text
    comp = first.json()
    fn = await element(test_client, "application_function")
    assert (await edge(test_client, comp, "assignment", fn)).status_code in (200, 201)

    async def lost_the_race(*_a: Any, **_k: Any) -> None:
        return None

    # The fast path misses, so the insert hits the binding unique index and
    # the survivor is recovered by its identity columns.
    monkeypatch.setattr(upsert, "find_synthetic_by_code_binding", lost_the_race)
    again = await test_client.post(
        "/architecture/elements",
        json={**body, "element_type": "business_role"},
        headers=hdrs(),
    )
    assert integrity(again)[0]["rule_id"] == "AM-REL-001"


@pytest.mark.parametrize(
    ("source_type", "rel", "target_type", "props", "key"),
    [
        ("application_component", "access", "data_object", {"access_type": 5}, "access_type"),
        ("business_actor", "association", "business_actor", {"directed": "maybe"}, "directed"),
        ("goal", "influence", "goal", {"strength": "x" * 51}, "strength"),
    ],
)
async def test_an_off_schema_relationship_attribute_is_a_422_not_a_500(
    test_client: AsyncClient,
    source_type: str,
    rel: str,
    target_type: str,
    props: dict,
    key: str,
):
    src = await element(test_client, source_type)
    tgt = await element(test_client, target_type)
    resp = await edge(test_client, src, rel, tgt, properties=props)
    found = integrity(resp)
    assert [(f["rule_id"], f["path"]) for f in found] == [("AM-REL-003", f"/properties/{key}")]


async def test_a_legacy_edge_with_off_schema_attributes_does_not_block_its_neighbours(
    test_client: AsyncClient, test_session
):
    from models.architecture import ArchitectureRelationship

    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    # Seed directly: the API refuses these attribute values.
    test_session.add(
        ArchitectureRelationship(
            slug=f"legacy-{uuid.uuid4().hex[:8]}",
            tenant_id=TENANT,
            tenant_name="t",
            subject_id=ALICE,
            visibility="tenant",
            source_entity_type="architecture_elements",
            source_id=uuid.UUID(comp["id"]),
            target_entity_type="architecture_elements",
            target_id=uuid.UUID(data["id"]),
            relationship_type="access",
            properties={"access_type": 5, "strength": "x" * 80, "directed": "maybe"},
        )
    )
    await test_session.commit()
    fn = await element(test_client, "application_function")
    resp = await edge(test_client, comp, "assignment", fn)
    assert resp.status_code in (200, 201), resp.text


async def test_a_put_that_keeps_legacy_properties_is_not_refused_for_them(
    test_client: AsyncClient, test_session
):
    """A PUT that omits ``properties`` keeps the stored value; that value is
    read like any stored row, not refused (or crashed on) as client input.
    Sending the same bad attribute is still a 422."""
    from models.architecture import ArchitectureRelationship

    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    other = await element(test_client, "data_object")
    row = ArchitectureRelationship(
        slug=f"legacy-{uuid.uuid4().hex[:8]}",
        tenant_id=TENANT,
        tenant_name="t",
        subject_id=ALICE,
        visibility="tenant",
        source_entity_type="architecture_elements",
        source_id=uuid.UUID(comp["id"]),
        target_entity_type="architecture_elements",
        target_id=uuid.UUID(data["id"]),
        relationship_type="access",
        properties={"access_type": 5},
    )
    test_session.add(row)
    await test_session.commit()
    url = f"/architecture/relationships/{row.id}"
    resp = await test_client.put(url, json={"target_id": other["id"]}, headers=hdrs())
    assert resp.status_code == 200, resp.text
    resp = await test_client.put(url, json={"properties": {"access_type": 5}}, headers=hdrs())
    assert [f["rule_id"] for f in integrity(resp)] == ["AM-REL-003"]


@pytest.mark.parametrize("stored", [["x"], "junk"])
async def test_kept_non_object_properties_do_not_crash_the_check(
    test_client: AsyncClient, test_session, stored: Any
):
    """JSONB can hold a non-object. The check reads it as no attributes. (The
    HTTP response model still requires an object; that predates this check.)"""
    from services.archimate_integrity import check_relationship_write

    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    await check_relationship_write(
        test_session,
        tenant_id=TENANT,
        subject_id=ALICE,
        source_entity_type="architecture_elements",
        source_id=comp["id"],
        target_entity_type="architecture_elements",
        target_id=data["id"],
        relationship_type="access",
        properties=stored,
        properties_sent=False,
    )


def _legacy_edge(source: dict, rel: str, target: dict, **extra: Any):
    from models.architecture import ArchitectureRelationship

    return ArchitectureRelationship(
        slug=f"legacy-{uuid.uuid4().hex[:8]}",
        tenant_id=TENANT,
        tenant_name="t",
        subject_id=ALICE,
        visibility="tenant",
        source_entity_type="architecture_elements",
        source_id=uuid.UUID(source["id"]),
        target_entity_type="architecture_elements",
        target_id=uuid.UUID(target["id"]),
        relationship_type=rel,
        **extra,
    )


async def test_reducing_a_legacy_mixed_junction_is_not_refused(
    test_client: AsyncClient, test_session
):
    """A junction mixed as (assignment, serving, triggering) that an update
    reduces to (assignment, serving) is BETTER, not newly broken. The finding's
    type set is a subset of the one before, so the write is accepted."""
    actor = await element(test_client, "business_actor")
    junction = await element(test_client, "junction")
    proc = await element(test_client, "business_process")
    proc2 = await element(test_client, "business_process")
    assert (await edge(test_client, actor, "assignment", junction)).status_code in (200, 201)
    test_session.add(_legacy_edge(junction, "serving", proc))
    trig = _legacy_edge(junction, "triggering", proc2)
    test_session.add(trig)
    await test_session.commit()
    resp = await test_client.put(
        f"/architecture/relationships/{trig.id}",
        json={"relationship_type": "serving"},
        headers=hdrs(),
    )
    assert resp.status_code == 200, resp.text


async def test_moving_both_ends_keeps_a_legacy_attribute_finding_in_before(
    test_client: AsyncClient, test_session
):
    """A PUT that moves BOTH ends of a row carrying a stale attribute (AM-REL-003)
    must not be refused for that pre-existing finding: the edited row is in the
    before-snapshot even though it is not incident on its new ends."""
    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    comp2 = await element(test_client, "application_component")
    data2 = await element(test_client, "data_object")
    row = _legacy_edge(comp, "access", data, properties={"strength": "+"})
    test_session.add(row)
    await test_session.commit()
    resp = await test_client.put(
        f"/architecture/relationships/{row.id}",
        json={"source_id": comp2["id"], "target_id": data2["id"]},
        headers=hdrs(),
    )
    assert resp.status_code == 200, resp.text
    # Holding the old row in "before" must not mask what the move BREAKS: both
    # ends moved onto a triple the matrix forbids is still refused.
    resp = await test_client.put(
        f"/architecture/relationships/{row.id}",
        json={"source_id": data["id"], "target_id": comp["id"]},
        headers=hdrs(),
    )
    assert "AM-REL-001" in {f["rule_id"] for f in integrity(resp)}


@pytest.mark.parametrize("stored", [["x"], "junk"])
async def test_reading_a_relationship_with_non_object_properties_is_not_a_500(
    test_client: AsyncClient, test_session, stored: Any
):
    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    row = _legacy_edge(comp, "access", data, properties=stored)
    test_session.add(row)
    await test_session.commit()
    resp = await test_client.get(f"/architecture/relationships/{row.id}", headers=hdrs())
    assert resp.status_code == 200, resp.text
    assert resp.json()["properties"] is None
    resp = await test_client.get(
        "/architecture/relationships", params={"limit": 500}, headers=hdrs()
    )
    assert resp.status_code == 200, resp.text
    assert str(row.id) in {r["id"] for r in _items(resp.json())}


def _items(body: Any) -> list[dict]:
    return body if isinstance(body, list) else body.get("items", [])


# --- finding identity: a NEW violation on a row that already had one ---------


async def test_a_new_attribute_violation_is_refused_beside_a_legacy_one(
    test_client: AsyncClient, test_session
):
    """The write gate keyed AM-REL-003 on (rule, relationship) only, so a row that
    already carried one (stale ``strength`` on an access edge) could gain a SECOND,
    different one (an invalid ``access_type``) and be accepted. The new violation is
    refused; the pre-existing one alone still does not block."""
    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    row = _legacy_edge(comp, "access", data, properties={"strength": "+"})
    test_session.add(row)
    await test_session.commit()
    url = f"/architecture/relationships/{row.id}"
    resp = await test_client.put(
        url, json={"properties": {"strength": "+", "access_type": "bogus"}}, headers=hdrs()
    )
    found = integrity(resp)
    assert [(f["rule_id"], f["path"]) for f in found] == [("AM-REL-003", "/access_type")], found
    resp = await test_client.put(
        url, json={"properties": {"strength": "+", "access_type": "read"}}, headers=hdrs()
    )
    assert resp.status_code == 200, resp.text


async def test_changing_an_invalid_attribute_value_is_a_new_violation(
    test_client: AsyncClient, test_session
):
    """Same attribute, different offending value: the write introduces it, so it is
    refused. Resending the stored value unchanged is not."""
    comp = await element(test_client, "application_component")
    data = await element(test_client, "data_object")
    row = _legacy_edge(comp, "access", data, properties={"access_type": "bogus"})
    test_session.add(row)
    await test_session.commit()
    url = f"/architecture/relationships/{row.id}"
    resp = await test_client.put(url, json={"properties": {"access_type": "worse"}}, headers=hdrs())
    assert [f["rule_id"] for f in integrity(resp)] == ["AM-REL-003"]
    resp = await test_client.put(
        url,
        json={"description": "touched", "properties": {"access_type": "bogus"}},
        headers=hdrs(),
    )
    assert resp.status_code == 200, resp.text


async def test_a_different_own_end_violation_on_the_same_row_is_refused(
    test_client: AsyncClient, test_session
):
    """AM-REL-002 likewise: a legacy edge ``actor -> r1`` (r1 already has actor as an
    end) moved onto ``r3`` (also ending on actor) is a NEW violation, not the old one."""
    actor = await element(test_client, "business_actor")
    role = await element(test_client, "business_role")
    role2 = await element(test_client, "business_role")
    r1 = (await edge(test_client, actor, "assignment", role)).json()
    r3 = (await edge(test_client, actor, "assignment", role2)).json()
    row = _legacy_edge(actor, "association", {"id": r1["id"]})
    row.target_entity_type = "architecture_relationships"
    test_session.add(row)
    await test_session.commit()
    url = f"/architecture/relationships/{row.id}"
    resp = await test_client.put(url, json={"description": "touched"}, headers=hdrs())
    assert resp.status_code == 200, resp.text
    resp = await test_client.put(url, json={"target_id": r3["id"]}, headers=hdrs())
    assert [f["rule_id"] for f in integrity(resp)] == ["AM-REL-002"]
