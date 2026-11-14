"""Architecture containers enumeration (P4.5) + validator (task #25 PR 1).

Surfaces the distinct container names reachable from a project's *effective*
architecture (project-scope rows ∪ tenant-scope rows the project inherits),
tagged by which scope contributed them and how many Artifact elements point
at each.

Two read shapes:

* ``GET /architecture/containers?projectId=…`` — list (existing P4.5 route).
  Backed by ``aggregate_containers_for_tenant`` which reads the structured
  ``architecture_element_realisation_bindings`` table.

* ``GET /architecture/containers/{container_name}?projectId=…`` —
  per-container validator (task #25). Returns ``200`` with
  ``{name, scope, element_count, valid: true}`` when the container is
  reachable; ``404`` when it isn't. fr's project↔repo provisioning flow
  uses this on attach (soft-fail) and primary-promote (hard-fail).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from db import get_session
from routers.responses import AUTH_RESPONSES, ErrorEnvelopeRoute
from schemas.architecture import ArchitectureContainerDetail
from schemas.common import ErrorResponse
from services.realisation_bindings import (
    aggregate_containers_for_tenant,
    container_detail,
)
from tools.architecture.conformance import ARTIFACT_TYPE

router = APIRouter(route_class=ErrorEnvelopeRoute)


@router.get(
    "",
    operation_id="list_architecture_containers",
    responses=AUTH_RESPONSES,
)
async def list_containers(
    project_id: str = Query(
        ...,
        alias="projectId",
        min_length=1,
        description=(
            "requirements_management project whose effective view "
            "(project-scope ∪ tenant-scope) to enumerate"
        ),
    ),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Return distinct containers bound to this project's effective Artifacts.

    Response shape::

        {
          "containers": [
            {"name": "fr",                "scope": "tenant",  "element_count": 4},
            {"name": "requirements_mgmt", "scope": "project", "element_count": 2},
            {"name": "coding-ai",         "scope": "mixed",   "element_count": 6}
          ],
          "total": 3
        }

    ``scope = "tenant"`` when every contributing element has ``project_id IS NULL``,
    ``"project"`` when every contributor has ``project_id = <pid>``, and
    ``"mixed"`` when both are present. The frontend uses this to label options
    so operators know which assets they're filtering against.
    """
    per_container = await aggregate_containers_for_tenant(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        element_type=ARTIFACT_TYPE,
    )

    containers = [
        {
            "name": name,
            "scope": _scope_label(
                has_tenant=bool(bucket["has_tenant_scope"]),
                has_project=bool(bucket["has_project_scope"]),
            ),
            "element_count": int(bucket["element_count"]),
        }
        for name, bucket in per_container.items()
    ]
    # Sort tenant-only first (most likely to be the default pick), then project,
    # then mixed; alphabetical inside each scope keeps it stable in the UI.
    containers.sort(key=lambda c: (_scope_sort_key(str(c["scope"])), str(c["name"])))

    return {"containers": containers, "total": len(containers)}


@router.get(
    "/{container_name}",
    response_model=ArchitectureContainerDetail,
    operation_id="get_architecture_container",
    responses={
        **AUTH_RESPONSES,
        404: {
            "description": "Container is not bound to any element in the project's effective view",
            "model": ErrorResponse,
        },
    },
)
async def get_container(
    container_name: str = Path(
        ...,
        min_length=1,
        max_length=255,
        description="Container name to validate",
    ),
    project_id: str = Query(
        ...,
        alias="projectId",
        min_length=1,
        description="Project whose effective view the container must reach",
    ),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ArchitectureContainerDetail:
    """Validate that ``container_name`` is reachable from this project.

    Returns ``200`` with the same per-container payload shape as the list
    route plus ``valid: true``. Returns ``404`` when no Artifact element in
    the project's effective view (project ∪ tenant) is bound to the
    container — fr treats this as ``container_not_bound_to_project`` when
    promoting a repo to primary, and as a soft warning on attach.
    """
    detail = await container_detail(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        container_name=container_name,
        element_type=ARTIFACT_TYPE,
    )
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Container {container_name!r} not bound to any element in project {project_id!r}"
            ),
        )
    return ArchitectureContainerDetail(**detail)


def _scope_label(*, has_tenant: bool, has_project: bool) -> str:
    if has_tenant and has_project:
        return "mixed"
    if has_tenant:
        return "tenant"
    return "project"


def _scope_sort_key(scope: str) -> int:
    return {"tenant": 0, "project": 1, "mixed": 2}.get(scope, 3)
