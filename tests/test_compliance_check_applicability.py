"""``POST /architecture/tools/compliance-check`` — never exercised end-to-end.

``check_compliance`` (``src/tools/architecture/compliance.py``) already has a
large PURE-FUNCTION unit suite in ``tests/test_arch_tools.py`` — but that suite
feeds it plain Python dicts directly and never goes near this route. This
route's own wiring — ``_load_elements`` / ``_load_standards`` /
``_load_relationships`` (``src/routers/architecture_tools.py``) reading real
rows through Postgres, tenant-scoped and project-scoped — has never been
exercised by an HTTP call at all: production has **0** rows in
``architecture_standards``, so every live call today takes the
``standards=[]`` degenerate branch. The real filtering logic in
``_applicable_elements`` is therefore unreachable in production and was
untested here.

``_applicable_elements`` maps a standard's ``standard_type`` to a set of
ArchiMate layers via ``STANDARD_TYPE_LAYERS`` and keeps only the elements
whose ``layer`` is in that set (or, for ``data_standard``, whose
``element_type`` is in ``DATA_ELEMENT_TYPES``) — e.g. an
``application_standard`` applies only to ``application``-layer elements. The
first test below seeds one standard and two elements (one in-layer, one not)
and asserts the ROUTE actually delivers that filtering through the real DB
path. The second test pins the ``standards=[]`` degenerate branch explicitly,
so a regression that made ``_applicable_elements`` return ``[]`` for every
standard (which looks identical to "no standards exist" from the response
shape alone) cannot hide behind "well, production has no standards anyway".
"""

from httpx import AsyncClient

ELEMENT_APP_COMPONENT = {
    "title": "Order Service",
    "description": "Microservice for orders",
    "layer": "application",
    "element_type": "application_component",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "compliance-project",
}

ELEMENT_BUSINESS_PROCESS = {
    "title": "Process Orders",
    "description": "Handles order lifecycle",
    "layer": "business",
    "element_type": "business_process",
    "status": "draft",
    "architecture_state": "baseline",
    "project_id": "compliance-project",
}

APPLICATION_STANDARD = {
    "title": "Application Naming Standard",
    "description": "All application components must be named consistently",
    "standard_type": "application_standard",
    "status": "approved",
    "compliance_level": "mandatory",
    "project_id": "compliance-project",
}


async def _create_element(client: AsyncClient, payload: dict) -> dict:
    resp = await client.post("/architecture/elements", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_standard(client: AsyncClient, payload: dict) -> dict:
    resp = await client.post("/architecture/standards", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _run_compliance_check(client: AsyncClient, project_id: str) -> dict:
    resp = await client.post(
        "/architecture/tools/compliance-check", json={"project_id": project_id}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestComplianceCheckAppliesStandardsOnlyToMatchingLayers:
    async def test_standard_is_checked_against_applicable_elements_only(
        self, authenticated_client: AsyncClient
    ):
        """An ``application_standard`` gates the application-layer element and
        must NOT gate the business-layer element seeded alongside it.

        KILLING MUTATION: replacing ``_applicable_elements`` with
        ``list(elements)`` (i.e. ignoring the standard's type) would pull the
        business-layer element into this standard's ``unlinked_elements`` and
        double ``applicable_count`` — both assertions below catch it.
        """
        app_elem = await _create_element(authenticated_client, ELEMENT_APP_COMPONENT)
        biz_elem = await _create_element(authenticated_client, ELEMENT_BUSINESS_PROCESS)
        await _create_standard(authenticated_client, APPLICATION_STANDARD)

        result = await _run_compliance_check(authenticated_client, "compliance-project")

        assert len(result["violations"]) == 1
        violation = result["violations"][0]
        assert violation["applicable_count"] == 1

        unlinked_ids = {e["id"] for e in violation["unlinked_elements"]}
        assert app_elem["id"] in unlinked_ids
        assert biz_elem["id"] not in unlinked_ids

    async def test_zero_standards_takes_the_degenerate_branch_explicitly(
        self, authenticated_client: AsyncClient
    ):
        """Pin today's live production shape: no standards seeded at all.

        This must read differently, on its own terms, from "a standard exists
        but its applicability matches nothing" (there is no ``violations``
        entry to inspect at all here, whereas the matches-nothing case would
        still iterate an empty-``applicable`` standard and simply ``continue``
        past it) — the pairing with the test above is what pins the
        distinction the task called out.
        """
        app_elem = await _create_element(authenticated_client, ELEMENT_APP_COMPONENT)
        biz_elem = await _create_element(authenticated_client, ELEMENT_BUSINESS_PROCESS)

        result = await _run_compliance_check(authenticated_client, "compliance-project")

        assert result["compliance_score"] is None
        assert result["violations"] == []
        assert result["warnings"] == []
        assert result["compliant"] == []
        assert result["optional"] == []
        assert result["optional_adherence"] is None

        uncovered_ids = {e["id"] for e in result["uncovered_elements"]}
        assert uncovered_ids == {app_elem["id"], biz_elem["id"]}
