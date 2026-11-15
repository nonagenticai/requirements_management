"""Persisted validation runs and findings (target spec §7.3, §7.5).

A run validates either a sealed revision (its stored manifest and ``content_hash``) or the
draft head (the manifest built exactly as a seal would, so a draft run and a seal of the
same content carry the same ``content_hash``). The validator is the SAME pure function the
write gate and the stateless ``…/tools/validate`` routes call:
``tools.architecture.archimate_rules.validate`` or ``tools.uml.rules.validate``.

The model is read in a short transaction (the draft head under ``FOR SHARE``, so no write
interleaves with the read), that transaction ends, the validator runs in the threadpool
(off the event loop) with no database transaction open, and the run and its findings are
stored in a second short transaction.

A SEALED revision above ``MODEL_SYNC_MAX_CONCEPTS`` (and at most
``MODEL_ASYNC_MAX_CONCEPTS``) is validated in the background instead: the request stores a
``queued`` run and returns it (202); an in-process task bounded by ``ASYNC_CONCURRENCY`` marks it
``running``, validates the already-loaded immutable manifest in a worker thread with no
connection held, and writes the findings and the final status in one transaction — the
same findings a synchronous run of the revision would store. The run is owned by this
process boot (``owner_boot_id``); a ``queued``/``running`` run owned by any other boot was
orphaned by a restart and is marked ``failed`` when read (``reap_orphans``). That check is
tenant-scoped on purpose: a startup sweep would need a cross-tenant read, which RLS refuses
(it would see 0 rows in production yet pass in a test suite that bypasses RLS).
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.concurrency import run_in_threadpool

from db import tenant_id_var
from models.modeling import ModelingModel, ModelingValidationFinding, ModelingValidationRun
from repositories.visibility import scope
from schemas.modeling import SEVERITY_RANK, ArchimateSnapshot, RulesetInfo, ValidationReport
from schemas.modeling_lifecycle import ValidationRunIn, ValidationRunOut
from schemas.uml import UmlSnapshot
from services import modeling_models as models_svc
from services.uml_draft import manifest_size
from settings import get_settings
from tools.architecture import archimate_rules
from tools.uml import rules as uml_rules

UNKNOWN_RULESET = "unknown_ruleset"
UNKNOWN_RULE = "unknown_rule"

log = logging.getLogger(__name__)

# This process boot. A queued/running run owned by any other boot is orphaned.
# ⚠️ That assumes ONE process per Deployment: gitops `strategy: Recreate`, `replicas: 1`
# and a single uvicorn worker, so every other boot is a dead one. With more processes
# serving at once, the live runs of another process would be read as orphaned and marked
# failed — the run ownership would then need a liveness check, not a boot id.
BOOT_ID = uuid4()
ACTIVE = ("queued", "running")
ORPHANED = "the process restarted before the background validation run finished"
# At most this many background validations run at once (single replica).
ASYNC_CONCURRENCY = 2
_SLOTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_TASKS: set[asyncio.Task[None]] = set()
# Requests between the queue-capacity check and their task joining ``_TASKS``.
_reserved = 0
QUEUE_FULL = "validation_queue_full"
QUEUE_FULL_RETRY_AFTER_S = 30


def slots() -> asyncio.Semaphore:
    """The running loop's background-validation semaphore (a Semaphore binds to one loop)."""
    loop = asyncio.get_running_loop()
    sem = _SLOTS.get(loop)
    if sem is None:
        sem = _SLOTS[loop] = asyncio.Semaphore(ASYNC_CONCURRENCY)
    return sem


@dataclass(frozen=True)
class Ruleset:
    info: RulesetInfo
    required: frozenset[str]
    validate: Callable[..., ValidationReport]
    snapshot: Callable[[dict[str, Any]], Any]
    size: Callable[[dict[str, Any]], int]

    @property
    def version(self) -> str:
        return str(self.info.reference.get("version") or self.info.language_version)


def _archimate_snapshot(manifest: dict[str, Any]) -> ArchimateSnapshot:
    return models_svc.archimate_snapshot(manifest)


def _uml_snapshot(manifest: dict[str, Any]) -> UmlSnapshot:
    return UmlSnapshot.model_validate(
        {k: manifest.get(k, []) for k in ("elements", "relationships", "interactions", "diagrams")}
    )


def ruleset_for(language: str) -> Ruleset:
    if language == "archimate":
        return Ruleset(
            info=archimate_rules.ruleset_info(with_rules=False),
            required=archimate_rules.REQUIRED_RULES,
            validate=archimate_rules.validate,
            snapshot=_archimate_snapshot,
            size=lambda m: len(m.get("concepts", [])) + len(m.get("relationships", [])),
        )
    return Ruleset(
        info=uml_rules.ruleset_info(with_rules=False),
        required=uml_rules.REQUIRED_RULES,
        validate=uml_rules.validate,
        snapshot=_uml_snapshot,
        size=manifest_size,
    )


def _too_large(total: int, *, revision: bool = False) -> HTTPException:
    settings = get_settings()
    message = (
        f"the target has {total} concepts and relationships; validation runs "
        f"in the background up to MODEL_ASYNC_MAX_CONCEPTS={settings.model_async_max_concepts}"
        if revision
        else f"the target has {total} concepts and relationships; validation is "
        f"synchronous up to MODEL_SYNC_MAX_CONCEPTS={settings.model_sync_max_concepts} "
        "(seal a revision to validate a larger model in the background)"
    )
    return HTTPException(
        status_code=422, detail={"code": models_svc.INPUT_TOO_LARGE, "message": message}
    )


def _unknown_rule(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": UNKNOWN_RULE, "message": str(exc)})


def _check_rules(language: str, body: ValidationRunIn) -> None:
    """Refuse unknown rule ids up front, as the synchronous validator would (422)."""
    try:
        if language == "archimate":
            archimate_rules.resolve_enabled(
                rules=body.rules, disable=body.disable, views_supplied=True
            )
        else:
            uml_rules.resolve_enabled(
                rules=body.rules, disable=body.disable, diagrams_supplied=True
            )
    except (archimate_rules.UnknownRuleError, uml_rules.UnknownRuleError) as exc:
        raise _unknown_rule(exc) from exc


async def _apply_report(
    session: AsyncSession,
    run: ModelingValidationRun,
    report: ValidationReport,
    ruleset: Ruleset,
) -> None:
    """Set a run's verdict from ``report``, flush it and stage its findings.

    Shared by the synchronous and the background path, so both store the same rows.
    """
    run.ruleset_id = report.ruleset_id
    run.enabled_rules = report.enabled_rules
    run.status = report.status
    run.examined = report.examined
    # A run examines the whole target or does not finish: expected == examined.
    run.expected = report.examined
    run.counts = report.counts.model_dump()
    run.clean = report.clean and ruleset.required <= set(report.enabled_rules)
    session.add(run)
    await session.flush()  # the run row exists (and has its id) before its findings
    for seq, f in enumerate(report.findings):
        ids = [t.id for t in f.targets]
        session.add(
            ModelingValidationFinding(
                tenant_id=run.tenant_id,
                run_id=run.id,
                seq=seq,
                rule_id=f.rule_id,
                ruleset_id=f.ruleset_id,
                severity=f.severity,
                severity_rank=SEVERITY_RANK[f.severity],
                targets=[t.model_dump() for t in f.targets],
                target_ids=ids,
                first_target_id=ids[0] if ids else "",
                path=f.path,
                message=f.message,
                evidence=f.evidence,
            )
        )


def run_out(run: ModelingValidationRun) -> ValidationRunOut:
    return ValidationRunOut(
        run_id=run.id,
        model_id=run.model_id,
        target=run.target,  # ty: ignore[invalid-argument-type]
        revision_id=run.revision_id,
        head=run.head,
        content_hash=run.content_hash,
        ruleset_id=run.ruleset_id,
        ruleset_version=run.ruleset_version,
        enabled_rules=list(run.enabled_rules),
        status=run.status,  # ty: ignore[invalid-argument-type]
        examined=dict(run.examined),
        expected=dict(run.expected),
        counts=dict(run.counts),
        clean=run.clean,
        started_at=run.started_at,
        finished_at=run.finished_at,
        requested_by=run.requested_by,
        error=run.error,
    )


async def run_validation(
    session: AsyncSession,
    model_id: UUID,
    body: ValidationRunIn,
    *,
    tenant_id: str,
    subject_id: str,
) -> ModelingValidationRun:
    model = await models_svc.get_model(session, model_id, tenant_id, subject_id)
    ruleset = ruleset_for(model.language)
    if body.ruleset_id is not None and body.ruleset_id != ruleset.info.ruleset_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": UNKNOWN_RULESET,
                "message": (
                    f"a {model.language} model is validated with "
                    f"'{ruleset.info.ruleset_id}', not '{body.ruleset_id}'"
                ),
            },
        )
    limit = get_settings().model_sync_max_concepts
    if body.revision_id is not None:
        revision = await models_svc.get_revision(session, model.id, body.revision_id)
        manifest, content_hash, head = revision.manifest, revision.content_hash, revision.head
        target, revision_id = "revision", revision.id
        size = ruleset.size(manifest)
        if size > get_settings().model_async_max_concepts:
            raise _too_large(size, revision=True)
        if size > limit:
            _check_rules(model.language, body)
            return await _queue_revision_run(
                session,
                model,
                revision,
                ruleset,
                body,
                requested_by=subject_id or tenant_id,
            )
    else:
        # FOR SHARE: a write's head bump (UPDATE) waits for this read to end, and this
        # read waits for an in-flight write to commit — the manifest is one state.
        model = await models_svc.get_model(session, model_id, tenant_id, subject_id, lock="share")
        total = await models_svc.count_concepts(session, model.id)
        if total > limit:
            raise _too_large(total)
        manifest = await models_svc.canonical_manifest(session, model)
        content_hash, head = await models_svc.hash_manifest(manifest), model.head
        target, revision_id = "draft_head", None
    model_pk, model_tenant = model.id, model.tenant_id
    # End the read transaction: validation runs with no transaction (and no lock) open.
    await session.commit()

    started = datetime.now(UTC)
    try:
        # CPU work (up to MODEL_SYNC_MAX_CONCEPTS): in the threadpool, never on the loop.
        report = await run_in_threadpool(
            lambda: ruleset.validate(
                ruleset.snapshot(manifest), rules=body.rules, disable=body.disable
            )
        )
    except (archimate_rules.UnknownRuleError, uml_rules.UnknownRuleError) as exc:
        raise _unknown_rule(exc) from exc
    finished = datetime.now(UTC)

    run = ModelingValidationRun(
        tenant_id=model_tenant,
        model_id=model_pk,
        target=target,
        revision_id=revision_id,
        head=head,
        content_hash=content_hash,
        ruleset_id=report.ruleset_id,
        ruleset_version=ruleset.version,
        required_rules=sorted(ruleset.required),
        requested_by=subject_id or tenant_id,
        started_at=started,
        finished_at=finished,
    )
    await _apply_report(session, run, report, ruleset)
    await session.commit()
    await session.refresh(run)
    return run


# -- background runs (a sealed revision above MODEL_SYNC_MAX_CONCEPTS) --


async def _queue_revision_run(
    session: AsyncSession,
    model: ModelingModel,
    revision: Any,
    ruleset: Ruleset,
    body: ValidationRunIn,
    *,
    requested_by: str,
) -> ModelingValidationRun:
    """Store a ``queued`` run of ``revision`` and start its background task.

    Each queued task holds its manifest in memory, so the queue is bounded too (the
    semaphore bounds only the running ones): beyond MODEL_ASYNC_MAX_QUEUED runs in this
    process, 503 ``validation_queue_full``. A slot is reserved before the first await,
    so concurrent requests cannot all pass the check.
    """
    global _reserved
    cap = get_settings().model_async_max_queued
    if len(_TASKS) + _reserved >= cap:
        raise HTTPException(
            status_code=503,
            detail={
                "code": QUEUE_FULL,
                "message": (
                    f"{cap} background validation runs are already queued or running "
                    f"(MODEL_ASYNC_MAX_QUEUED={cap}); retry later"
                ),
            },
            headers={"Retry-After": str(QUEUE_FULL_RETRY_AFTER_S)},
        )
    _reserved += 1
    try:
        return await _store_and_start(session, model, revision, ruleset, body, requested_by)
    finally:
        _reserved -= 1


async def _store_and_start(
    session: AsyncSession,
    model: ModelingModel,
    revision: Any,
    ruleset: Ruleset,
    body: ValidationRunIn,
    requested_by: str,
) -> ModelingValidationRun:
    manifest = revision.manifest  # immutable: a sealed revision never changes
    run = ModelingValidationRun(
        tenant_id=model.tenant_id,
        model_id=model.id,
        target="revision",
        revision_id=revision.id,
        head=revision.head,
        content_hash=revision.content_hash,
        ruleset_id=ruleset.info.ruleset_id,
        ruleset_version=ruleset.version,
        enabled_rules=[],
        required_rules=sorted(ruleset.required),
        status="queued",
        examined={},
        expected={},
        counts={},
        clean=False,
        requested_by=requested_by,
        owner_boot_id=BOOT_ID,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    engine = session.bind
    assert isinstance(engine, AsyncEngine)
    task = asyncio.create_task(
        _background_run(engine, run.id, tenant_id_var.get(None), manifest, ruleset, body),
        name=f"validation-run-{run.id}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return run


def background_tasks() -> list[asyncio.Task[None]]:
    """The in-flight background validation tasks (cancelled on shutdown)."""
    return list(_TASKS)


def _owned(run_id: UUID, *statuses: str) -> Any:
    return (
        ModelingValidationRun.id == run_id,
        ModelingValidationRun.owner_boot_id == BOOT_ID,
        ModelingValidationRun.status.in_(statuses),
    )


async def _background_run(
    engine: AsyncEngine,
    run_id: UUID,
    tenant_id: str | None,
    manifest: dict[str, Any],
    ruleset: Ruleset,
    body: ValidationRunIn,
) -> None:
    # RLS: the GUC is bound at transaction-begin from tenant_id_var; bind it explicitly
    # (never rely on the request's context surviving) or every statement sees 0 rows.
    tenant_id_var.set(tenant_id)
    try:
        async with slots():
            async with AsyncSession(engine, expire_on_commit=False) as session:
                started = await session.execute(
                    update(ModelingValidationRun)
                    .where(*_owned(run_id, "queued"))
                    .values(status="running", started_at=datetime.now(UTC))
                )
                await session.commit()
            if started.rowcount != 1:  # ty: ignore[missing-attribute]
                return  # reaped as orphaned meanwhile
            # CPU work, no connection held: the manifest is already in memory.
            report = await asyncio.to_thread(
                lambda: ruleset.validate(
                    ruleset.snapshot(manifest), rules=body.rules, disable=body.disable
                )
            )
            async with AsyncSession(engine, expire_on_commit=False) as session:
                run = (
                    await session.execute(
                        select(ModelingValidationRun)
                        .where(*_owned(run_id, "running"))
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None:
                    return
                await _apply_report(session, run, report, ruleset)
                run.finished_at = datetime.now(UTC)
                await session.commit()
    except BaseException as exc:
        log.exception("validation_run.background_failed run_id=%s", run_id)
        await _fail(engine, run_id, f"{type(exc).__name__}: {exc}"[:1000])
        if not isinstance(exc, Exception):
            raise


async def _fail(engine: AsyncEngine, run_id: UUID, reason: str) -> None:
    try:
        async with AsyncSession(engine) as session:
            await session.execute(
                update(ModelingValidationRun)
                .where(*_owned(run_id, *ACTIVE))
                .values(status="failed", error=reason, finished_at=datetime.now(UTC))
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - the next boot reaps it as orphaned
        log.exception("validation_run.mark_failed_failed run_id=%s", run_id)


async def reap_orphans(
    session: AsyncSession, *, model_id: UUID | None = None, run_id: UUID | None = None
) -> None:
    """Mark ``queued``/``running`` runs not owned by this boot ``failed``.

    Runs in its own short transaction on the caller's engine, so it never commits a
    caller's pending work. Tenant-scoped like every read (RLS GUC from tenant_id_var).
    """
    cond = [
        ModelingValidationRun.status.in_(ACTIVE),
        or_(
            ModelingValidationRun.owner_boot_id.is_(None),
            ModelingValidationRun.owner_boot_id != BOOT_ID,
        ),
    ]
    if model_id is not None:
        cond.append(ModelingValidationRun.model_id == model_id)
    if run_id is not None:
        cond.append(ModelingValidationRun.id == run_id)
    engine = session.bind
    assert isinstance(engine, AsyncEngine)
    async with AsyncSession(engine) as own:
        await own.execute(
            update(ModelingValidationRun)
            .where(*cond)
            .values(status="failed", error=ORPHANED, finished_at=datetime.now(UTC))
        )
        await own.commit()


def visible_model_ids(tenant_id: str, subject_id: str) -> Any:
    """Subquery: ids of the models the caller can see."""
    return scope(
        select(ModelingModel.id),
        ModelingModel,
        tenant_id=tenant_id,
        subject_id=subject_id or tenant_id,
    ).scalar_subquery()


async def get_run(
    session: AsyncSession, run_id: UUID, tenant_id: str, subject_id: str
) -> ModelingValidationRun:
    """A run of a model the caller can see, else 404 (never another subject's)."""
    row = (
        await session.execute(
            select(ModelingValidationRun).where(
                ModelingValidationRun.id == run_id,
                ModelingValidationRun.model_id.in_(visible_model_ids(tenant_id, subject_id)),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="validation run not found")
    if row.status in ACTIVE and row.owner_boot_id != BOOT_ID:
        await reap_orphans(session, run_id=row.id)
        await session.refresh(row)
    return row


async def list_runs(
    session: AsyncSession,
    model_id: UUID,
    *,
    target: str | None,
    clean: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[ModelingValidationRun], int]:
    await reap_orphans(session, model_id=model_id)
    stmt = select(ModelingValidationRun).where(ModelingValidationRun.model_id == model_id)
    if target:
        stmt = stmt.where(ModelingValidationRun.target == target)
    if clean is not None:
        stmt = stmt.where(ModelingValidationRun.clean.is_(clean))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(
                    ModelingValidationRun.started_at.desc(), ModelingValidationRun.id.desc()
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def latest_run_summary(session: AsyncSession, model_id: UUID) -> dict[str, Any] | None:
    rows, _ = await list_runs(session, model_id, target=None, clean=None, limit=1, offset=0)
    if not rows:
        return None
    run = rows[0]
    return {
        "run_id": str(run.id),
        "target": run.target,
        "revision_id": None if run.revision_id is None else str(run.revision_id),
        "head": run.head,
        "content_hash": run.content_hash,
        "ruleset_id": run.ruleset_id,
        "status": run.status,
        "clean": run.clean,
        "counts": run.counts,
        "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
    }


async def list_findings(
    session: AsyncSession,
    run_id: UUID,
    *,
    severity: str | None,
    rule_id: str | None,
    target_id: str | None,
    limit: int,
    offset: int,
) -> tuple[list[ModelingValidationFinding], int]:
    """Ordered by (severity desc, rule_id, first target id) — spec §7.3."""
    stmt = select(ModelingValidationFinding).where(ModelingValidationFinding.run_id == run_id)
    if severity:
        stmt = stmt.where(ModelingValidationFinding.severity == severity)
    if rule_id:
        stmt = stmt.where(ModelingValidationFinding.rule_id == rule_id)
    if target_id:
        stmt = stmt.where(ModelingValidationFinding.target_ids.contains([target_id]))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(
                    ModelingValidationFinding.severity_rank,
                    ModelingValidationFinding.rule_id,
                    ModelingValidationFinding.first_target_id,
                    ModelingValidationFinding.seq,
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total
