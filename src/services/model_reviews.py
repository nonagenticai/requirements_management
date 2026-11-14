"""Review and publication of sealed revisions (plan 2026-10-26 §7.3).

Lifecycle: draft head → sealed revision → reviewed (approve | reject) → published. Review
state is kept apart from the immutable revision. The reviewer and the publisher come from
the authenticated principal, never from the body. Every step runs on a one-user tenant:
the subject who sealed a revision may approve it, and a private model (``visibility='user'``)
is reviewed and published by its owner like any other.

Publishing revision R is refused (409) unless
* the LATEST review of R approves it (``revision_not_approved``), and
* a validation run of the model's current ruleset exists on R's ``content_hash`` (a
  revision run, or a draft-head run of identical content) that is ``complete``, ``clean``
  and has every required rule enabled (``revision_not_validated``).

Publishing sets ``modeling_models.published_revision_id`` and appends a
``modeling_publications`` row naming the revision it supersedes, so the history of what
was published when is kept. The legacy per-row ``approved_by`` / ``approved_at`` /
``published_at`` fields are server-managed on architecture rows and play no part here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.modeling import (
    ModelingModelRevision,
    ModelingPublication,
    ModelingReview,
    ModelingValidationRun,
)
from schemas.modeling_lifecycle import ReviewIn
from services import modeling_models as models_svc
from services.modeling_validation import ruleset_for

REVISION_NOT_APPROVED = "revision_not_approved"
REVISION_NOT_VALIDATED = "revision_not_validated"
REVISION_ALREADY_PUBLISHED = "revision_already_published"
REASON_REQUIRED = "review_reason_required"


def _sub(tenant_id: str, subject_id: str) -> str:
    return subject_id or tenant_id


async def review_revision(
    session: AsyncSession,
    model_id: UUID,
    revision_id: UUID,
    payload: ReviewIn,
    *,
    tenant_id: str,
    subject_id: str,
) -> ModelingReview:
    model = await models_svc.get_model(session, model_id, tenant_id, subject_id)
    revision = await models_svc.get_revision(session, model.id, revision_id)
    if payload.decision == "reject" and not payload.reason:
        raise HTTPException(
            status_code=422,
            detail={"code": REASON_REQUIRED, "message": "a rejection must carry a reason"},
        )
    review = ModelingReview(
        tenant_id=model.tenant_id,
        model_id=model.id,
        revision_id=revision.id,
        decision=payload.decision,
        reason=payload.reason,
        actor=_sub(tenant_id, subject_id),
    )
    session.add(review)
    await session.commit()
    await session.refresh(review)
    return review


async def list_reviews(
    session: AsyncSession, model_id: UUID, revision_id: UUID
) -> list[ModelingReview]:
    return list(
        (
            await session.execute(
                select(ModelingReview)
                .where(
                    ModelingReview.model_id == model_id,
                    ModelingReview.revision_id == revision_id,
                )
                .order_by(ModelingReview.created_at.desc(), ModelingReview.id.desc())
            )
        )
        .scalars()
        .all()
    )


def _refused(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


async def qualifying_run(
    session: AsyncSession, model: Any, revision: ModelingModelRevision
) -> ModelingValidationRun | None:
    """The newest complete, clean run on the revision's content with every required rule."""
    ruleset = ruleset_for(model.language)
    runs = (
        (
            await session.execute(
                select(ModelingValidationRun)
                .where(
                    ModelingValidationRun.model_id == model.id,
                    ModelingValidationRun.content_hash == revision.content_hash,
                    ModelingValidationRun.ruleset_id == ruleset.info.ruleset_id,
                    ModelingValidationRun.status == "complete",
                    ModelingValidationRun.clean.is_(True),
                )
                .order_by(ModelingValidationRun.finished_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return next((r for r in runs if ruleset.required <= set(r.enabled_rules)), None)


async def publish_revision(
    session: AsyncSession,
    model_id: UUID,
    revision_id: UUID,
    *,
    tenant_id: str,
    subject_id: str,
) -> ModelingPublication:
    model = await models_svc.get_mutable_model(session, model_id, tenant_id, subject_id, lock=True)
    revision = await models_svc.get_revision(session, model.id, revision_id)
    if model.published_revision_id == revision.id:
        raise _refused(REVISION_ALREADY_PUBLISHED, "this revision is already the published one")
    reviews = await list_reviews(session, model.id, revision.id)
    if not reviews or reviews[0].decision != "approve":
        raise _refused(
            REVISION_NOT_APPROVED,
            "the revision's latest review is not an approval"
            if reviews
            else "the revision has not been reviewed",
        )
    run = await qualifying_run(session, model, revision)
    if run is None:
        raise _refused(
            REVISION_NOT_VALIDATED,
            "no complete, clean validation run with every required rule enabled exists on "
            f"this revision's content ({revision.content_hash})",
        )
    publication = ModelingPublication(
        tenant_id=model.tenant_id,
        model_id=model.id,
        revision_id=revision.id,
        supersedes_revision_id=model.published_revision_id,
        review_id=reviews[0].id,
        validation_run_id=run.id,
        published_by=_sub(tenant_id, subject_id),
    )
    session.add(publication)
    model.published_revision_id = revision.id
    await session.commit()
    await session.refresh(publication)
    return publication


async def list_publications(session: AsyncSession, model_id: UUID) -> list[ModelingPublication]:
    return list(
        (
            await session.execute(
                select(ModelingPublication)
                .where(ModelingPublication.model_id == model_id)
                .order_by(ModelingPublication.published_at.desc(), ModelingPublication.id.desc())
            )
        )
        .scalars()
        .all()
    )
