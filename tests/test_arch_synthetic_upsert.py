"""Tests for the synthetic-Artifact upsert path on ``POST /architecture/elements``.

When ``is_synthetic=true`` AND ``properties.code_binding.{container,
component_id}`` are both present, the POST becomes an upsert keyed on
``(tenant_id, project_id, code_binding.container,
code_binding.component_id)``. This is the contract the fr Architecture
Governance review queue (P4.4) relies on so re-accepting the same proposal
does not create duplicate Artifact rows. See
``docs/ARCHITECTURE_PROPOSALS_CONTRACT.md``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from models.architecture import ArchitectureElement, ArchitectureElementRealisationBinding
from schemas.architecture import ArchitectureElementIn
from services.architecture_upsert import code_binding_upsert_key
from tests.tenants import T


def _artifact_payload(
    *,
    title: str = "fr/fr.frontend.composer",
    container: str = "fr",
    component_id: str = "fr.frontend.composer",
    project_id: str | None = "test-project",
    extra_properties: dict | None = None,
    is_synthetic: bool = True,
) -> dict:
    properties: dict = {
        "code_binding": {
            "container": container,
            "component_id": component_id,
            "path": "frontend/src/lib/composer.ts",
            "kind": "service",
        }
    }
    if extra_properties:
        properties.update(extra_properties)
    payload = {
        "title": title,
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": is_synthetic,
        "properties": properties,
    }
    if project_id is not None:
        payload["project_id"] = project_id
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper — no DB needed
# ─────────────────────────────────────────────────────────────────────────────


class TestCodeBindingUpsertKey:
    """``code_binding_upsert_key`` returns the upsert key only when the payload qualifies."""

    def test_returns_key_when_synthetic_and_binding_complete(self):
        payload = ArchitectureElementIn.model_validate(_artifact_payload())
        assert code_binding_upsert_key(payload) == ("fr", "fr.frontend.composer")

    def test_returns_none_when_not_synthetic(self):
        payload = ArchitectureElementIn.model_validate(_artifact_payload(is_synthetic=False))
        assert code_binding_upsert_key(payload) is None

    def test_returns_none_when_no_properties(self):
        payload = ArchitectureElementIn.model_validate(
            {
                "title": "x",
                "layer": "technology",
                "element_type": "artifact",
                "is_synthetic": True,
            }
        )
        assert code_binding_upsert_key(payload) is None

    def test_returns_none_when_code_binding_missing_container(self):
        payload = ArchitectureElementIn.model_validate(
            {
                "title": "x",
                "layer": "technology",
                "element_type": "artifact",
                "is_synthetic": True,
                "properties": {"code_binding": {"component_id": "x.y"}},
            }
        )
        assert code_binding_upsert_key(payload) is None

    def test_returns_none_when_code_binding_missing_component_id(self):
        payload = ArchitectureElementIn.model_validate(
            {
                "title": "x",
                "layer": "technology",
                "element_type": "artifact",
                "is_synthetic": True,
                "properties": {"code_binding": {"container": "fr"}},
            }
        )
        assert code_binding_upsert_key(payload) is None

    def test_returns_none_when_component_id_is_empty_string(self):
        payload = ArchitectureElementIn.model_validate(_artifact_payload(component_id=""))
        assert code_binding_upsert_key(payload) is None


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end via the router — exercises the SQL JSONB filter
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestArchitectureElementUpsert:
    async def test_first_post_returns_201_and_inserts(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post("/architecture/elements", json=_artifact_payload())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["is_synthetic"] is True
        # The request's ``code_binding`` block is stripped from the persisted
        # JSONB; the binding lives in the structured table, exposed via the
        # /bindings sub-route.
        assert "code_binding" not in (body.get("properties") or {})
        bindings = await authenticated_client.get(f"/architecture/elements/{body['id']}/bindings")
        assert bindings.status_code == 200, bindings.text
        rows = bindings.json()["bindings"]
        assert len(rows) == 1
        assert rows[0]["container"] == "fr"
        assert rows[0]["component_id"] == "fr.frontend.composer"

    async def test_second_post_returns_200_and_does_not_duplicate(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        first = await authenticated_client.post("/architecture/elements", json=_artifact_payload())
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]

        # Re-POST identical body; should upsert into the same row.
        second_payload = _artifact_payload(title="fr/fr.frontend.composer (updated)")
        second = await authenticated_client.post("/architecture/elements", json=second_payload)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first_id
        assert second.json()["title"] == "fr/fr.frontend.composer (updated)"

        # Verify exactly one row exists for the binding key in this tenant
        # via the structured ``architecture_element_realisation_bindings``
        # table (the canonical store post-T3.2; the JSONB ``code_binding`` is
        # stripped from ``properties`` before persistence).
        async with test_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ArchitectureElement)
                        .join(
                            ArchitectureElementRealisationBinding,
                            ArchitectureElementRealisationBinding.element_id
                            == ArchitectureElement.id,
                        )
                        .where(
                            ArchitectureElement.tenant_id == T("user-123"),
                            ArchitectureElement.is_synthetic.is_(True),
                            ArchitectureElementRealisationBinding.binding_kind
                            == "coding_component",
                            ArchitectureElementRealisationBinding.container == "fr",
                            ArchitectureElementRealisationBinding.component_id
                            == "fr.frontend.composer",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1

    async def test_different_project_ids_create_separate_rows(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        a = await authenticated_client.post(
            "/architecture/elements",
            json=_artifact_payload(project_id="project-alpha"),
        )
        b = await authenticated_client.post(
            "/architecture/elements",
            json=_artifact_payload(project_id="project-beta"),
        )
        assert a.status_code == 201
        assert b.status_code == 201
        assert a.json()["id"] != b.json()["id"]

    async def test_non_synthetic_post_is_a_plain_create(self, authenticated_client: AsyncClient):
        """A12: a non-synthetic POST never engages the code_binding upsert and has no
        title identity — each POST is a plain create (200) with its own id."""
        payload = _artifact_payload(is_synthetic=False)
        first = await authenticated_client.post("/architecture/elements", json=payload)
        assert first.status_code == 200, first.text
        second = await authenticated_client.post("/architecture/elements", json=payload)
        assert second.status_code == 200, second.text
        assert second.json()["id"] != first.json()["id"]

    async def test_synthetic_without_code_binding_is_a_plain_create(
        self, authenticated_client: AsyncClient
    ):
        """``is_synthetic=true`` without a complete ``code_binding`` has no upsert key,
        so (A12) it is a plain create: a re-POST is a second row. Retry safety is the
        ``Idempotency-Key`` header's job, not the title's."""
        payload = {
            "title": "Generated scaffolding (no binding)",
            "layer": "technology",
            "element_type": "artifact",
            "is_synthetic": True,
            "properties": {"note": "no code_binding here"},
            "project_id": "test-project",
        }
        first = await authenticated_client.post("/architecture/elements", json=payload)
        second = await authenticated_client.post("/architecture/elements", json=payload)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]
        key = {"Idempotency-Key": "scaffold-1"}
        third = await authenticated_client.post("/architecture/elements", json=payload, headers=key)
        fourth = await authenticated_client.post(
            "/architecture/elements", json=payload, headers=key
        )
        assert (third.status_code, fourth.status_code) == (200, 200)
        assert third.json() == fourth.json()


@pytest.mark.asyncio
class TestArchitectureElementIdIdentity:
    """A12 (replaces the WS3-B title get-or-create): the non-code_binding create path
    is a plain create — elements are identified by id, two may share
    ``(tenant_id, project_id, title, element_type)``. A retry is made safe with the
    ``Idempotency-Key`` header (``tests/test_arch_idempotency_key.py``)."""

    @staticmethod
    def _plain_payload(*, title: str = "Payments Capability") -> dict:
        # No ``code_binding`` in properties → exercises the plain-create branch.
        return {
            "title": title,
            "description": "core payments",
            "layer": "strategy",
            "element_type": "capability",
            "status": "draft",
            "project_id": "ws3b-project",
        }

    async def test_repost_same_title_creates_a_second_row_and_keeps_the_first(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        payload = self._plain_payload()
        first = await authenticated_client.post("/architecture/elements", json=payload)
        assert first.status_code == 200, first.text
        first_id = first.json()["id"]

        revised = self._plain_payload()
        revised["description"] = "core payments (revised)"
        second = await authenticated_client.post("/architecture/elements", json=revised)
        assert second.status_code == 200, second.text
        assert second.json()["id"] != first_id
        # The first concept is NOT overwritten (the A12 defect).
        got = await authenticated_client.get(f"/architecture/elements/{first_id}")
        assert got.json()["description"] == "core payments"

        async with test_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ArchitectureElement).where(
                            ArchitectureElement.tenant_id == T("user-123"),
                            ArchitectureElement.title == "Payments Capability",
                            ArchitectureElement.element_type == "capability",
                            ArchitectureElement.project_id == "ws3b-project",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2

    async def test_different_title_creates_distinct_row(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        a = await authenticated_client.post(
            "/architecture/elements", json=self._plain_payload(title="Alpha Capability")
        )
        b = await authenticated_client.post(
            "/architecture/elements", json=self._plain_payload(title="Beta Capability")
        )
        assert a.status_code == 200, a.text
        assert b.status_code == 200, b.text
        assert a.json()["id"] != b.json()["id"]

        async with test_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ArchitectureElement).where(
                            ArchitectureElement.tenant_id == T("user-123"),
                            ArchitectureElement.element_type == "capability",
                            ArchitectureElement.project_id == "ws3b-project",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2
