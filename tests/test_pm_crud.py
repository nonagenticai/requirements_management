"""Tests for all PM CRUD endpoints: Programmes, Projects, Resources, Risks, Benefits.

All tests hit real PostgreSQL through the FastAPI test client — no mocks.
"""

import uuid
from uuid import uuid4

from httpx import AsyncClient

from tests.briefs import MIN_BRIEF
from tests.project_types import typed
from tests.tenants import T

# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

PROGRAMME_PAYLOAD = {
    "title": "Test Programme Alpha",
    "description": "A programme for integration testing",
    "status": "identifying",
}

PROJECT_PAYLOAD = {
    "title": "Test Project Beta",
    "description": "A project for integration testing",
    "status": "initiating",
    "budget": 50000.0,
    "currency": "EUR",
    "brief": MIN_BRIEF,
}

RESOURCE_PAYLOAD = {
    "title": "Senior Developer",
    "description": "Full-stack senior developer",
    "resource_type": "person",
    "capacity_per_day": 7.5,
    "cost_per_unit": 120.0,
    "skills": ["python", "fastapi", "postgresql"],
    "calendar_data": {"timezone": "Europe/Amsterdam"},
}

RISK_PAYLOAD = {
    "title": "Database migration failure",
    "description": "Risk of data loss during PostgreSQL migration",
    "project_id": "test-project",
    "probability": 0.3,
    "impact_score": 4.0,
    "category": "technical",
    "response_strategy": "mitigate",
    "response_plan": "Run migration in staging first with rollback scripts",
    "status": "identified",
    "owner": "dba-team",
}

BENEFIT_PAYLOAD = {
    "title": "Reduced deployment time",
    "description": "CI/CD pipeline reduces deployment from 2h to 15min",
    "benefit_type": "non_financial",
    "measurement": "Average deployment duration in minutes",
    "baseline_value": 120.0,
    "target_value": 15.0,
    "status": "expected",
    "owner": "platform-team",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_programme(client: AsyncClient, payload: dict | None = None) -> dict:
    resp = await client.post("/pm/programmes", json=payload or PROGRAMME_PAYLOAD)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_project(client: AsyncClient, payload: dict | None = None) -> dict:
    resp = await client.post("/pm/projects", json=await typed(client, payload or PROJECT_PAYLOAD))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_resource(client: AsyncClient, payload: dict | None = None) -> dict:
    resp = await client.post("/pm/resources", json=payload or RESOURCE_PAYLOAD)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_risk(client: AsyncClient, payload: dict | None = None) -> dict:
    resp = await client.post("/pm/risks", json=payload or RISK_PAYLOAD)
    # A fresh risk insert with a project_id returns 201 (get-or-create); an
    # in-place update on a re-run returns 200.
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _create_benefit(client: AsyncClient, payload: dict | None = None) -> dict:
    resp = await client.post("/pm/benefits", json=payload or BENEFIT_PAYLOAD)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ===================================================================
# 1. Programme CRUD
# ===================================================================


class TestProgrammeCRUD:
    async def test_create_programme(self, authenticated_client: AsyncClient):
        data = await _create_programme(authenticated_client)
        assert data["title"] == "Test Programme Alpha"
        assert data["description"] == "A programme for integration testing"
        assert data["status"] == "identifying"
        assert data["tenant_id"] == T("user-123")
        assert "id" in data
        assert "slug" in data
        assert "created_at" in data
        assert "updated_at" in data

    async def test_get_programme_by_id(self, authenticated_client: AsyncClient):
        created = await _create_programme(authenticated_client)
        pid = created["id"]

        resp = await authenticated_client.get(f"/pm/programmes/{pid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == pid
        assert data["title"] == "Test Programme Alpha"
        assert data["tenant_id"] == T("user-123")

    async def test_list_programmes_with_pagination(self, authenticated_client: AsyncClient):
        await _create_programme(authenticated_client)
        await _create_programme(
            authenticated_client,
            {**PROGRAMME_PAYLOAD, "title": "Programme Two", "status": "defining"},
        )

        resp = await authenticated_client.get("/pm/programmes")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2
        assert "X-Total-Count" in resp.headers
        assert int(resp.headers["X-Total-Count"]) >= 2

    async def test_list_programmes_filter_by_status(self, authenticated_client: AsyncClient):
        await _create_programme(authenticated_client)
        await _create_programme(
            authenticated_client,
            {**PROGRAMME_PAYLOAD, "title": "Defining Programme", "status": "defining"},
        )

        resp = await authenticated_client.get("/pm/programmes?status=defining")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["status"] == "defining" for item in items)

    async def test_update_programme(self, authenticated_client: AsyncClient):
        created = await _create_programme(authenticated_client)
        pid = created["id"]

        update = {**PROGRAMME_PAYLOAD, "title": "Updated Programme", "status": "defining"}
        resp = await authenticated_client.put(f"/pm/programmes/{pid}", json=update)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Updated Programme"
        assert data["status"] == "defining"

    async def test_delete_programme(self, authenticated_client: AsyncClient):
        created = await _create_programme(authenticated_client)
        pid = created["id"]

        resp = await authenticated_client.delete(f"/pm/programmes/{pid}")
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/pm/programmes/{pid}")
        assert resp.status_code == 404

    async def test_get_nonexistent_programme(self, authenticated_client: AsyncClient):
        fake_id = str(uuid.uuid4())
        resp = await authenticated_client.get(f"/pm/programmes/{fake_id}")
        assert resp.status_code == 404

    async def test_invalid_status_returns_422(self, authenticated_client: AsyncClient):
        payload = {**PROGRAMME_PAYLOAD, "status": "nonexistent_status"}
        resp = await authenticated_client.post("/pm/programmes", json=payload)
        assert resp.status_code == 422

    async def test_tenant_isolation(self, test_client: AsyncClient):
        """user-123 cannot see programmes created by user-789."""
        test_client.headers = {"X-Tenant-Id": T("user-123")}
        p1 = await _create_programme(test_client)

        test_client.headers = {"X-Tenant-Id": T("user-789")}
        await _create_programme(test_client, {**PROGRAMME_PAYLOAD, "title": "Other Programme"})

        # List as user-789 should not include user-123's programme
        resp = await test_client.get("/pm/programmes")
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()]
        assert p1["id"] not in ids

        # Direct GET by user-789 on user-123's programme should 403
        resp = await test_client.get(f"/pm/programmes/{p1['id']}")
        assert resp.status_code == 404


# ===================================================================
# 2. Project CRUD
# ===================================================================


class TestProjectCRUD:
    async def test_create_project(self, authenticated_client: AsyncClient):
        data = await _create_project(authenticated_client)
        assert data["title"] == "Test Project Beta"
        assert data["status"] == "initiating"
        assert data["budget"] == 50000.0
        assert data["currency"] == "EUR"
        assert data["tenant_id"] == T("user-123")
        assert "id" in data
        assert "slug" in data

    async def test_create_project_with_programme_id(self, authenticated_client: AsyncClient):
        prog = await _create_programme(authenticated_client)
        payload = {**PROJECT_PAYLOAD, "programme_id": prog["id"], "title": "Linked Project"}
        data = await _create_project(authenticated_client, payload)
        assert data["programme_id"] == prog["id"]

    async def test_get_project_by_id(self, authenticated_client: AsyncClient):
        created = await _create_project(authenticated_client)
        pid = created["id"]

        resp = await authenticated_client.get(f"/pm/projects/{pid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == pid
        assert data["title"] == "Test Project Beta"

    async def test_list_projects_with_pagination(self, authenticated_client: AsyncClient):
        await _create_project(authenticated_client)
        await _create_project(
            authenticated_client,
            {**PROJECT_PAYLOAD, "title": "Project Two"},
        )

        resp = await authenticated_client.get("/pm/projects")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2
        assert "X-Total-Count" in resp.headers
        assert int(resp.headers["X-Total-Count"]) >= 2

    async def test_list_projects_filter_by_programme_id(self, authenticated_client: AsyncClient):
        prog = await _create_programme(authenticated_client)
        await _create_project(
            authenticated_client,
            {**PROJECT_PAYLOAD, "title": "In Programme", "programme_id": prog["id"]},
        )
        await _create_project(
            authenticated_client,
            {**PROJECT_PAYLOAD, "title": "No Programme"},
        )

        resp = await authenticated_client.get(f"/pm/projects?programme_id={prog['id']}")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["programme_id"] == prog["id"] for item in items)

    async def test_list_projects_filter_by_origin_project_id(
        self, authenticated_client: AsyncClient
    ):
        # fr#1470: fr finds the PM rows minted for one of its projects by this key.
        origin = str(uuid4())
        await _create_project(
            authenticated_client,
            {
                **PROJECT_PAYLOAD,
                "title": "Minted For Origin",
                "origin_kind": "project",
                "origin_project_id": origin,
            },
        )
        await _create_project(authenticated_client, {**PROJECT_PAYLOAD, "title": "Unrelated"})

        resp = await authenticated_client.get(f"/pm/projects?origin_project_id={origin}")
        assert resp.status_code == 200
        items = resp.json()
        assert [i["title"] for i in items] == ["Minted For Origin"]

        bogus = await authenticated_client.get("/pm/projects?origin_project_id=not-a-uuid")
        assert bogus.status_code == 422

    async def test_update_project_to_closed(self, authenticated_client: AsyncClient):
        # fr#1470: "closed" is the terminal state fr writes when it archives the origin project.
        created = await _create_project(authenticated_client)
        resp = await authenticated_client.put(
            f"/pm/projects/{created['id']}", json={**PROJECT_PAYLOAD, "status": "closed"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"

    async def test_update_project(self, authenticated_client: AsyncClient):
        created = await _create_project(authenticated_client)
        pid = created["id"]

        update = {**PROJECT_PAYLOAD, "title": "Updated Project", "budget": 75000.0}
        resp = await authenticated_client.put(f"/pm/projects/{pid}", json=update)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Updated Project"
        assert data["budget"] == 75000.0

    async def test_delete_project(self, authenticated_client: AsyncClient):
        created = await _create_project(authenticated_client)
        pid = created["id"]

        resp = await authenticated_client.delete(f"/pm/projects/{pid}")
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/pm/projects/{pid}")
        assert resp.status_code == 404

    async def test_invalid_status_returns_422(self, authenticated_client: AsyncClient):
        payload = {**PROJECT_PAYLOAD, "status": "bogus_status"}
        resp = await authenticated_client.post("/pm/projects", json=payload)
        assert resp.status_code == 422

    async def test_tenant_isolation(self, test_client: AsyncClient):
        test_client.headers = {"X-Tenant-Id": T("user-123")}
        p1 = await _create_project(test_client)

        test_client.headers = {"X-Tenant-Id": T("user-789")}
        resp = await test_client.get(f"/pm/projects/{p1['id']}")
        assert resp.status_code == 404


# ===================================================================
# 3. Resource CRUD
# ===================================================================


class TestResourceCRUD:
    async def test_create_resource(self, authenticated_client: AsyncClient):
        data = await _create_resource(authenticated_client)
        assert data["title"] == "Senior Developer"
        assert data["resource_type"] == "person"
        assert data["capacity_per_day"] == 7.5
        assert data["cost_per_unit"] == 120.0
        assert data["skills"] == ["python", "fastapi", "postgresql"]
        assert data["calendar_data"] == {"timezone": "Europe/Amsterdam"}
        assert data["tenant_id"] == T("user-123")
        assert "id" in data
        assert "slug" in data

    async def test_get_resource_by_id(self, authenticated_client: AsyncClient):
        created = await _create_resource(authenticated_client)
        rid = created["id"]

        resp = await authenticated_client.get(f"/pm/resources/{rid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == rid
        assert data["title"] == "Senior Developer"

    async def test_list_resources_with_pagination(self, authenticated_client: AsyncClient):
        await _create_resource(authenticated_client)
        await _create_resource(
            authenticated_client,
            {**RESOURCE_PAYLOAD, "title": "Project Manager", "resource_type": "person"},
        )

        resp = await authenticated_client.get("/pm/resources")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2
        assert "X-Total-Count" in resp.headers
        assert int(resp.headers["X-Total-Count"]) >= 2

    async def test_list_resources_filter_by_type(self, authenticated_client: AsyncClient):
        await _create_resource(authenticated_client)
        await _create_resource(
            authenticated_client,
            {**RESOURCE_PAYLOAD, "title": "Build Server", "resource_type": "equipment"},
        )

        resp = await authenticated_client.get("/pm/resources?resource_type=equipment")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["resource_type"] == "equipment" for item in items)

    async def test_update_resource(self, authenticated_client: AsyncClient):
        created = await _create_resource(authenticated_client)
        rid = created["id"]

        update = {
            **RESOURCE_PAYLOAD,
            "title": "Lead Developer",
            "capacity_per_day": 6.0,
            "skills": ["python", "fastapi", "postgresql", "kubernetes"],
        }
        resp = await authenticated_client.put(f"/pm/resources/{rid}", json=update)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Lead Developer"
        assert data["capacity_per_day"] == 6.0
        assert "kubernetes" in data["skills"]

    async def test_delete_resource(self, authenticated_client: AsyncClient):
        created = await _create_resource(authenticated_client)
        rid = created["id"]

        resp = await authenticated_client.delete(f"/pm/resources/{rid}")
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/pm/resources/{rid}")
        assert resp.status_code == 404

    async def test_invalid_resource_type_returns_422(self, authenticated_client: AsyncClient):
        payload = {**RESOURCE_PAYLOAD, "resource_type": "invalid_type"}
        resp = await authenticated_client.post("/pm/resources", json=payload)
        assert resp.status_code == 422

    async def test_tenant_isolation(self, test_client: AsyncClient):
        test_client.headers = {"X-Tenant-Id": T("user-123")}
        r1 = await _create_resource(test_client)

        test_client.headers = {"X-Tenant-Id": T("user-789")}
        resp = await test_client.get(f"/pm/resources/{r1['id']}")
        assert resp.status_code == 404


# ===================================================================
# 4. Risk CRUD
# ===================================================================


class TestRiskCRUD:
    async def test_create_risk(self, authenticated_client: AsyncClient):
        data = await _create_risk(authenticated_client)
        assert data["title"] == "Database migration failure"
        assert data["project_id"] == "test-project"
        assert data["probability"] == 0.3
        assert data["impact_score"] == 4.0
        assert data["category"] == "technical"
        assert data["response_strategy"] == "mitigate"
        assert data["status"] == "identified"
        assert data["owner"] == "dba-team"
        assert data["tenant_id"] == T("user-123")
        assert "id" in data
        assert "slug" in data

    async def test_create_risk_get_or_create_status_codes(
        self, authenticated_client: AsyncClient, test_session
    ):
        """A project-scoped risk get-or-create returns 201 on first insert and
        200 on a re-POST (same title/project), reusing the same row."""
        from sqlalchemy import func, select

        from models.pm import Risk

        payload = {**RISK_PAYLOAD, "title": "GetOrCreate Status Risk", "project_id": "goc-status"}

        r1 = await authenticated_client.post("/pm/risks", json=payload)
        assert r1.status_code == 201, r1.text
        rid = r1.json()["id"]

        r2 = await authenticated_client.post("/pm/risks", json=payload)
        assert r2.status_code == 200, r2.text
        assert r2.json()["id"] == rid

        count = (
            await test_session.execute(
                select(func.count())
                .select_from(Risk)
                .where(Risk.title == "GetOrCreate Status Risk", Risk.project_id == "goc-status")
            )
        ).scalar_one()
        assert count == 1

    async def test_risk_score_auto_computed(self, authenticated_client: AsyncClient):
        """risk_score should be probability * impact_score when not provided."""
        data = await _create_risk(authenticated_client)
        expected_score = 0.3 * 4.0  # 1.2
        assert abs(data["risk_score"] - expected_score) < 0.001

    async def test_risk_score_custom_value(self, authenticated_client: AsyncClient):
        """When risk_score is explicitly provided, it should be used."""
        payload = {**RISK_PAYLOAD, "title": "Custom Score Risk", "risk_score": 2.5}
        data = await _create_risk(authenticated_client, payload)
        assert abs(data["risk_score"] - 2.5) < 0.001

    async def test_get_risk_by_id(self, authenticated_client: AsyncClient):
        created = await _create_risk(authenticated_client)
        rid = created["id"]

        resp = await authenticated_client.get(f"/pm/risks/{rid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == rid
        assert data["title"] == "Database migration failure"

    async def test_list_risks_with_pagination(self, authenticated_client: AsyncClient):
        await _create_risk(authenticated_client)
        await _create_risk(
            authenticated_client,
            {**RISK_PAYLOAD, "title": "API rate limit risk", "category": "external"},
        )

        resp = await authenticated_client.get("/pm/risks")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2
        assert "X-Total-Count" in resp.headers
        assert int(resp.headers["X-Total-Count"]) >= 2

    async def test_list_risks_filter_by_status(self, authenticated_client: AsyncClient):
        await _create_risk(authenticated_client)
        await _create_risk(
            authenticated_client,
            {**RISK_PAYLOAD, "title": "Closed Risk", "status": "closed"},
        )

        resp = await authenticated_client.get("/pm/risks?status=closed")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["status"] == "closed" for item in items)

    async def test_list_risks_filter_by_category(self, authenticated_client: AsyncClient):
        await _create_risk(authenticated_client)
        await _create_risk(
            authenticated_client,
            {**RISK_PAYLOAD, "title": "Cost Risk", "category": "cost"},
        )

        resp = await authenticated_client.get("/pm/risks?category=cost")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["category"] == "cost" for item in items)

    async def test_list_risks_filter_by_project_id(self, authenticated_client: AsyncClient):
        await _create_risk(authenticated_client)
        await _create_risk(
            authenticated_client,
            {**RISK_PAYLOAD, "title": "Other Project Risk", "project_id": "other-project"},
        )

        resp = await authenticated_client.get("/pm/risks?project_id=test-project")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["project_id"] == "test-project" for item in items)

    async def test_update_risk(self, authenticated_client: AsyncClient):
        created = await _create_risk(authenticated_client)
        rid = created["id"]

        update = {
            **RISK_PAYLOAD,
            "title": "Updated Risk",
            "probability": 0.1,
            "impact_score": 2.0,
            "status": "mitigated",
        }
        resp = await authenticated_client.put(f"/pm/risks/{rid}", json=update)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Updated Risk"
        assert data["probability"] == 0.1
        assert data["impact_score"] == 2.0
        assert data["status"] == "mitigated"
        # risk_score recomputed on update
        assert abs(data["risk_score"] - 0.2) < 0.001

    async def test_delete_risk(self, authenticated_client: AsyncClient):
        created = await _create_risk(authenticated_client)
        rid = created["id"]

        resp = await authenticated_client.delete(f"/pm/risks/{rid}")
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/pm/risks/{rid}")
        assert resp.status_code == 404

    async def test_probability_out_of_range_returns_422(self, authenticated_client: AsyncClient):
        # probability > 1
        payload = {**RISK_PAYLOAD, "probability": 1.5}
        resp = await authenticated_client.post("/pm/risks", json=payload)
        assert resp.status_code == 422

        # probability < 0
        payload2 = {**RISK_PAYLOAD, "title": "Neg prob", "probability": -0.1}
        resp2 = await authenticated_client.post("/pm/risks", json=payload2)
        assert resp2.status_code == 422

    async def test_invalid_category_returns_422(self, authenticated_client: AsyncClient):
        payload = {**RISK_PAYLOAD, "category": "nonexistent_category"}
        resp = await authenticated_client.post("/pm/risks", json=payload)
        assert resp.status_code == 422

    async def test_invalid_response_strategy_returns_422(self, authenticated_client: AsyncClient):
        payload = {**RISK_PAYLOAD, "response_strategy": "ignore_it"}
        resp = await authenticated_client.post("/pm/risks", json=payload)
        assert resp.status_code == 422

    async def test_invalid_status_returns_422(self, authenticated_client: AsyncClient):
        payload = {**RISK_PAYLOAD, "status": "nonexistent_status"}
        resp = await authenticated_client.post("/pm/risks", json=payload)
        assert resp.status_code == 422

    async def test_get_nonexistent_risk(self, authenticated_client: AsyncClient):
        fake_id = str(uuid.uuid4())
        resp = await authenticated_client.get(f"/pm/risks/{fake_id}")
        assert resp.status_code == 404

    async def test_tenant_isolation(self, test_client: AsyncClient):
        test_client.headers = {"X-Tenant-Id": T("user-123")}
        r1 = await _create_risk(test_client)

        test_client.headers = {"X-Tenant-Id": T("user-789")}
        await _create_risk(test_client, {**RISK_PAYLOAD, "title": "Other User Risk"})

        # List as user-789 should not include user-123's risk
        resp = await test_client.get("/pm/risks")
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()]
        assert r1["id"] not in ids

        # Direct GET by user-789 on user-123's risk should 403
        resp = await test_client.get(f"/pm/risks/{r1['id']}")
        assert resp.status_code == 404


# ===================================================================
# 5. Benefit CRUD
# ===================================================================


class TestBenefitCRUD:
    async def test_create_benefit(self, authenticated_client: AsyncClient):
        data = await _create_benefit(authenticated_client)
        assert data["title"] == "Reduced deployment time"
        assert data["benefit_type"] == "non_financial"
        assert data["measurement"] == "Average deployment duration in minutes"
        assert data["baseline_value"] == 120.0
        assert data["target_value"] == 15.0
        assert data["status"] == "expected"
        assert data["owner"] == "platform-team"
        assert data["tenant_id"] == T("user-123")
        assert "id" in data
        assert "slug" in data

    async def test_create_benefit_with_programme_id(self, authenticated_client: AsyncClient):
        prog = await _create_programme(authenticated_client)
        payload = {
            **BENEFIT_PAYLOAD,
            "title": "Programme Benefit",
            "programme_id": prog["id"],
        }
        data = await _create_benefit(authenticated_client, payload)
        assert data["programme_id"] == prog["id"]

    async def test_get_benefit_by_id(self, authenticated_client: AsyncClient):
        created = await _create_benefit(authenticated_client)
        bid = created["id"]

        resp = await authenticated_client.get(f"/pm/benefits/{bid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == bid
        assert data["title"] == "Reduced deployment time"

    async def test_list_benefits_with_pagination(self, authenticated_client: AsyncClient):
        await _create_benefit(authenticated_client)
        await _create_benefit(
            authenticated_client,
            {**BENEFIT_PAYLOAD, "title": "Revenue Increase", "benefit_type": "financial"},
        )

        resp = await authenticated_client.get("/pm/benefits")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2
        assert "X-Total-Count" in resp.headers
        assert int(resp.headers["X-Total-Count"]) >= 2

    async def test_list_benefits_filter_by_status(self, authenticated_client: AsyncClient):
        await _create_benefit(authenticated_client)
        await _create_benefit(
            authenticated_client,
            {**BENEFIT_PAYLOAD, "title": "Realized Benefit", "status": "realized"},
        )

        resp = await authenticated_client.get("/pm/benefits?status=realized")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["status"] == "realized" for item in items)

    async def test_list_benefits_filter_by_benefit_type(self, authenticated_client: AsyncClient):
        await _create_benefit(authenticated_client)
        await _create_benefit(
            authenticated_client,
            {**BENEFIT_PAYLOAD, "title": "Financial Benefit", "benefit_type": "financial"},
        )

        resp = await authenticated_client.get("/pm/benefits?benefit_type=financial")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["benefit_type"] == "financial" for item in items)

    async def test_list_benefits_filter_by_programme_id(self, authenticated_client: AsyncClient):
        prog = await _create_programme(authenticated_client)
        await _create_benefit(
            authenticated_client,
            {
                **BENEFIT_PAYLOAD,
                "title": "In Programme",
                "programme_id": prog["id"],
            },
        )
        await _create_benefit(
            authenticated_client,
            {**BENEFIT_PAYLOAD, "title": "No Programme"},
        )

        resp = await authenticated_client.get(f"/pm/benefits?programme_id={prog['id']}")
        assert resp.status_code == 200
        items = resp.json()
        assert all(item["programme_id"] == prog["id"] for item in items)

    async def test_update_benefit(self, authenticated_client: AsyncClient):
        created = await _create_benefit(authenticated_client)
        bid = created["id"]

        update = {
            **BENEFIT_PAYLOAD,
            "title": "Updated Benefit",
            "actual_value": 18.0,
            "status": "emerging",
        }
        resp = await authenticated_client.put(f"/pm/benefits/{bid}", json=update)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Updated Benefit"
        assert data["actual_value"] == 18.0
        assert data["status"] == "emerging"

    async def test_delete_benefit(self, authenticated_client: AsyncClient):
        created = await _create_benefit(authenticated_client)
        bid = created["id"]

        resp = await authenticated_client.delete(f"/pm/benefits/{bid}")
        assert resp.status_code == 200

        resp = await authenticated_client.get(f"/pm/benefits/{bid}")
        assert resp.status_code == 404

    async def test_invalid_benefit_type_returns_422(self, authenticated_client: AsyncClient):
        payload = {**BENEFIT_PAYLOAD, "benefit_type": "nonexistent_type"}
        resp = await authenticated_client.post("/pm/benefits", json=payload)
        assert resp.status_code == 422

    async def test_invalid_status_returns_422(self, authenticated_client: AsyncClient):
        payload = {**BENEFIT_PAYLOAD, "status": "nonexistent_status"}
        resp = await authenticated_client.post("/pm/benefits", json=payload)
        assert resp.status_code == 422

    async def test_get_nonexistent_benefit(self, authenticated_client: AsyncClient):
        fake_id = str(uuid.uuid4())
        resp = await authenticated_client.get(f"/pm/benefits/{fake_id}")
        assert resp.status_code == 404

    async def test_tenant_isolation(self, test_client: AsyncClient):
        test_client.headers = {"X-Tenant-Id": T("user-123")}
        b1 = await _create_benefit(test_client)

        test_client.headers = {"X-Tenant-Id": T("user-789")}
        await _create_benefit(test_client, {**BENEFIT_PAYLOAD, "title": "Other User Benefit"})

        # List as user-789 should not include user-123's benefit
        resp = await test_client.get("/pm/benefits")
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()]
        assert b1["id"] not in ids

        # Direct GET by user-789 on user-123's benefit should 403
        resp = await test_client.get(f"/pm/benefits/{b1['id']}")
        assert resp.status_code == 404
