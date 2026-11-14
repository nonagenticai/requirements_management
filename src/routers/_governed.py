"""Shared route pieces for the G8 governed-asset rules (principles / requirements / decisions).

See ``services/governed_asset_service.py`` for the rules themselves.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, create_model

from auth.pm_only import is_delegated
from schemas.common import ErrorResponse, partial_model

PROMOTE_FR_ONLY_CODE = "asset_promotion_requires_fr"


def scope_project_query(*, for_list: bool) -> Any:
    """``scope_project_id`` — the project a call is made FOR. Read, never written."""
    if for_list:
        description = (
            "Project view (G8): list what this project uses. The project's own copy of an "
            "asset is returned IN PLACE OF the original it supersedes (see "
            "`include_shadowed`); other projects' copies and promoted copies are left out. "
            "Not a filter on any column — the tenant's other assets are still listed. "
            "Unlike `project_id`, which filters."
        )
    else:
        description = (
            "The project this edit is made for (G8). Used ONLY to check ownership — it is "
            "never written: when set, the row must be owned by this project "
            "(origin_kind='project' and origin_project_id equal to it, or project_id equal to "
            "it), else 409 `inherited_asset_requires_copy`; POST `.../project-copy` instead. "
            "Omit it for a tenant-level edit."
        )
    return Query(None, description=description)


def include_shadowed_query() -> Any:
    return Query(
        False,
        description=(
            "With `scope_project_id`: also list the originals the project's copies stand in "
            "for. Ignored without `scope_project_id`."
        ),
    )


def governed_patch_model(in_schema: type[BaseModel], server_managed: frozenset[str]):
    """The PATCH body: ``in_schema`` all-optional, minus the server-managed fields."""
    return partial_model(in_schema, exclude=server_managed)


def project_copy_model(patch_model: type[BaseModel], kind: str) -> type[BaseModel]:
    """``{project_id, changes}`` — ``changes`` is the kind's PATCH body."""
    # A runtime-built model is a VALUE here, not a type expression: bind it to
    # ``Any`` so the ``| None`` below is checked as the runtime union it is.
    changes_model: Any = patch_model
    return create_model(  # type: ignore[call-overload]
        f"{kind}ProjectCopyIn",
        __config__=ConfigDict(extra="forbid"),
        project_id=(
            UUID,
            Field(..., description="The project the copy is made for (its origin_project_id)"),
        ),
        changes=(
            changes_model | None,
            Field(
                default=None,
                description=(
                    "Fields to change on the copy (the PATCH body of this kind). On a repeat "
                    "call for the same project they patch the existing copy."
                ),
            ),
        ),
    )


class ProjectCopyStatic(BaseModel):
    """What type checkers see for a kind's runtime-built ``project_copy_model`` body."""

    project_id: UUID
    changes: BaseModel | None = None


class PromoteIn(BaseModel):
    """Optional promotion details recorded on the original."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approved_by: str | None = Field(
        default=None,
        max_length=100,
        description="The tenant_admin who approved the promotion (recorded as approved_by)",
    )


async def require_fr_delegated(request: Request) -> None:
    """Promotion is fr's, after a tenant_admin approval (G8 R313, R208: approver rights
    live in fr). RM trusts fr exactly where it already does: a fr-DELEGATED principal (an
    introspected subject on ``AUTH_DELEGATING_SUBJECTS`` acting via ``X-Effective-Tenant-Id``).
    Everything else — a plain licence (the conveyor), a
    non-allowlisted subject — is refused. Fail closed: ``AUTH_DELEGATING_SUBJECTS`` empty ⇒
    nobody may promote."""
    if not is_delegated(request):
        raise HTTPException(
            status_code=403,
            detail={
                "code": PROMOTE_FR_ONLY_CODE,
                "message": (
                    f"{PROMOTE_FR_ONLY_CODE}: promoting a project copy to the tenant is an fr "
                    "approval; only a fr-delegated call may do it"
                ),
            },
        )


def project_copy_responses(kind: str) -> dict[int | str, dict[str, Any]]:
    return {
        404: {"description": f"{kind} not found", "model": ErrorResponse},
        409: {
            "description": (
                "`asset_is_a_project_copy`, `asset_owned_by_project` (PATCH it with "
                "scope_project_id instead), `project_copy_conflict` (the project already has "
                "an asset with this identity), `asset_superseded`"
            ),
            "model": ErrorResponse,
        },
        422: {"description": "Validation error", "model": ErrorResponse},
    }


def promote_responses(kind: str) -> dict[int | str, dict[str, Any]]:
    return {
        403: {
            "description": f"`{PROMOTE_FR_ONLY_CODE}`: only a fr-delegated call may promote",
            "model": ErrorResponse,
        },
        404: {"description": f"{kind} not found", "model": ErrorResponse},
        409: {
            "description": ("`not_a_project_copy`, `copy_already_promoted`, `original_missing`"),
            "model": ErrorResponse,
        },
    }


PATCH_409 = {
    409: {
        "description": (
            "`inherited_asset_requires_copy` (scope_project_id does not own the row) or "
            "`asset_superseded` (a promoted copy is read-only)"
        ),
        "model": ErrorResponse,
    }
}
