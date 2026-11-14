"""Integration tests for ``GET /architecture/containers`` (P4.5).

Covers the container enumeration the fr queue panel's container ``<Select>``
consumes, plus the regression-lock proving the ``_load_elements`` /
``_load_relationships`` scope-model fix (tenant-scope rows included).

Seeds Artifact elements directly so we exercise the JSONB ``code_binding``
aggregation. Authenticated via ``authenticated_client`` (a plain ``X-Tenant-Id``
becomes a test licence, see ``tests/conftest.py``).
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.tenants import T

_TENANT = T("user-123")  # matches conftest's authenticated_client default
_PROJECT = "p4-5-containers-test"
_OTHER_PROJECT = "p4-5-other-project"


# ─────────────────────────────────────────────────────────────────────────────
# Seed helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _seed_artifact(
    client: AsyncClient,
    *,
    title: str,
    container: str,
    project_id: str | None,
) -> dict:
    """Seed one synthetic Artifact bound to ``container``.

    ``project_id=None`` produces a tenant-scope asset (the case the new endpoint
    must surface as ``scope="tenant"``).
    """
    body: dict = {
        "title": title,
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "properties": {
            "code_binding": {
                "container": container,
                "component_id": f"{container}.{title.lower().replace(' ', '_')}",
                "path": f"src/{title.lower().replace(' ', '_')}.py",
                "kind": "service",
            }
        },
    }
    if project_id is not None:
        body["project_id"] = project_id
    resp = await client.post("/architecture/elements", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _seed_app_component(
    client: AsyncClient,
    *,
    title: str,
    project_id: str | None,
) -> dict:
    """Seed one ApplicationComponent (no code_binding — must NOT appear in container list)."""
    body: dict = {
        "title": title,
        "layer": "application",
        "element_type": "application_component",
        "architecture_state": "baseline",
    }
    if project_id is not None:
        body["project_id"] = project_id
    resp = await client.post("/architecture/elements", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_containers_requires_project_id(authenticated_client: AsyncClient) -> None:
    """``projectId`` is mandatory."""
    resp = await authenticated_client.get("/architecture/containers")
    assert resp.status_code == 422, resp.text


async def test_containers_empty_project(authenticated_client: AsyncClient) -> None:
    """Project with no Artifacts → empty list, total 0."""
    resp = await authenticated_client.get(
        "/architecture/containers",
        params={"projectId": "empty-project-no-artifacts"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"containers": [], "total": 0}


async def test_containers_project_scope_only(authenticated_client: AsyncClient) -> None:
    """Single project-scope Artifact → one entry tagged ``scope="project"``."""
    await _seed_artifact(
        authenticated_client,
        title="ProjOnly Svc",
        container="project-only-repo",
        project_id=_PROJECT,
    )

    resp = await authenticated_client.get(
        "/architecture/containers",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    matching = [c for c in payload["containers"] if c["name"] == "project-only-repo"]
    assert len(matching) == 1
    assert matching[0] == {"name": "project-only-repo", "scope": "project", "element_count": 1}


async def test_containers_tenant_scope_visible_from_project_view(
    authenticated_client: AsyncClient,
) -> None:
    """Tenant-scope Artifact (project_id IS NULL) MUST surface in a project's view.

    This is the user's stated scope model — tenant assets reusable across all
    projects — and the bug the P4.5 loader fix corrects.
    """
    await _seed_artifact(
        authenticated_client,
        title="Tenant Svc",
        container="tenant-shared-repo",
        project_id=None,
    )

    resp = await authenticated_client.get(
        "/architecture/containers",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    matching = [c for c in resp.json()["containers"] if c["name"] == "tenant-shared-repo"]
    assert len(matching) == 1
    assert matching[0] == {"name": "tenant-shared-repo", "scope": "tenant", "element_count": 1}


async def test_containers_mixed_scope_aggregates(authenticated_client: AsyncClient) -> None:
    """Same container name in BOTH scopes → single entry tagged ``"mixed"``, count summed."""
    await _seed_artifact(
        authenticated_client,
        title="Mixed Tenant Part",
        container="mixed-repo",
        project_id=None,
    )
    await _seed_artifact(
        authenticated_client,
        title="Mixed Project Part",
        container="mixed-repo",
        project_id=_PROJECT,
    )

    resp = await authenticated_client.get(
        "/architecture/containers",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    matching = [c for c in resp.json()["containers"] if c["name"] == "mixed-repo"]
    assert len(matching) == 1
    assert matching[0] == {"name": "mixed-repo", "scope": "mixed", "element_count": 2}


async def test_containers_excludes_other_projects(authenticated_client: AsyncClient) -> None:
    """Artifacts of a DIFFERENT project must NOT appear in this project's view."""
    await _seed_artifact(
        authenticated_client,
        title="Other Project Svc",
        container="other-project-repo",
        project_id=_OTHER_PROJECT,
    )

    resp = await authenticated_client.get(
        "/architecture/containers",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    names = {c["name"] for c in resp.json()["containers"]}
    assert "other-project-repo" not in names


async def test_containers_excludes_non_artifact_elements(
    authenticated_client: AsyncClient,
) -> None:
    """ApplicationComponents (no code_binding) must NOT contribute to the list."""
    await _seed_app_component(
        authenticated_client,
        title="Not An Artifact",
        project_id=_PROJECT,
    )

    resp = await authenticated_client.get(
        "/architecture/containers",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    # No artifact means no container at all for this project.
    assert resp.json() == {"containers": [], "total": 0}


# ─────────────────────────────────────────────────────────────────────────────
# GET /architecture/containers/{container_name} — the fr validator route
# ─────────────────────────────────────────────────────────────────────────────


async def test_container_validator_returns_404_for_unknown(
    authenticated_client: AsyncClient,
) -> None:
    """No binding under this project → ``404 container_not_bound_to_project``.

    This is the shape fr's project↔repo flow surfaces as a soft warning on
    attach and as a hard ``409`` on promote-to-primary.
    """
    resp = await authenticated_client.get(
        "/architecture/containers/no-such-container",
        params={"projectId": "validator-empty-project"},
    )
    assert resp.status_code == 404, resp.text


async def test_container_validator_tenant_scope_visible_from_project(
    authenticated_client: AsyncClient,
) -> None:
    """Tenant-scope binding must validate from a child project's view."""
    await _seed_artifact(
        authenticated_client,
        title="Validator Tenant Svc",
        container="validator-tenant-repo",
        project_id=None,
    )

    resp = await authenticated_client.get(
        "/architecture/containers/validator-tenant-repo",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "validator-tenant-repo"
    assert body["scope"] == "tenant"
    assert body["element_count"] >= 1
    assert body["valid"] is True


async def test_container_validator_project_scope(
    authenticated_client: AsyncClient,
) -> None:
    """Project-scope binding validates with ``scope="project"``."""
    await _seed_artifact(
        authenticated_client,
        title="Validator Proj Svc",
        container="validator-project-repo",
        project_id=_PROJECT,
    )

    resp = await authenticated_client.get(
        "/architecture/containers/validator-project-repo",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "project"
    assert body["valid"] is True


async def test_container_validator_mixed_scope(authenticated_client: AsyncClient) -> None:
    """Same container in BOTH scopes → ``scope="mixed"``, count summed."""
    await _seed_artifact(
        authenticated_client,
        title="Validator Mixed Tenant Part",
        container="validator-mixed-repo",
        project_id=None,
    )
    await _seed_artifact(
        authenticated_client,
        title="Validator Mixed Project Part",
        container="validator-mixed-repo",
        project_id=_PROJECT,
    )

    resp = await authenticated_client.get(
        "/architecture/containers/validator-mixed-repo",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "mixed"
    assert body["element_count"] == 2


async def test_container_validator_isolates_other_project(
    authenticated_client: AsyncClient,
) -> None:
    """A container bound only under a different project must 404 here."""
    await _seed_artifact(
        authenticated_client,
        title="Validator OtherProj Svc",
        container="validator-otherproject-repo",
        project_id=_OTHER_PROJECT,
    )

    resp = await authenticated_client.get(
        "/architecture/containers/validator-otherproject-repo",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 404, resp.text


# ─────────────────────────────────────────────────────────────────────────────
# Sort-order test (existing, kept at the bottom)
# ─────────────────────────────────────────────────────────────────────────────


async def test_containers_sort_order_tenant_first_then_project_then_mixed(
    authenticated_client: AsyncClient,
) -> None:
    """``tenant`` < ``project`` < ``mixed`` in sort order; alphabetical within scope."""
    # Two tenant, two project, one mixed
    await _seed_artifact(
        authenticated_client,
        title="zz-tenant",
        container="zz-tenant-repo",
        project_id=None,
    )
    await _seed_artifact(
        authenticated_client,
        title="aa-tenant",
        container="aa-tenant-repo",
        project_id=None,
    )
    await _seed_artifact(
        authenticated_client,
        title="bb-project",
        container="bb-project-repo",
        project_id=_PROJECT,
    )
    await _seed_artifact(
        authenticated_client,
        title="cc-mixed-tenant",
        container="cc-mixed-repo",
        project_id=None,
    )
    await _seed_artifact(
        authenticated_client,
        title="cc-mixed-project",
        container="cc-mixed-repo",
        project_id=_PROJECT,
    )

    resp = await authenticated_client.get(
        "/architecture/containers",
        params={"projectId": _PROJECT},
    )
    assert resp.status_code == 200, resp.text
    ordering = [(c["scope"], c["name"]) for c in resp.json()["containers"]]
    # Tenant rows alphabetised, then project rows, then mixed rows.
    expected_prefix = [
        ("tenant", "aa-tenant-repo"),
        ("tenant", "zz-tenant-repo"),
        ("project", "bb-project-repo"),
        ("mixed", "cc-mixed-repo"),
    ]
    assert ordering == expected_prefix
