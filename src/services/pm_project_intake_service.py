"""The fr project intake on ``pm_projects`` (George 2026-10-13) — the rules ``ProjectService``
applies on create and update.

* ``priority`` (a level NAME, case-insensitive) / ``priority_level_id`` resolve to one of the
  CALLER tenant's ``pm_priority_levels``; anything else is 422. Another tenant's level never
  counts.
* ``shape`` is immutable once set (409).
* Omitted intake fields keep their stored value on update; an explicit null clears.
* ``intake_updated_at`` is stamped whenever a stored intake value really changes (or a brief
  version is appended), never on a no-op.
* ``redact_foreign``: a project read by a tenant that does not own it (a system-published
  project inherited by a platform tenant) carries NO intake — every intake field is null,
  consistent with its ``/brief/versions`` being 404 to that tenant. The intake is what one
  tenant's user typed (repositories, secret ids, brief); sharing the project row does not
  share it.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.pm import Project
from models.pm_catalogue import PriorityLevel
from repositories.visibility import same_tenant
from schemas.pm import INTAKE_COLUMNS
from schemas.pm_intake import ItInputs, ProjectBrief, ProjectRepository, rewrite_violation

# Every intake field on ``ProjectOut`` — nulled for a reader outside the owning tenant.
INTAKE_OUT_FIELDS: tuple[str, ...] = (
    *INTAKE_COLUMNS,
    "priority",
    "brief",
    "brief_version",
    "brief_author_kind",
    "brief_updated_at",
    "intake_updated_at",
)


def redact_foreign(tenant_id: str, items: list[Any]) -> None:
    """Null every intake field of each ``ProjectOut`` the caller's tenant does not own."""
    for out in items:
        if same_tenant(out.tenant_id, tenant_id):
            continue
        for field in INTAKE_OUT_FIELDS:
            setattr(out, field, None)


async def resolve_priority(
    session: AsyncSession, payload: Any, tenant_id: str
) -> tuple[bool, UUID | None]:
    """``(given, priority_level_id)`` from ``priority`` and/or ``priority_level_id``."""
    fields = payload.model_fields_set
    by_name, by_id = "priority" in fields, "priority_level_id" in fields
    if not (by_name or by_id):
        return False, None
    named: UUID | None = None
    if by_name and payload.priority is not None:
        named = (
            await session.execute(
                select(PriorityLevel.id).where(
                    PriorityLevel.tenant_id == tenant_id,
                    func.lower(PriorityLevel.name) == payload.priority,
                )
            )
        ).scalar_one_or_none()
        if named is None:
            names = (
                (
                    await session.execute(
                        select(PriorityLevel.name)
                        .where(PriorityLevel.tenant_id == tenant_id)
                        .order_by(PriorityLevel.rank)
                    )
                )
                .scalars()
                .all()
            )
            raise HTTPException(
                status_code=422,
                detail=f"unknown priority {payload.priority!r}; this tenant's levels: {names}",
            )
    if by_id and payload.priority_level_id is not None:
        owned = (
            await session.execute(
                select(PriorityLevel.id).where(
                    PriorityLevel.id == payload.priority_level_id,
                    PriorityLevel.tenant_id == tenant_id,
                )
            )
        ).first()
        if owned is None:
            raise HTTPException(
                status_code=422, detail=f"unknown priority_level_id {payload.priority_level_id}"
            )
    if by_name and by_id and named != payload.priority_level_id:
        raise HTTPException(
            status_code=422, detail="priority and priority_level_id name different levels"
        )
    return True, named if by_name else payload.priority_level_id


def intake_given(payload: Any) -> bool:
    fields = payload.model_fields_set
    return bool(fields & {*INTAKE_COLUMNS, "priority", "brief"})


def assert_shape_unchanged(project: Project, payload: Any) -> None:
    if "shape" not in payload.model_fields_set or project.shape is None:
        return
    if payload.shape != project.shape:
        raise HTTPException(
            status_code=409,
            detail=f"shape is immutable once set (stored {project.shape!r}, got {payload.shape!r})",
        )


def assert_rewrite_shape(payload: Any, project: Project | None = None) -> None:
    """422 when the project, as it will be stored, is a malformed ``it_flow: rewrite``.

    Checked on the MERGED result: on update an omitted ``repositories`` / ``it_inputs`` /
    ``brief`` keeps the stored value (``project``), so a PUT of one part is judged against the
    others. Projects whose ``it_flow`` is not ``rewrite`` are never refused here.
    """
    fields = payload.model_fields_set
    if project is None or "repositories" in fields:
        repos = payload.repositories
    else:
        stored = project.repositories
        repos = [ProjectRepository.model_validate(r) for r in stored] if stored else None
    if project is None or "it_inputs" in fields:
        it_inputs = payload.it_inputs
    else:
        stored_it = project.it_inputs
        it_inputs = ItInputs.model_validate(stored_it) if stored_it else None
    if project is None or "brief" in fields:
        brief = payload.brief
    else:
        # The stored brief snapshot (the current version), re-validated like the other halves.
        stored_brief = project.brief
        brief = ProjectBrief.model_validate(stored_brief) if stored_brief else None
    violation = rewrite_violation(repos, it_inputs, brief)
    if violation is not None:
        code, message = violation
        raise HTTPException(status_code=422, detail={"code": code, "message": message})


def _norm(value: Any) -> Any:
    return str(value) if isinstance(value, UUID) else value


def intake_changed(project: Project, payload: Any, keep: frozenset[str]) -> bool:
    """True when a written intake column's new value differs from the stored one."""
    new = payload.model_dump(mode="json")
    return any(_norm(new[f]) != _norm(getattr(project, f)) for f in INTAKE_COLUMNS if f not in keep)


def now() -> datetime:
    return datetime.now(UTC)
