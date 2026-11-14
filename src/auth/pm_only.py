"""After the request, only PM (the conveyor / n8n) writes a project's resources.

George 2026-10-13, v4 ruling 1 (D15): fr may CREATE a project on a user's behalf
(``POST /pm/projects`` — the request, which writes brief version 1). Every later write to
that project's resources by a fr-delegated caller (``Principal.delegated``) is refused:
403 ``project_writes_are_pm_only``. Reads stay open to fr; non-delegated callers (the
conveyor) are unaffected.

George 2026-10-13 (later the same day): the PM RUNTIME entities are covered too — "that is
simpler". They are all linked to a project, so there is NO create exception there: a
delegated create is 403 as well. fr only creates projects.

ONE dependency, ``require_pm_writer`` (``PM_ONLY``), carried by every guarded route;
``tests/test_pm_fr_readonly_runtime.py`` and ``tests/test_pm_v4_rulings.py`` walk the app and
fail if a mutating route under a guarded prefix lacks it.

GUARDED (every POST/PUT/PATCH/DELETE):

* ``/pm/projects/{eid}...`` (create ``POST /pm/projects`` stays open: it is the request; a
  delegated ``PUT /pm/projects/{eid}`` may only CLOSE the project — ``{"title": <ignored>,
  "status": "closed"}``, R345 (4) — checked in the route, not by this dependency; a
  delegated get-or-create re-POST that lands on an EXISTING row is refused with the same error
  by ``ProjectService`` via ``project_writes_are_pm_only_error``). ``PUT
  /pm/projects/{eid}/planka-board`` completes the request (fr links the board it just built,
  v4 R73 / D20): open to fr while the project has no board or for the identical link; a
  different link afterwards is the same 403, raised by ``pm_project_board_service``.
* ``/pm/risks``, ``/pm/resources``, ``/pm/benefits`` — create, update, delete, and the
  ``/{eid}/quality`` POSTs (they persist a quality score).
* ``POST /pm/cards/{id}/ledger`` — the card ledger's only write (it has no update or delete).

NOT GUARDED by this module (fr may write them):

* the catalogue — ``/pm/project-types``, ``/pm/project-phases``, ``/pm/capabilities``,
  ``/pm/card-types``, ``/pm/recipes``, ``/pm/wip-limits``, ``/pm/priority-levels``,
  ``/pm/deliverable-kinds``, ``/pm/milestone-kinds``, ``/pm/gates``,
  ``/pm/board-templates``, ``/pm/catalogue`` (fr helps tenants manage it);
* ``/pm/programmes`` (out of v4 scope);
* the requirements / decisions / principles / architecture routes — EXCEPT that a write to
  ``/architecture/relationships`` (create, update, delete) touching a PM runtime entity at
  either end (before or after an update) is refused to a fr-delegated caller by
  ``require_pm_writer_for_entity_types``, so that generic route is not a side door around
  the PM routes. Delegated writes of plain architecture relationships (no PM runtime end)
  stay allowed.

The requirements / principles / decisions write routes carry the G3 guard instead
(``auth/governed_scope.py``): fr keeps its tenant-level writes there, and a NON-fr (conveyor)
write must name its project (``GOVERNED_WRITE_SCOPE_MODE`` observe → enforce).
"""

from fastapi import Depends, HTTPException, Request

from auth.author_kind import author_kind_of
from schemas.common import ErrorResponse

PM_ONLY_CODE = "project_writes_are_pm_only"


def project_writes_are_pm_only_error() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "code": PM_ONLY_CODE,
            "message": (
                f"{PM_ONLY_CODE}: after the project request, only PM (the conveyor) writes "
                "the project's resources; fr may read them"
            ),
        },
    )


def is_delegated(request: Request) -> bool:
    """True iff the request's resolved principal is fr-delegated."""
    return author_kind_of(getattr(request.state, "principal", None)) == "user"


async def require_pm_writer(request: Request) -> None:
    """FastAPI dependency: 403 ``project_writes_are_pm_only`` for a fr-delegated caller."""
    if is_delegated(request):
        raise project_writes_are_pm_only_error()


# ``ArchitectureRelationship.{source,target}_entity_type`` values (table names) that name a PM
# runtime entity. Only ``pm_resources`` is accepted by today's request schema (``EntityType``);
# the rest are listed so a widened vocabulary, or a legacy row, is covered without a second
# edit here.
PM_RUNTIME_ENTITY_TYPES = frozenset({"pm_resources", "pm_risks", "pm_benefits"})


async def require_pm_writer_for_entity_types(request: Request, *entity_types: str | None) -> None:
    """``require_pm_writer``, applied only when any of ``entity_types`` is a PM runtime
    entity (an endpoint of the relationship row being written)."""
    if any(t in PM_RUNTIME_ENTITY_TYPES for t in entity_types):
        await require_pm_writer(request)


# Route kwargs: ``dependencies=PM_ONLY`` and ``responses={..., **PM_ONLY_RESPONSES}``.
PM_ONLY = [Depends(require_pm_writer)]
PM_ONLY_403 = {
    "description": (
        "Access forbidden; ``project_writes_are_pm_only`` (a fr-delegated caller may not "
        "write a project's PM resources; George 10-13 v4 ruling 1)"
    ),
    "model": ErrorResponse,
}
PM_ONLY_RESPONSES: dict[int | str, dict] = {403: PM_ONLY_403}
