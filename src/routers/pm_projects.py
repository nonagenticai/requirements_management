"""Project CRUD endpoints."""

from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_subject_id, get_tenant_id, get_visibility
from auth.author_kind import AuthorKind, get_author_kind
from auth.pm_only import PM_ONLY, PM_ONLY_403, is_delegated, project_writes_are_pm_only_error
from db import get_session
from routers.responses import AUTH_RESPONSES
from schemas.common import ErrorResponse
from schemas.pm import (
    ProjectCapabilitiesOut,
    ProjectCapabilitiesSetIn,
    ProjectCapabilitiesSetOut,
    ProjectCapabilityEventOut,
    ProjectIn,
    ProjectOut,
    ProjectPhaseEventOut,
    ProjectPhaseSetIn,
    ProjectPhaseSetOut,
    ProjectWipLimitOut,
)
from schemas.pm_board_link import PlankaId, ProjectBoardLinkIn, ProjectBoardLinkOut, ProjectBoardOut
from schemas.pm_coach import (
    CoachActionAppliedIn,
    CoachActionIn,
    CoachActionOut,
    CoachAdmissionOut,
)
from schemas.pm_intake import (
    BriefVersionIn,
    BriefVersionOut,
    BriefVersionSummaryOut,
    CriterionCheckIn,
    CriterionCheckOut,
    CriterionId,
)
from schemas.registers import ProjectRegistersOut
from services import pm_coach_action_service as coach_svc
from services import pm_project_board_service as board_svc
from services import pm_project_brief_service as brief_svc
from services import pm_project_capability_service as capability_svc
from services import pm_project_phase_service as phase_svc
from services import pm_project_register_service as register_svc
from services import pm_project_wip_service as wip_svc
from services import project_service as svc

router = APIRouter()

# v4 ruling 1 (D15): after the request (the create), only PM writes a project's resources.
# Every mutating ``/{eid}...`` route carries this; fr-delegated callers get 403
# ``project_writes_are_pm_only``. Reads and the catalogue are not covered.
# ``PM_ONLY`` / ``PM_ONLY_403`` live in ``auth.pm_only`` (shared with the runtime routers).


@router.post(
    "",
    response_model=ProjectOut,
    operation_id="create_project",
    responses={
        **AUTH_RESPONSES,
        403: {
            "description": (
                "``project_writes_are_pm_only``: a fr-delegated ``origin_project_id`` re-POST "
                "that lands on an EXISTING project (a write after the request)"
            ),
            "model": ErrorResponse,
        },
        409: {
            "description": (
                "Conflicting change; ``brief_missing_v1`` (an ``origin_project_id`` re-POST "
                "with a brief onto an existing project that has no brief version 1); "
                "``project_type_missing_setup_phase`` (the type does not select a ``setup`` "
                "phase); ``origin_project_exists`` (the fr project already has a PM project "
                "the caller's visibility cannot reach; its id is not disclosed)"
            ),
            "model": ErrorResponse,
        },
        422: {
            "description": (
                "Validation error; ``brief_required`` (a project is created without a "
                "``brief``); ``project_type_required`` (no ``project_type_id``)"
            ),
            "model": ErrorResponse,
        },
    },
)
async def create_project(
    payload: ProjectIn,
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    visibility: str = Depends(get_visibility),
    author_kind: AuthorKind = Depends(get_author_kind),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectOut:
    """Create a new project.

    When ``origin_project_id`` is set (``origin_kind='project'``: the fr project this PM
    project is for) this is a get-or-create keyed on ``(tenant_id, origin_project_id)`` —
    one PM project per fr project for life, closed ones included (R345, unique index
    ``uq_pm_projects_tenant_origin_project``). A re-POST updates that project in place
    (title included) instead of minting a duplicate. When the existing row is filed under
    a visibility the caller's re-POST cannot reach, it is 409 ``origin_project_exists``
    (the existing id is not disclosed).

    ``project_type_id`` is REQUIRED (422 ``project_type_required``) and must be one of the
    tenant's project types (else 422). Every project starts in its type's ``setup`` system
    phase (George 10-13 v4 ruling 2 / D16): RM sets ``current_phase`` to it and writes the
    first phase event (null → setup); a type without a ``setup`` phase is 409
    ``project_type_missing_setup_phase``. Later phase moves: ``PUT /pm/projects/{eid}/phase``.

    This is the request: a fr-delegated caller may create (v4 ruling 1 / D15). Every later
    write to the project is PM-only (403 ``project_writes_are_pm_only``), and that includes
    a delegated ``origin_project_id`` re-POST that lands on an EXISTING row — it would update
    that row in place, so it is refused with the same 403 rather than treated as a create.

    A ``brief`` is REQUIRED whenever a project row is created (422 ``brief_required``): it
    becomes version 1 and sets its ``constraints`` freely (immutable afterwards). On the
    ``origin_project_id`` re-POST that lands on an existing row the brief stays optional
    (omitted = unchanged). That re-POST keeps the existing row's stored ``origin_*`` as they
    are: a differing ``origin_workflow_run_id`` is ignored, never written and never a 409
    (only ``PUT`` refuses an origin change). ``author_kind`` is derived from the caller
    (``auth.author_kind``): ``user`` iff fr-delegated, else ``conveyor``.
    """
    if payload.project_type_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "project_type_required",
                "message": (
                    "project_type_required: a project is created with its project_type_id; "
                    "it starts in that type's setup phase"
                ),
            },
        )
    payload._author_kind = author_kind
    if payload.origin_project_id:
        obj, _created = await svc.get_or_create(
            session,
            payload,
            tenant_id,
            subject_id,
            visibility=visibility,
            identity_fields=("origin_project_id",),
        )
        return obj
    return await svc.create(session, payload, tenant_id, subject_id, visibility=visibility)


@router.get(
    "/{eid}",
    response_model=ProjectOut,
    operation_id="get_project",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: {"description": "Access forbidden", "model": ErrorResponse},
    },
)
async def get_project(
    eid: UUID = Path(..., description="Project ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectOut:
    """Retrieve project by ID."""
    return await svc.get(session, eid, tenant_id, subject_id, include_system=True)


@router.get(
    "",
    response_model=list[ProjectOut],
    operation_id="list_projects",
    responses=AUTH_RESPONSES,
)
async def list_projects(
    response: Response,
    status: str | None = Query(None, description="Filter by status"),
    programme_id: str | None = Query(None, description="Filter by programme"),
    origin_project_id: UUID | None = Query(
        None, description="Filter by the fr project the row was minted for"
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ProjectOut]:
    """List projects with optional filters."""
    items, total = await svc.list_all(
        session,
        tenant_id,
        subject_id,
        include_system=True,
        limit=limit,
        offset=offset,
        status=status,
        programme_id=programme_id,
        origin_project_id=origin_project_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


FR_CLOSE_STATUS = "closed"


def _is_fr_close(payload: ProjectIn) -> bool:
    """True iff the PUT sets ``status`` to ``closed`` and nothing else. ``title`` is required
    by the schema, so it may be sent, but it is never written from fr's body."""
    return (
        payload.model_fields_set <= {"title", "status"}
        and "status" in payload.model_fields_set
        and payload.status == FR_CLOSE_STATUS
    )


@router.put(
    "/{eid}",
    response_model=ProjectOut,
    operation_id="update_project",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: PM_ONLY_403,
        409: {
            "description": (
                "Type change would strand the phase; ``brief_missing_v1`` (a brief sent for "
                "a project that has no brief version 1); ``origin_immutable`` (an origin_* "
                "value that differs from the stored one)"
            ),
            "model": ErrorResponse,
        },
        422: {
            "description": (
                "Validation error; ``brief_constraints_immutable`` (the brief's constraints "
                "differ from the current version's)"
            ),
            "model": ErrorResponse,
        },
    },
)
async def update_project(
    request: Request,
    eid: UUID = Path(..., description="Project ID"),
    payload: ProjectIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    author_kind: AuthorKind = Depends(get_author_kind),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectOut:
    """Update project.

    EVERY omitted field keeps its stored value (``status``, ``created_by_user_id``, dates,
    budget, ...); an explicit null clears a nullable field. ``title`` is still required.

    ⛔ ``origin_kind`` / ``origin_project_id`` / ``origin_workflow_run_id`` are the project's
    creation identity (the POST get-or-create key): set at create, IMMUTABLE afterwards.
    Omitted = kept; a value equal to the stored one is accepted (fr round-trips whole
    objects); any other value, an explicit null against a stored value included, is refused
    (409 ``origin_immutable``, naming the field).

    An omitted ``project_type_id`` or ``wip_limit`` is unchanged; an explicit null
    ``wip_limit`` clears it (the catalogue policy applies again). Changing it while the project has a
    current phase that the new type does not select is refused (409) — the phase is
    never silently cleared; move the phase first.

    A ``brief`` is appended as the next version. ⛔ Its ``constraints`` must equal the current
    version's, whoever the author is (422 ``brief_constraints_immutable``): the conveyor must
    never be able to loosen the gate that checks its work, and fr does not let users edit them
    after create either. A project with no version 1 (a legacy row) cannot gain one here: 409
    ``brief_missing_v1``. ``author_kind`` is ``user`` iff fr-delegated, else ``conveyor``.

    PM-only, with ONE fr exception (R345 (4)): when fr archives its project, a fr-delegated
    caller may CLOSE it — body ``{"title": <any>, "status": "closed"}``. Only ``status`` is
    applied: the stored title is kept (``title`` is schema-required but never written from
    fr). Any other field or status from fr is 403 ``project_writes_are_pm_only``.
    """
    keep: frozenset[str] = frozenset()
    if is_delegated(request):
        if not _is_fr_close(payload):
            raise project_writes_are_pm_only_error()
        keep = frozenset({"title"})
    payload._author_kind = author_kind
    return await svc.update(
        session, eid, payload, tenant_id, subject_id, keep_fields=keep, reject_origin_change=True
    )


@router.put(
    "/{eid}/phase",
    response_model=ProjectPhaseSetOut,
    operation_id="set_project_phase",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        403: PM_ONLY_403,
        404: {"description": "Project not found", "model": ErrorResponse},
        409: {
            "description": (
                "Project has no project_type_id, if_current_phase does not match "
                "(the detail carries the actual current phase), or a concurrent change"
            ),
            "model": ErrorResponse,
        },
        422: {"description": "Not one of the type's phases", "model": ErrorResponse},
    },
)
async def set_project_phase(
    eid: UUID = Path(..., description="Project ID"),
    payload: ProjectPhaseSetIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectPhaseSetOut:
    """Set the project's current phase (n8n is the only writer; fr reads it).

    Give exactly one of ``phase_id`` / ``phase`` (the phase's name). The ONLY check is
    that the phase is one of the project type's selected phases (else 422 naming the
    allowed phases); a project with no ``project_type_id`` is 409. Gates are approvals,
    not transition rules: skipping a gated phase forward and moving backwards are both
    accepted.

    Idempotent: re-stamping the phase the project is already in returns 200 with
    ``changed: false`` and does NOT bump ``phase_updated_at`` or write a phase event.
    Every real change writes one ``pm_project_phase_events`` row in the same transaction.

    ``restart: true`` starts a new visit even when the target is the current phase: it
    bumps ``phase_updated_at`` (so the coach's per-visit caps start at zero), writes a
    phase event with ``from_phase_id == to_phase_id``, and returns ``changed: true``.

    ``if_current_phase`` (id or name) is a compare-and-set: when present and not the
    current phase, 409 with the actual ``current_phase``. An explicit ``null`` means
    "expect no phase yet"; omitted means no check.
    """
    return await phase_svc.set_phase(svc, session, eid, payload, tenant_id, subject_id)


@router.patch(
    "/{eid}/criteria/{criterion_id}",
    response_model=CriterionCheckOut,
    operation_id="set_project_criterion_check",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        403: PM_ONLY_403,
        404: {
            "description": "Project not found, or ``criterion_not_found``",
            "model": ErrorResponse,
        },
        409: {"description": "A concurrent brief change; retry", "model": ErrorResponse},
        422: {
            "description": (
                "``criterion_met_needs_evidence`` / ``criterion_unchecked_takes_no_evidence``"
            ),
            "model": ErrorResponse,
        },
    },
)
async def set_project_criterion_check(
    eid: UUID = Path(..., description="Project ID"),
    criterion_id: CriterionId = Path(..., description="The acceptance criterion's id"),
    payload: CriterionCheckIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    author_kind: AuthorKind = Depends(get_author_kind),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CriterionCheckOut:
    """Mark one acceptance criterion ``met`` / ``not_met`` / ``unchecked``, with evidence.

    PM / n8n only (R-CK7: the conveyor's QA and closure flows check criteria; fr displays
    them and never marks one): a fr-delegated caller is 403 ``project_writes_are_pm_only``,
    exactly as for ``PUT /pm/projects/{eid}/phase``. RM stamps ``checked_at`` (now) and
    ``checked_by`` (the caller's principal). The check is stored in the brief: the change
    appends the next brief version (note ``acceptance criterion <id>: <status>``).
    Idempotent: the same status and evidence again is ``changed: false`` with no new version.
    """
    return await brief_svc.set_criterion_check(
        svc,
        session,
        eid,
        criterion_id,
        payload,
        author_kind=author_kind,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


@router.get(
    "/{eid}/capabilities",
    response_model=ProjectCapabilitiesOut,
    operation_id="get_project_capabilities",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Project not found", "model": ErrorResponse},
    },
)
async def get_project_capabilities(
    eid: UUID = Path(..., description="Project ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectCapabilitiesOut:
    """The project's capability selection (R260 v1; fr reads it, PM writes it).

    ``selected`` — what the PM selected, with the reason and the approving sign-off.
    ``implicit`` — ``project_management`` and ``kanban_coach``: every project has them and
    nobody selects them. ``selectable`` — every capability with at least one card type in the
    project type's composition (minus the implicit two), with its card types per phase in the
    type's phase order; ``purpose`` is the capability's catalogue description.
    """
    return await capability_svc.get(svc, session, eid, tenant_id, subject_id)


@router.get(
    "/{eid}/capabilities/events",
    response_model=list[ProjectCapabilityEventOut],
    operation_id="list_project_capability_events",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Project not found", "model": ErrorResponse},
    },
)
async def list_project_capability_events(
    response: Response,
    eid: UUID = Path(..., description="Project ID"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ProjectCapabilityEventOut]:
    """The project's capability selection changes, newest first (``X-Total-Count`` carries
    the total)."""
    items, total = await capability_svc.list_events(
        svc, session, eid, tenant_id, subject_id, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.put(
    "/{eid}/capabilities",
    response_model=ProjectCapabilitiesSetOut,
    operation_id="set_project_capabilities",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        403: PM_ONLY_403,
        404: {"description": "Project not found", "model": ErrorResponse},
        409: {
            "description": (
                "Project has no project_type_id; ``if_current_mismatch`` (the detail names the "
                "current selection); or a concurrent change"
            ),
            "model": ErrorResponse,
        },
        422: {
            "description": (
                "``capability_not_in_type_composition`` (names the offending capabilities); "
                "``implicit_capability`` (project_management / kanban_coach); an empty list, "
                "an empty reason or a repeated name"
            ),
            "model": ErrorResponse,
        },
    },
)
async def set_project_capabilities(
    eid: UUID = Path(..., description="Project ID"),
    payload: ProjectCapabilitiesSetIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectCapabilitiesSetOut:
    """Replace the project's capability selection (PM / n8n only; v4 ruling 1).

    The body is the FULL selection: capabilities not listed are removed. Each needs a
    non-empty ``reason`` and must be selectable (a card type in the project type's
    composition, else 422 ``capability_not_in_type_composition``); the implicit
    ``project_management`` / ``kanban_coach`` are refused (422 ``implicit_capability``).

    R302: each capability may also carry the PM's consult decision — ``involvement``
    (none|light|standard|heavy), ``purpose``, ``rigor`` / ``complexity`` (low|medium|high),
    the raw ``consult`` answer and ``order_after`` (selected capability names that precede
    it). All optional; an omitted one is stored null (``order_after`` ``[]``).

    Idempotent: an event (``GET .../capabilities/events``) is written only when the selected
    NAME set changes; a change only to a kept capability's reason or R302 fields updates the
    rows, writes no event and returns ``changed: false``. ``approval_id`` (the fr sign-off) is stored on the added rows and the
    event. ``if_current`` is a compare-and-set on the current names (order-insensitive):
    a mismatch is 409 ``if_current_mismatch``.
    """
    return await capability_svc.set_capabilities(svc, session, eid, payload, tenant_id, subject_id)


@router.get(
    "/{eid}/coach-actions",
    response_model=list[CoachActionOut],
    operation_id="list_project_coach_actions",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Project not found", "model": ErrorResponse},
    },
)
async def list_project_coach_actions(
    response: Response,
    eid: UUID = Path(..., description="Project ID"),
    target_card_id: str | None = Query(
        None, max_length=64, description="Only the rows of this target card"
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[CoachActionOut]:
    """The project's kanban-coach admissions, oldest first (``X-Total-Count`` carries the
    total). R311: RM is the durable record; markers on the card are the trace."""
    items, total = await coach_svc.list_actions(
        svc,
        session,
        eid,
        tenant_id,
        subject_id,
        target_card_id=target_card_id,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.post(
    "/{eid}/coach-actions",
    response_model=CoachAdmissionOut,
    operation_id="record_project_coach_action",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        403: PM_ONLY_403,
        404: {"description": "Project not found", "model": ErrorResponse},
        409: {
            "description": (
                "``coach_execution_id_reused`` (admitted on another project of the tenant); "
                "``conflict`` (a concurrent change the lock did not serialise)"
            ),
            "model": ErrorResponse,
        },
    },
)
async def record_project_coach_action(
    eid: UUID = Path(..., description="Project ID"),
    payload: CoachActionIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CoachAdmissionOut:
    """Admit a kanban coach (R311, kanban-coach-errors.md §4.3, §4.4; PM / n8n only).

    One transaction under the project row lock. A re-POST by the same ``coach_execution_id``
    is answered from its row (``replayed: true``). A second coach of the same incident
    (tenant, project, ``target_card_id``, ``entry``, ``root_execution_id``, ``exit_group``)
    gets ``{admitted: false, reason: same_incident, winner}`` and must post nothing.
    Otherwise the caps (``caps``, defaults 1/3/10) move ``requested_remedy`` only UP the order
    rerun = move = repull < ask < block: a second automatic remedy in the target's entry ⇒
    ``ask``; an ask after an ask in the entry, or with ``ask_allowed: false`` ⇒ ``block``; the
    card's cap for the current phase visit ⇒ ``block``; a NEW incident beyond the project's
    cap for the current phase visit ⇒ ``block``. Every count uses the effective remedy
    (``applied_remedy`` if recorded, else ``remedy``); ``note`` rows count for none. The
    answer carries the counts including the admitted row.
    """
    return await coach_svc.admit(svc, session, eid, payload, tenant_id, subject_id)


@router.patch(
    "/{eid}/coach-actions/{action_id}",
    response_model=CoachActionOut,
    operation_id="record_project_coach_action_applied",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        403: PM_ONLY_403,
        404: {"description": "Project or admission not found", "model": ErrorResponse},
        409: {
            "description": (
                "``coach_execution_mismatch`` (not this row's coach); ``not_admitted``; "
                "``applied_remedy_already_set`` (a different value was recorded)"
            ),
            "model": ErrorResponse,
        },
        422: {
            "description": (
                "``applied_remedy_not_allowed`` (not keep / note / ask-block upgrade); "
                "``reason_required`` (an upgrade without ``post_admit_unverified``)"
            ),
            "model": ErrorResponse,
        },
    },
)
async def record_project_coach_action_applied(
    eid: UUID = Path(..., description="Project ID"),
    action_id: UUID = Path(..., description="Coach admission ID"),
    payload: CoachActionAppliedIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> CoachActionOut:
    """Record the remedy the admitted coach applies after its post-Admit re-read (R311 §3).

    Under the project row lock. The coach may KEEP the admitted remedy, DROP to ``note`` (on
    evidence someone else acted), or, when the admitted remedy is automatic, UPGRADE to
    ``ask`` / ``block`` with ``reason: post_admit_unverified``. RM applies the per-entry rule:
    an ``ask`` upgrade becomes ``block`` when another row of this target + entry already has
    effective ``ask``; ``effective_remedy`` in the answer is what the coach applies.
    Idempotent for the same value; a different value once recorded is 409.
    """
    return await coach_svc.record_applied(
        svc, session, eid, action_id, payload, tenant_id, subject_id
    )


@router.put(
    "/{eid}/planka-board",
    response_model=ProjectBoardLinkOut,
    operation_id="link_project_planka_board",
    responses={
        **AUTH_RESPONSES,
        403: {
            "description": (
                "``project_writes_are_pm_only``: a fr-delegated caller sends a DIFFERENT link "
                "for a project that already has one (re-linking is PM's)"
            ),
            "model": ErrorResponse,
        },
        404: {"description": "Project not found", "model": ErrorResponse},
        409: {
            "description": "``planka_board_in_use``: the board is linked to another project "
            "of this tenant",
            "model": ErrorResponse,
        },
    },
)
async def link_project_planka_board(
    request: Request,
    eid: UUID = Path(..., description="Project ID"),
    payload: ProjectBoardLinkIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectBoardLinkOut:
    """Link the project to its Planka board (George 10-13, v4 R73 / D20).

    fr calls this once, right after it builds the board from RM's board template: the board,
    its Planka project and the list id of each conveyor list (TODO, READY, DOING, QA, DONE,
    BLOCKED). Linking the board completes the request, so a fr-delegated caller may write it
    while the project has no board.

    Idempotent: re-sending the stored link is 200 with ``changed: false`` and writes nothing.
    A different link for a project that already has one is 403 ``project_writes_are_pm_only``
    for fr (v4 ruling 1); PM (the conveyor) may re-link. A board linked to another project of
    this tenant is 409 ``planka_board_in_use`` (ids are per tenant: Planka is per tenant).
    """
    return await board_svc.link(
        svc, session, eid, payload, tenant_id, subject_id, delegated=is_delegated(request)
    )


@router.get(
    "/by-planka-board/{planka_board_id}",
    response_model=ProjectBoardOut,
    operation_id="get_project_by_planka_board",
    responses={
        **AUTH_RESPONSES,
        404: {
            "description": "``planka_board_not_linked``: no project of this tenant has the board",
            "model": ErrorResponse,
        },
    },
)
async def get_project_by_planka_board(
    planka_board_id: PlankaId = Path(..., description="The Planka board id (webhook boardId)"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectBoardOut:
    """The project linked to a Planka board: what n8n needs to go from a Planka webhook
    (``data.item.boardId`` / ``listId``) to the RM project — tenant, fr project id, type,
    phase, status, WIP limit, repository url, and the conveyor list of each list id
    (``lists_by_id``)."""
    return await board_svc.lookup(svc, session, planka_board_id, tenant_id, subject_id)


@router.get(
    "/{eid}/phase-events",
    response_model=list[ProjectPhaseEventOut],
    operation_id="list_project_phase_events",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Project not found", "model": ErrorResponse},
    },
)
async def list_project_phase_events(
    response: Response,
    eid: UUID = Path(..., description="Project ID"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[ProjectPhaseEventOut]:
    """The project's phase changes, newest first (``X-Total-Count`` carries the total)."""
    items, total = await phase_svc.list_events(
        svc, session, eid, tenant_id, subject_id, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get(
    "/{eid}/registers",
    response_model=ProjectRegistersOut,
    operation_id="get_project_registers",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Project not found", "model": ErrorResponse},
    },
)
async def get_project_registers(
    eid: UUID = Path(..., description="Project ID"),
    phase: str | None = Query(
        None,
        max_length=64,
        description=(
            "Only rows raised in this phase (a phase name, e.g. setup, preliminary, "
            "execution). Filters risks, decisions and requirements; criteria are never filtered."
        ),
    ),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectRegistersOut:
    """The project's registers in one read: risks, decisions, requirements, criteria + counts.

    Risks are ordered by score (probability x impact, rounded to 2 decimals) highest first,
    decisions by decision time and requirements by raise time, newest first. Criteria come
    from the CURRENT brief: ``success_criteria`` (``kind: success``, no check) and
    ``acceptance_criteria`` (``kind: acceptance``, with its check); none = ``[]``.
    """
    return await register_svc.registers(
        svc, session, eid, tenant_id=tenant_id, subject_id=subject_id, phase=phase
    )


@router.get(
    "/{eid}/wip-limit",
    response_model=ProjectWipLimitOut,
    operation_id="get_project_wip_limit",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Project not found", "model": ErrorResponse},
    },
)
async def get_project_wip_limit(
    eid: UUID = Path(..., description="Project ID"),
    phase_id: UUID | None = Query(None, description="The card's phase (for ``scoped``)"),
    capability_id: UUID | None = Query(None, description="The card's capability (for ``scoped``)"),
    card_type_id: UUID | None = Query(None, description="The card's card type (for ``scoped``)"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ProjectWipLimitOut:
    """The WIP limit to check before pulling one of the project's cards into DOING.

    ``effective_wip_limit`` is the project TOTAL: the project's own ``wip_limit`` when set
    (``source: "project"``; it always wins for the total), else the tenant's catalogue
    project-total row for the project's type, else the tenant-global total (``source:
    "catalogue"``, ``catalogue_limit_id`` names the row), else null (``source: "none"``,
    no limit). 0 means no card may be in DOING (paused).

    Given a phase / capability / card type, ``scoped`` is the most specific catalogue row
    that matches on one of them (e.g. a per-card-type cap), or null. It caps that scope;
    the project total applies as well.
    """
    out = await svc.get(session, eid, tenant_id, subject_id, include_system=True)
    return await wip_svc.project_wip_limit(
        session,
        tenant_id,
        out,
        {"phase_id": phase_id, "capability_id": capability_id, "card_type_id": card_type_id},
    )


@router.post(
    "/{eid}/brief/versions",
    response_model=BriefVersionOut,
    operation_id="create_project_brief_version",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        403: PM_ONLY_403,
        404: {"description": "Project not found", "model": ErrorResponse},
        409: {
            "description": (
                "``brief_missing_v1`` (the project has no brief version 1 — a legacy row; v1 "
                "is written only at creation); ``brief_version_mismatch`` (if_current_version "
                "is not the current version; the detail carries ``current_version``) or a "
                "concurrent append"
            ),
            "model": ErrorResponse,
        },
        422: {
            "description": (
                "Not a valid brief; ``brief_constraints_immutable`` (constraints differ from "
                "the current version's)"
            ),
            "model": ErrorResponse,
        },
    },
)
async def create_project_brief_version(
    eid: UUID = Path(..., description="Project ID"),
    payload: BriefVersionIn = Body(...),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    author_kind: AuthorKind = Depends(get_author_kind),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> BriefVersionOut:
    """Append the next version of the project's brief (a FULL snapshot) and make it current.

    Versions are append-only and numbered 1, 2, … under a row lock on the project.

    ⛔ ``constraints`` are IMMUTABLE after version 1, for every author: a snapshot whose
    constraints differ from the current version's (same list, same order, deep-equal) is 422
    ``brief_constraints_immutable``. v1 is written at create; it is ``user`` when fr creates
    the project on a user's behalf (delegated), otherwise ``conveyor``. Appending to a project
    without v1 is refused (409 ``brief_missing_v1``).

    ``author_kind`` is derived from the caller, never the body: ``user`` iff the call is
    fr-delegated (``Principal.delegated``), else ``conveyor``. The author's subject is the
    caller's principal. ``if_current_version`` is a compare-and-set (409
    ``brief_version_mismatch``; omitted = no check).
    Re-sending an identical snapshot still appends a version (intentional: ``note`` says why).
    """
    cas = (
        payload.if_current_version
        if "if_current_version" in payload.model_fields_set
        else (brief_svc._UNSET)
    )
    return await brief_svc.append(
        svc,
        session,
        eid,
        brief=payload.brief,
        author_kind=author_kind,
        note=payload.note,
        if_current_version=cas,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


@router.get(
    "/{eid}/brief/versions",
    response_model=list[BriefVersionSummaryOut],
    operation_id="list_project_brief_versions",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Project not found", "model": ErrorResponse},
    },
)
async def list_project_brief_versions(
    response: Response,
    eid: UUID = Path(..., description="Project ID"),
    limit: int = Query(100, ge=1, le=1000, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[BriefVersionSummaryOut]:
    """The brief's versions, newest first, without the snapshots (``X-Total-Count``)."""
    items, total = await brief_svc.list_versions(
        svc, session, eid, tenant_id=tenant_id, subject_id=subject_id, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get(
    "/{eid}/brief/versions/{brief_version}",
    response_model=BriefVersionOut,
    operation_id="get_project_brief_version",
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Project or version not found", "model": ErrorResponse},
    },
)
async def get_project_brief_version(
    eid: UUID = Path(..., description="Project ID"),
    brief_version: int = Path(
        ...,
        ge=1,
        description=(
            "Brief version number. Named ``brief_version`` (not ``version``) so the generated "
            "n8n node does not merge it with the project body field ``version``."
        ),
    ),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> BriefVersionOut:
    """One version of the brief, with its full snapshot."""
    return await brief_svc.get_version(
        svc, session, eid, brief_version, tenant_id=tenant_id, subject_id=subject_id
    )


@router.delete(
    "/{eid}",
    operation_id="delete_project",
    dependencies=PM_ONLY,
    responses={
        **AUTH_RESPONSES,
        404: {"description": "Element not found", "model": ErrorResponse},
        403: PM_ONLY_403,
    },
)
async def delete_project(
    eid: UUID = Path(..., description="Project ID"),
    tenant_id: str = Depends(get_tenant_id),
    subject_id: str = Depends(get_subject_id),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> dict[str, Any]:
    """Delete project."""
    return await svc.delete(session, eid, tenant_id, subject_id)
