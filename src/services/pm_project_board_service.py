"""The project ↔ Planka board link (George 2026-10-13, v4 R73 / D20).

* ``link`` — ``PUT /pm/projects/{eid}/planka-board``. fr writes it once, right after it
  builds the board from RM's board template: linking the board completes the request, so a
  fr-delegated caller MAY write it while the project has no board, and may re-send the same
  link (idempotent, ``changed: false``). Once linked, a DIFFERENT link from fr is 403
  ``project_writes_are_pm_only`` (v4 ruling 1: after the request only PM writes); PM (the
  conveyor) may re-link. A board already linked to another project OF THE SAME TENANT is 409
  ``planka_board_in_use`` (``uq_pm_projects_tenant_planka_board_id`` stands behind the check).
  Planka is per tenant (each tenant brings its own instance), so board ids are only unique
  within a tenant: a board id linked in another tenant never affects this result — neither
  blocking the link nor revealing, by a 409, that the id is linked elsewhere.
* ``lookup`` — ``GET /pm/projects/by-planka-board/{planka_board_id}``: from a Planka webhook's
  board id to everything n8n needs (tenant, fr project id, type, phase, status, WIP limit,
  list ids, repository url). Tenant-scoped like every other project read (own tenant only,
  never system-widened); another tenant's board is a 404, and the per-tenant UNIQUE means at
  most one row can match.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy.exc
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.pm_only import project_writes_are_pm_only_error
from models.pm import Project
from models.pm_catalogue import ProjectType
from repositories.visibility import scope
from schemas.pm import ProjectOut
from schemas.pm_board_link import (
    CONVEYOR_LISTS,
    ProjectBoardLinkIn,
    ProjectBoardLinkOut,
    ProjectBoardOut,
)
from services.entity_service import _resolve_subject

BOARD_IN_USE = "planka_board_in_use"


def _board_in_use(board_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": BOARD_IN_USE,
            "message": (
                f"{BOARD_IN_USE}: Planka board {board_id} is already linked to another "
                "project of this tenant"
            ),
        },
    )


def _primary_repository(repositories: list[Any] | None) -> Any | None:
    """The primary repository, else the first one; None when there is none."""
    if not repositories:
        return None
    return next((r for r in repositories if r.primary), repositories[0])


async def _type_name(session: AsyncSession, project_type_id: UUID | None) -> str | None:
    if project_type_id is None:
        return None
    return (
        await session.execute(select(ProjectType.name).where(ProjectType.id == project_type_id))
    ).scalar_one_or_none()


async def board_out(session: AsyncSession, out: ProjectOut, project: Project) -> dict[str, Any]:
    """The ``ProjectBoardOut`` fields for one project (``out`` carries the derived fields)."""
    lists = dict(project.planka_list_ids) if project.planka_list_ids else None
    repo = _primary_repository(out.repositories)
    return {
        "project_id": out.id,
        "tenant_id": out.tenant_id,
        "fr_project_id": out.origin_project_id,
        "title": out.title,
        "status": out.status,
        "project_type_id": out.project_type_id,
        "project_type": await _type_name(session, out.project_type_id),
        "current_phase_id": out.current_phase_id,
        "current_phase": out.current_phase,
        "wip_limit": out.wip_limit,
        "effective_wip_limit": out.effective_wip_limit,
        "wip_limit_source": out.wip_limit_source,
        "repository_url": repo.url if repo else None,
        "repository_branch": repo.branch if repo else None,
        "repository_sha": repo.sha if repo else None,
        "repositories": out.repositories,
        "planka_board_id": project.planka_board_id,
        "planka_project_id": project.planka_project_id,
        "planka_list_ids": lists,
        "lists_by_id": {v: k for k, v in lists.items()} if lists else None,
        "planka_linked_at": project.planka_linked_at,
    }


async def link(
    service: Any,
    session: AsyncSession,
    eid: UUID,
    body: ProjectBoardLinkIn,
    tenant_id: str,
    subject_id: str,
    *,
    delegated: bool,
) -> ProjectBoardLinkOut:
    project = await service._get_mutable(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    # Serialise concurrent link writes on this project.
    await session.refresh(project, with_for_update=True)
    wanted_lists = {name: getattr(body.planka_list_ids, name) for name in CONVEYOR_LISTS}
    wanted = (body.planka_board_id, body.planka_project_id, wanted_lists)
    stored = (project.planka_board_id, project.planka_project_id, project.planka_list_ids)

    changed = stored != wanted
    if changed:
        if delegated and project.planka_board_id is not None:
            # The request is complete once the board is linked; a re-link is PM's.
            raise project_writes_are_pm_only_error()
        # Scoped to the project's (= the caller's) tenant: another tenant's Planka is another
        # id space, so its links must neither block this one nor be observable from here.
        other = (
            await session.execute(
                select(Project.id).where(
                    Project.tenant_id == project.tenant_id,
                    Project.planka_board_id == body.planka_board_id,
                    Project.id != project.id,
                )
            )
        ).first()
        if other is not None:
            raise _board_in_use(body.planka_board_id)
        project.planka_board_id = body.planka_board_id
        project.planka_project_id = body.planka_project_id
        project.planka_list_ids = wanted_lists
        project.planka_linked_at = datetime.now(UTC)
        try:
            await session.commit()
        except sqlalchemy.exc.IntegrityError as exc:
            # The per-tenant unique constraint: a racing link in this tenant won.
            await session.rollback()
            raise _board_in_use(body.planka_board_id) from exc

    out = await service.get(session, eid, tenant_id, subject_id)
    project = await service._get_visible(session, eid, tenant_id=tenant_id, subject_id=subject_id)
    return ProjectBoardLinkOut(**await board_out(session, out, project), changed=changed)


async def lookup(
    service: Any,
    session: AsyncSession,
    planka_board_id: str,
    tenant_id: str,
    subject_id: str,
) -> ProjectBoardOut:
    # Own tenant only (no system widening): with UNIQUE (tenant_id, planka_board_id) at most
    # one row can match, so scalar_one_or_none cannot see a second tenant's link.
    stmt = scope(
        select(Project).where(Project.planka_board_id == planka_board_id),
        Project,
        tenant_id=tenant_id,
        subject_id=_resolve_subject(tenant_id, subject_id),
        include_system=False,
    )
    project = (await session.execute(stmt)).scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "planka_board_not_linked",
                "message": f"no project of this tenant is linked to Planka board {planka_board_id}",
            },
        )
    out = await service.get(session, project.id, tenant_id, subject_id)
    return ProjectBoardOut(**await board_out(session, out, project))
