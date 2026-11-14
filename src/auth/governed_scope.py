"""G3 + R313 §1(b): a non-fr write to a governed asset must name its project.

George 2026-10-19, G3: R16 "only PM (n8n) writes RM" — "PM" means the conveyor. RM's
identity model (``auth/author_kind.py``, George 10-13) already splits every caller in two:
``user`` iff fr-delegated, ``conveyor`` otherwise. So on requirements / principles /
decisions there is no third caller class to refuse by identity alone; the PM-only rule
that has teeth here is about WHAT the conveyor may write:

* **fr-delegated (``author_kind=user``)** — unchanged: tenant-level edits without a project
  scope stay allowed (R313 §1(b)), and only fr may ``promote`` (``require_fr_delegated``).
* **conveyor (every non-delegated caller)** — its writes are PROJECT writes: create, PUT,
  PATCH and DELETE must name the project (``scope_project_id`` query, or the create body's
  ``project_id`` / ``origin_project_id``), and a tenant-wide bulk delete is never a project
  write. ``project-copy`` always carries its ``project_id`` and is unaffected.

``GOVERNED_WRITE_SCOPE_MODE`` (read per call; unset ⇒ ``observe``):

* ``observe`` — log a warning + count ``requirements_management_governed_write_unscoped_total``
  ``{result="allowed"}``, then allow (the conveyor does not send ``scope_project_id`` yet);
* ``enforce`` — 422 ``conveyor_write_requires_project_scope`` (counted ``result="refused"``).

An unknown value raises (fail closed); ``Settings`` rejects it at startup.
"""

import logging
import os
from typing import Any

from fastapi import HTTPException, Request

from auth.pm_only import is_delegated
from metrics import governed_write_unscoped_total
from schemas.common import ErrorResponse
from settings import parse_auth_mode

logger = logging.getLogger(__name__)

SCOPE_REQUIRED_CODE = "conveyor_write_requires_project_scope"
MODE_ENV = "GOVERNED_WRITE_SCOPE_MODE"


def governed_write_scope_mode() -> str:
    return parse_auth_mode(MODE_ENV, os.getenv(MODE_ENV))


def require_conveyor_project_scope(
    request: Request, *, kind: str, op: str, project_scope: Any
) -> None:
    """Allow fr-delegated calls and project-scoped calls; for an unscoped conveyor call,
    observe (log + count, allow) or enforce (422 ``conveyor_write_requires_project_scope``)."""
    if is_delegated(request):
        return
    if project_scope is not None and str(project_scope).strip():
        return
    mode = governed_write_scope_mode()
    principal = getattr(request.state, "principal", None)
    result = "refused" if mode == "enforce" else "allowed"
    governed_write_unscoped_total.labels(kind=kind, op=op, mode=mode, result=result).inc()
    logger.warning(
        "governed_write_unscoped kind=%s op=%s mode=%s result=%s tenant=%s subject=%s path=%s",
        kind,
        op,
        mode,
        result,
        getattr(principal, "tenant_id", ""),
        getattr(principal, "subject_id", ""),
        request.url.path,
    )
    if mode == "enforce":
        raise HTTPException(
            status_code=422,
            detail={
                "code": SCOPE_REQUIRED_CODE,
                "message": (
                    f"{SCOPE_REQUIRED_CODE}: a {kind} {op} not made through fr is a project "
                    "write (G3, R313 §1(b)); send scope_project_id (or the create body's "
                    "project_id / origin_project_id). Tenant-level edits are fr's."
                ),
            },
        )


SCOPE_REQUIRED_422: dict[int | str, dict[str, Any]] = {
    422: {
        "description": (
            f"Validation error, or `{SCOPE_REQUIRED_CODE}` (a non-fr write without a project "
            f"scope, when {MODE_ENV}=enforce)"
        ),
        "model": ErrorResponse,
    }
}


def body_project_scope(payload: Any) -> Any:
    """A create body's project scope: ``project_id`` (requirements, decisions) or
    ``origin_project_id`` (all three kinds)."""
    pid = getattr(payload, "project_id", None)
    if pid is not None and str(pid).strip():
        return pid
    return getattr(payload, "origin_project_id", None)
