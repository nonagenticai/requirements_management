"""``GET /pm/projects/{eid}/registers`` (REG-1): a project's risks, decisions, requirements
and criteria in one read, with the counts fr's "Registers at a glance" strip shows.

Scoping is the sibling ``/pm/projects/{eid}/...`` reads' own: the project is fetched with
``_get_visible`` (own tenant + the caller's user-visible rows; never another tenant, never an
inherited system row), so a cross-tenant ``eid`` is 404 before any register is read. Every
register query is then ``scope()``-d to the caller as well, so a row is only listed when the
caller could read it by id.

Which rows belong to the project:

* risks: ``pm_risks.project_id`` is "the parent project's slug or id" (free text), so both
  spellings match (trimmed, case-insensitive, as ``GovernedAssetService.owns`` compares).
* decisions / requirements (governed assets): the same ``project_id`` match, plus the
  project's own live project copies (``origin_project_id``), filtered through
  ``project_view_where`` so a promoted copy is not listed and an original the project has a
  copy of is shown once, as the copy.
* criteria: the CURRENT brief version's ``success_criteria`` (strings, no check) and
  ``acceptance_criteria`` (items with their check). No brief, or neither key: ``[]``.

Lists are capped at ``REGISTER_MAX_ROWS`` each (with a unique ORDER BY); the counts are SQL
aggregates over the same predicates, so they stay exact past the cap.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import case, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.decisions import Decision
from models.pm import Risk
from models.requirements import Requirement
from repositories.visibility import scope
from schemas.pm_intake import ProjectBrief
from schemas.registers import (
    ProjectRegistersOut,
    RegisterCounts,
    RegisterCriterion,
    RegisterDecision,
    RegisterRequirement,
    RegisterRisk,
)
from services import decision_service, requirement_service
from services.entity_service import _resolve_subject
from services.pm_project_brief_service import _current_snapshot

REGISTER_MAX_ROWS = 500
CLOSED_RISK_STATUSES = ("closed",)


def normalise_phase(phase: str | None) -> str | None:
    """The ``?phase=`` filter in the stored form (strip, lowercase; blank = no filter)."""
    if phase is None:
        return None
    return phase.strip().lower() or None


def _project_keys(project: Any) -> list[str]:
    return sorted({str(project.id).lower(), *([project.slug.lower()] if project.slug else [])})


def _project_id_match(model: Any, keys: list[str]) -> Any:
    return func.lower(func.trim(model.project_id)).in_(keys)


async def _rows(session: AsyncSession, stmt: Any) -> list[Any]:
    return list((await session.execute(stmt.limit(REGISTER_MAX_ROWS))).scalars().all())


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    )


def _criteria(snapshot: dict | None) -> list[RegisterCriterion]:
    if not snapshot:
        return []
    brief = ProjectBrief.model_validate(snapshot)
    out = [RegisterCriterion(kind="success", text=t) for t in (brief.success_criteria or [])]
    out.extend(
        RegisterCriterion(
            kind="acceptance",
            id=c.id,
            text=c.text,
            status=c.status,
            evidence=[e.model_dump(mode="json") for e in c.evidence],
            checked_at=c.checked_at,
            checked_by=c.checked_by,
        )
        for c in (brief.acceptance_criteria or [])
    )
    return out


def _risk(r: Risk) -> RegisterRisk:
    return RegisterRisk(
        id=r.id,
        slug=r.slug,
        title=r.title,
        description=r.description,
        category=r.category,
        probability=r.probability,
        impact=r.impact_score,
        score=round(r.risk_score, 2),
        status=r.status,
        owner=r.owner,
        response_strategy=r.response_strategy,
        response_plan=r.response_plan,
        phase=r.phase,
        card_id=r.card_id,
        raised_at=r.raised_at,
        due_at=r.due_at,
        last_reviewed_at=r.last_reviewed_at,
    )


def _decision(d: Decision) -> RegisterDecision:
    alternatives = list(d.alternatives or [])
    return RegisterDecision(
        id=d.id,
        slug=d.slug,
        title=d.title,
        status=d.status,
        context=d.context,
        decision=d.decision,
        consequences=d.consequences,
        alternatives=alternatives,
        alternatives_count=len(alternatives),
        decided_by_kind=d.decided_by_kind,
        decided_at=d.decided_at,
        approved_by=d.approved_by,
        phase=d.phase,
        card_id=d.card_id,
        raised_at=d.raised_at,
    )


def _requirement(r: Requirement) -> RegisterRequirement:
    return RegisterRequirement(
        id=r.id,
        slug=r.slug,
        title=r.title,
        description=r.description,
        status=r.status,
        owner=r.owner,
        acceptance=list(r.acceptance or []),
        version_status=r.version_status,
        phase=r.phase,
        card_id=r.card_id,
        raised_at=r.raised_at,
    )


async def registers(
    project_svc: Any,
    session: AsyncSession,
    eid: UUID,
    *,
    tenant_id: str,
    subject_id: str,
    phase: str | None = None,
) -> ProjectRegistersOut:
    project = await project_svc._get_visible(
        session, eid, tenant_id=tenant_id, subject_id=subject_id
    )
    phase = normalise_phase(phase)
    keys = _project_keys(project)
    sub = _resolve_subject(tenant_id, subject_id)

    def scoped(model: Any, *where: Any) -> Any:
        stmt = scope(select(model), model, tenant_id=tenant_id, subject_id=sub).where(*where)
        return stmt.where(model.phase == phase) if phase is not None else stmt

    risks_q = scoped(Risk, _project_id_match(Risk, keys))

    def governed(svc: Any, model: Any) -> Any:
        member = or_(_project_id_match(model, keys), svc._active_copy_clause(model, project.id))
        view = svc.project_view_where(project.id, tenant_id=tenant_id, subject_id=subject_id)
        return scoped(model, member, *view)

    decisions_q = governed(decision_service, Decision)
    requirements_q = governed(requirement_service, Requirement)

    risks = await _rows(
        session,
        risks_q.order_by(
            Risk.risk_score.desc(), nulls_last(Risk.raised_at.asc()), Risk.created_at, Risk.id
        ),
    )
    decided = func.coalesce(Decision.decided_at, Decision.raised_at, Decision.created_at)
    decisions = await _rows(
        session, decisions_q.order_by(nulls_last(decided.desc()), Decision.id.desc())
    )
    raised = func.coalesce(Requirement.raised_at, Requirement.created_at)
    requirements = await _rows(
        session, requirements_q.order_by(nulls_last(raised.desc()), Requirement.id.desc())
    )

    risk_sq = risks_q.subquery()
    open_flag = case((risk_sq.c.status.in_(CLOSED_RISK_STATUSES), 0), else_=1)
    risks_total, risks_open = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(open_flag), 0)).select_from(risk_sq)
        )
    ).one()
    criteria = _criteria(await _current_snapshot(session, project))

    return ProjectRegistersOut(
        project_id=project.id,
        phase=phase,
        risks=[_risk(r) for r in risks],
        decisions=[_decision(d) for d in decisions],
        requirements=[_requirement(r) for r in requirements],
        criteria=criteria,
        counts=RegisterCounts(
            risks_open=int(risks_open),
            risks_total=int(risks_total),
            decisions=await _count(session, decisions_q),
            requirements=await _count(session, requirements_q),
            criteria_met=sum(1 for c in criteria if c.status == "met"),
            criteria_total=len(criteria),
        ),
    )
