"""Upsert support for synthetic Artifacts bound to coding-analysis code units.

When fr's Architecture Governance review queue (P4.4) accepts a proposal, it
POSTs to ``/architecture/elements`` with ``is_synthetic=true`` and
``properties.code_binding.{container, component_id}`` set. This module
provides the lookup that turns those POSTs into idempotent upserts on
``(tenant_id, project_id, container, component_id)`` — keyed against the
structured ``architecture_element_realisation_bindings`` table — so
re-accepting the same proposal does not duplicate the Artifact row.

Contract: ``docs/ARCHITECTURE_PROPOSALS_CONTRACT.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import sqlalchemy.exc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureElement, ArchitectureElementRealisationBinding
from repositories.hierarchy import assert_parent_acyclic
from repositories.visibility import scope
from schemas.architecture import ArchitectureElementIn
from services.archimate_integrity import assert_retype_keeps_relationships_valid
from services.slug_retry import (
    MAX_SLUG_RETRIES,
    find_slug_constraint_name,
    is_fatal_non_slug_violation,
)

if TYPE_CHECKING:
    from services.entity_service import EntityService

# Cached OPTIONAL fast-path for the concurrent-slug-collision recovery below:
# the ArchitectureElement (tenant_id, slug) UniqueConstraint name
# (uq_arch_elements_tenant_slug). Module-level because these are free functions,
# not a class — do NOT reach into the injected ``svc``'s private attribute.
_ARCH_ELEMENT_SLUG_CONSTRAINT = find_slug_constraint_name(ArchitectureElement)


def code_binding_upsert_key(payload: ArchitectureElementIn) -> tuple[str, str] | None:
    """Return ``(container, component_id)`` when the payload qualifies for upsert, else ``None``.

    A payload qualifies when ``is_synthetic`` is true AND
    ``properties.code_binding.container`` and
    ``properties.code_binding.component_id`` are both present and non-empty.
    The payload JSONB is the request-side wire format; the persisted source
    of truth is the structured ``architecture_element_realisation_bindings``
    table (``find_synthetic_by_code_binding`` reads from there).
    """
    if not getattr(payload, "is_synthetic", False):
        return None
    binding = (payload.properties or {}).get("code_binding") or {}
    container = binding.get("container")
    component_id = binding.get("component_id")
    if not container or component_id is None or component_id == "":
        return None
    return (str(container), str(component_id))


async def find_synthetic_by_code_binding(
    session: AsyncSession,
    *,
    tenant_id: str,
    subject_id: str,
    project_id: str | None,
    container: str,
    component_id: str,
) -> ArchitectureElement | None:
    """Find an existing synthetic Artifact matching the upsert key.

    Reads from the structured ``architecture_element_realisation_bindings``
    table (kind ``coding_component``) joined on ``element_id``. Project scoping
    mirrors the create path on the element row: a payload with
    ``project_id=None`` matches rows that also have a NULL ``project_id``;
    otherwise the values must be equal.

    Visibility scoping (via ``repositories.visibility.scope``) narrows the
    lookup to rows the caller can actually see — all tenant-visible rows plus
    the caller's own user-visible rows. Without it, a re-accept by a different
    subject could resolve to ANOTHER subject's user-visible row, and the
    downstream ``svc.update`` (which re-checks ownership) would then 404.

    The final fetch is a deterministic ordered single row (mirrors the FIX-2
    idiom in ``entity_service.get_or_create``): the realisation-binding dedup
    index is keyed per ``element_id``, so two distinct synthetic elements can
    each hold a ``coding_component`` binding on the same
    ``(container, component_id)``. A bare ``scalar_one_or_none`` would then raise
    ``MultipleResultsFound`` (a sqlalchemy InvalidRequestError, NOT an
    IntegrityError → uncaught → HTTP 500). ``order_by(...).limit(1).first()``
    is total and returns the earliest-created row deterministically.
    """
    query = (
        select(ArchitectureElement)
        .join(
            ArchitectureElementRealisationBinding,
            ArchitectureElementRealisationBinding.element_id == ArchitectureElement.id,
        )
        .where(
            ArchitectureElement.tenant_id == tenant_id,
            ArchitectureElement.is_synthetic.is_(True),
            ArchitectureElementRealisationBinding.binding_kind == "coding_component",
            ArchitectureElementRealisationBinding.container == container,
            ArchitectureElementRealisationBinding.component_id == component_id,
        )
    )
    # Resolve subject before scoping: the create path stores a user-visible row's
    # subject_id as ``subject_id or tenant_id`` (the ``_autofill_subject_id``
    # before_insert listener maps a trusted-network ``""`` → ``tenant_id``), and
    # every other ``scope()`` call site resolves first. Passing the raw ``""``
    # here would make a user-visible synthetic re-accept miss its own row and mint
    # a duplicate. Tenant-visible rows (the AA-intake default) ignore subject_id.
    query = scope(
        query, ArchitectureElement, tenant_id=tenant_id, subject_id=subject_id or tenant_id
    )
    if project_id is None:
        query = query.where(ArchitectureElement.project_id.is_(None))
    else:
        query = query.where(ArchitectureElement.project_id == project_id)
    query = query.order_by(ArchitectureElement.created_at, ArchitectureElement.id).limit(1)
    return (await session.execute(query)).scalars().first()


async def find_synthetic_by_identity_columns(
    session: AsyncSession,
    *,
    tenant_id: str,
    subject_id: str,
    project_id: str | None,
    container: str,
    component_id: str,
) -> ArchitectureElement | None:
    """Find a synthetic Artifact by its denormalised identity columns on the
    element row (``synthetic_binding_container`` / ``synthetic_binding_component_id``).

    Used by the IntegrityError recovery branch of the upsert. The race winner
    commits its ELEMENT (carrying the identity columns — this is what fires the
    ``uq_arch_elements_synthetic_binding_*`` index) inside ``svc.create``, but
    (without an ``Idempotency-Key``) writes its binding row in a SEPARATE later commit. So at the instant the
    losing racer catches the unique violation, the winner's binding row may not
    be committed yet — a binding-table lookup could miss it. Reading the element's
    own identity columns is total against that ordering hazard.

    Same visibility/project scoping as ``find_synthetic_by_code_binding`` so the
    recovery only resolves rows the caller can actually mutate.
    """
    query = select(ArchitectureElement).where(
        ArchitectureElement.tenant_id == tenant_id,
        ArchitectureElement.is_synthetic.is_(True),
        ArchitectureElement.synthetic_binding_container == container,
        ArchitectureElement.synthetic_binding_component_id == component_id,
    )
    query = scope(
        query, ArchitectureElement, tenant_id=tenant_id, subject_id=subject_id or tenant_id
    )
    if project_id is None:
        query = query.where(ArchitectureElement.project_id.is_(None))
    else:
        query = query.where(ArchitectureElement.project_id == project_id)
    query = query.order_by(ArchitectureElement.created_at, ArchitectureElement.id).limit(1)
    return (await session.execute(query)).scalars().first()


async def _assert_update_keeps_tree_and_types(
    session: AsyncSession,
    *,
    row: ArchitectureElement,
    payload: ArchitectureElementIn,
    tenant_id: str,
    subject_id: str,
) -> None:
    """The checks the regular PUT runs, with the RESOLVED row's id.

    The route's pre-branch ``assert_parent_acyclic`` runs with ``element_id=None``
    (it cannot know the row yet), which misses re-parenting an existing synthetic
    under its own descendant. Keyed by code binding, not type, this update can
    also change ``element_type`` — re-check the element's relationships (A06).
    """
    await assert_parent_acyclic(
        session,
        model=ArchitectureElement,
        tenant_id=tenant_id,
        element_id=row.id,
        parent_id=payload.parent_id,
    )
    await assert_retype_keeps_relationships_valid(
        session,
        element_id=row.id,
        new_type=payload.element_type,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )


def _bind(
    hook: Callable[[Any, bool], Awaitable[None]] | None, *, created: bool
) -> Callable[[Any], Awaitable[None]] | None:
    """The service-level ``before_commit`` for one branch of the upsert."""
    if hook is None:
        return None

    async def bound(out: Any) -> None:
        await hook(out, created)

    return bound


async def upsert_synthetic_by_code_binding(
    session: AsyncSession,
    *,
    svc: "EntityService",
    payload: ArchitectureElementIn,
    tenant_id: str,
    subject_id: str,
    visibility: str,
    project_id: str | None,
    container: str,
    component_id: str,
    before_commit: Callable[[Any, bool], Awaitable[None]] | None = None,
    reclaim: Callable[[], Awaitable[None]] | None = None,
    model_id: Any = None,
) -> tuple[Any, bool]:
    """Idempotent synthetic-Artifact upsert with a DB-race backstop.

    ``model_id`` (validated by the route) places a NEW synthetic in that model; ``None``
    lets the flush hook assign the scope's default model. An existing synthetic keeps its
    model.

    Returns ``(result, created)`` — ``created=True`` → 201 (fresh insert),
    ``False`` → 200 (updated an existing synthetic).

    ``before_commit(out, created)`` is handed to whichever write wins (as the service's
    ``before_commit``), so it runs exactly once, inside that write's single transaction.

    ``reclaim`` (an ``Idempotency-Key`` route's ``Idempotency.reclaim``) is awaited right
    after the recovery ``rollback`` — which also discards the uncommitted claim — and
    BEFORE the survivor is read or updated (or the slug retried), so a same-key request
    can never hold the claim while this one holds the survivor's row lock (40P01).

    1. Fast path: ``find_synthetic_by_code_binding`` (binding table). A sequential
       re-accept finds the prior synthetic → ``svc.update`` in place. This keeps
       the pre-migration behaviour for synthetics whose ``coding_component``
       binding was attached via the CRUD surface (identity columns unset).
    2. Otherwise optimistically ``svc.create`` with the identity columns set
       (server-controlled ``extra_columns`` — never client payload). Under a
       concurrent double-mint the second insert violates the
       ``uq_arch_elements_synthetic_binding_*`` index → ``IntegrityError``; we
       roll back and recover the winner via its committed identity columns
       (mirrors ``EntityService.get_or_create`` / ``realisation_bindings.attach_binding``).

    The optimistic create is wrapped in a bounded slug-regeneration retry: two
    concurrent synthetics with the SAME title but a DIFFERENT ``component_id``
    derive the same title-slug base (synthetics are excluded from the title
    identity index but NOT from ``uq_arch_elements_tenant_slug``). The loser's
    identity-column re-SELECT keys on THIS caller's component_id → finds nothing
    → without recovery it re-raises → 500. Regenerating the slug on a fresh
    ``svc.create`` (which sees the committed winner's slug and appends a suffix)
    and retrying lands both rows.
    """
    existing = await find_synthetic_by_code_binding(
        session,
        tenant_id=tenant_id,
        subject_id=subject_id,
        project_id=project_id,
        container=container,
        component_id=component_id,
    )
    if existing is not None:
        await _assert_update_keeps_tree_and_types(
            session, row=existing, payload=payload, tenant_id=tenant_id, subject_id=subject_id
        )
        result = await svc.update(
            session,
            existing.id,
            payload,
            tenant_id,
            subject_id,
            before_commit=_bind(before_commit, created=False),
        )
        return result, False

    # The fast-path lookup above stays OUTSIDE the loop: a slug collision is with
    # a DIFFERENT binding, so re-running it on retry would still miss.
    for attempt in range(MAX_SLUG_RETRIES + 1):
        try:
            result = await svc.create(
                session,
                payload,
                tenant_id,
                subject_id,
                visibility=visibility,
                extra_columns={
                    "synthetic_binding_container": container,
                    "synthetic_binding_component_id": component_id,
                    **({"model_id": model_id} if model_id is not None else {}),
                },
                before_commit=_bind(before_commit, created=True),
            )
            return result, True
        except sqlalchemy.exc.IntegrityError as e:
            # Lost the concurrent double-mint race. Recover the survivor by its
            # committed element identity columns (the winner's binding row may
            # not be committed yet — see find_synthetic_by_identity_columns).
            await session.rollback()
            if reclaim is not None:
                await reclaim()
            survivor = await find_synthetic_by_identity_columns(
                session,
                tenant_id=tenant_id,
                subject_id=subject_id,
                project_id=project_id,
                container=container,
                component_id=component_id,
            )
            if survivor is None:
                # Empty identity re-SELECT → not our synthetic-binding dup. The
                # benign cause is a same-title / different-component slug
                # collision on uq_arch_elements_tenant_slug; regenerate + retry.
                # A genuinely-fatal other constraint (or bound exhaustion)
                # surfaces rather than mis-reporting a transient failure as an
                # idempotent success.
                if is_fatal_non_slug_violation(e, _ARCH_ELEMENT_SLUG_CONSTRAINT):
                    raise
                if attempt < MAX_SLUG_RETRIES:
                    continue
                raise
            # Same checks as the fast path above, with the survivor's id.
            await _assert_update_keeps_tree_and_types(
                session, row=survivor, payload=payload, tenant_id=tenant_id, subject_id=subject_id
            )
            result = await svc.update(
                session,
                survivor.id,
                payload,
                tenant_id,
                subject_id,
                before_commit=_bind(before_commit, created=False),
            )
            return result, False
    # Unreachable: the loop either returns or the final attempt re-raises.
    raise AssertionError("upsert_synthetic_by_code_binding retry loop exited without return")
