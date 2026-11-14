"""A project's WIP limit: its own value, else the tenant's catalogue policy (George 2026-10-13).

"RM's default limit lets only one card per project be in DOING at a time. That is the WIP
limit, and we should define wip-limit in RM on the project fields of the API."

* ``pm_projects.wip_limit`` is the project TOTAL. NULL = not set on the project; 0 = no
  card may be in DOING (a pause). When set it always wins for the total.
* Otherwise the catalogue (``pm_wip_limits``) decides: the project-total row resolved for
  the project's ``project_type_id`` with phase, capability and card type all NULL — the
  per-project-type total, else the tenant-global total (``pm_catalogue_service`` resolve
  rules). Otherwise there is no limit (``source = "none"``).
* Only the CALLER's tenant catalogue is ever read. A system-tenant project inherited into
  another tenant's read never resolves through that tenant's catalogue (its type is not
  theirs): it reports its own value or ``"none"``.
* ``enrich`` fills ``ProjectOut`` for a whole page with ONE catalogue query, never one per
  project.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from repositories.visibility import same_tenant
from schemas.pm import ProjectOut, ProjectWipLimitOut, ScopedWipLimit
from services import pm_catalogue_service as cat
from settings import get_settings

_SCOPE_DIMS = ("phase_id", "capability_id", "card_type_id")


def _uses_caller_catalogue(out: ProjectOut, tenant_id: str) -> bool:
    """The caller's catalogue applies to the caller's own projects only."""
    return not out.is_system or same_tenant(tenant_id, get_settings().system_tenant_id)


async def enrich(
    session: AsyncSession, tenant_id: str, outs: list[ProjectOut]
) -> dict[UUID | None, Any]:
    """Set ``effective_wip_limit`` / ``wip_limit_source`` on every item (one query total).

    Returns the catalogue project-total row used per project type.
    """
    need = {
        o.project_type_id
        for o in outs
        if o.wip_limit is None and _uses_caller_catalogue(o, tenant_id)
    }
    totals = await cat.project_total_wip_limits(session, tenant_id, need)
    for o in outs:
        if o.wip_limit is not None:
            o.effective_wip_limit, o.wip_limit_source = o.wip_limit, "project"
            continue
        row = totals.get(o.project_type_id) if _uses_caller_catalogue(o, tenant_id) else None
        if row is not None:
            o.effective_wip_limit, o.wip_limit_source = row.wip_limit, "catalogue"
        else:
            o.effective_wip_limit, o.wip_limit_source = None, "none"
    return totals


async def project_wip_limit(
    session: AsyncSession,
    tenant_id: str,
    out: ProjectOut,
    scope: dict[str, UUID | None],
) -> ProjectWipLimitOut:
    """The project's total limit, plus the more specific catalogue row for ``scope``."""
    totals = await enrich(session, tenant_id, [out])
    total_row = totals.get(out.project_type_id) if out.wip_limit_source == "catalogue" else None

    scoped = None
    if any(scope.get(d) is not None for d in _SCOPE_DIMS) and _uses_caller_catalogue(
        out, tenant_id
    ):
        query = {"project_type_id": out.project_type_id, **{d: scope.get(d) for d in _SCOPE_DIMS}}
        best = await cat.match_wip_limit(session, tenant_id, query)
        if best is not None and any(getattr(best, d) is not None for d in _SCOPE_DIMS):
            scoped = ScopedWipLimit(
                id=best.id, wip_limit=best.wip_limit, matched_on=cat.wip_matched_on(best)
            )

    return ProjectWipLimitOut(
        project_id=out.id,
        wip_limit=out.wip_limit,
        effective_wip_limit=out.effective_wip_limit,
        source=out.wip_limit_source,
        catalogue_limit_id=total_row.id if total_row is not None else None,
        scoped=scoped,
    )
