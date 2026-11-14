"""Service helpers for ``architecture_element_realisation_bindings``.

Owns three concerns:

* ``write_coding_component_binding`` — canonical write hook called from the
  element create/update path. Idempotently materialises a row of kind
  ``coding_component`` from the wire payload's ``properties.code_binding``
  block. The structured table is the single source of truth — the JSONB
  ``code_binding`` block on the wire is request-only and is not persisted on
  the element row (stripped in the router before the entity write).

* ``list_bindings_for_element`` / ``attach_binding`` / ``detach_binding`` —
  the building blocks for the ``/architecture/elements/{eid}/bindings`` CRUD
  endpoints.

* ``aggregate_containers_for_tenant`` / ``container_detail`` — the structured
  reads consumed by ``/architecture/containers`` (list) and
  ``/architecture/containers/{name}`` (validator).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid7

import sqlalchemy.exc
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import (
    ArchitectureElement,
    ArchitectureElementRealisationBinding,
)
from models.modeling import bump_element_models
from schemas.architecture import ArchitectureElementRealisationBindingIn

# ─────────────────────────────────────────────────────────────────────────────
# Canonical write hook (called from element create/update routes)
# ─────────────────────────────────────────────────────────────────────────────


async def write_coding_component_binding(
    session: AsyncSession,
    *,
    element_id: UUID,
    tenant_id: str,
    container: str,
    component_id: str,
    extra_properties: dict[str, Any] | None = None,
) -> None:
    """Idempotently write a ``coding_component`` binding for ``element_id``.

    Called whenever an element write carries a complete request
    ``code_binding.{container, component_id}`` pair. Idempotency comes from
    the partial-unique ``uq_realisation_binding_dedup`` index — the ON
    CONFLICT clause makes re-writing the same key a no-op. ``properties`` is
    overwritten on re-write so a renamed ``path`` / ``kind`` / ``node_id``
    propagates without leaving a stale row.
    """
    payload = {
        "id": uuid7(),
        "element_id": element_id,
        "tenant_id": tenant_id,
        "binding_kind": "coding_component",
        "container": str(container),
        "component_id": str(component_id),
        "repository_url": None,
        "repository_branch": None,
        "properties": dict(extra_properties or {}),
    }
    stmt = pg_insert(ArchitectureElementRealisationBinding).values(payload)
    # The dedup index is partial / uses COALESCE so we can't reference it by
    # column list directly. Postgres still honours ON CONFLICT … ON CONSTRAINT
    # but we don't have a named constraint here — use ON CONFLICT DO NOTHING
    # against the unique index name, then UPDATE the matched row separately if
    # the properties payload should change.
    stmt = stmt.on_conflict_do_nothing(index_elements=None)
    await session.execute(stmt)
    # If the row already existed, refresh ``properties`` so re-writing an
    # element whose path/kind/node_id changed does not leave stale metadata.
    # The dedup-unique tuple is (element_id, kind, container, component_id, '')
    # so we can target it by those four columns.
    await session.execute(
        update(ArchitectureElementRealisationBinding)
        .where(
            ArchitectureElementRealisationBinding.element_id == element_id,
            ArchitectureElementRealisationBinding.binding_kind == "coding_component",
            ArchitectureElementRealisationBinding.container == str(container),
            ArchitectureElementRealisationBinding.component_id == str(component_id),
        )
        .values(properties=dict(extra_properties or {}))
    )
    # Core writes bypass the ORM flush hook; bindings are manifest content of the model.
    await bump_element_models(session, [element_id])


def extract_code_binding_from_payload(
    properties: dict[str, Any] | None,
) -> tuple[str, str, dict[str, Any]] | None:
    """Return ``(container, component_id, extra_properties)`` if the request
    payload carries a complete ``code_binding`` block, else ``None``.

    Looser than ``architecture_upsert.code_binding_upsert_key`` — the upsert
    key requires ``is_synthetic=True`` (the governance-accept path); any
    element create/update whose payload has container + component_id should
    produce a structured binding row.
    """
    binding = (properties or {}).get("code_binding") or {}
    container = binding.get("container")
    component_id = binding.get("component_id")
    if not container or component_id is None or component_id == "":
        return None
    extra = {
        key: binding[key]
        for key in ("path", "kind", "node_id")
        if binding.get(key) is not None and binding.get(key) != ""
    }
    return (str(container), str(component_id), extra)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD helpers for the new /architecture/elements/{eid}/bindings routes
# ─────────────────────────────────────────────────────────────────────────────


async def _load_element(
    session: AsyncSession, element_id: UUID, tenant_id: str
) -> ArchitectureElement | None:
    el = await session.get(ArchitectureElement, element_id)
    if el is None or el.tenant_id != tenant_id:
        return None
    return el


async def list_bindings_for_element(
    session: AsyncSession,
    *,
    element_id: UUID,
    tenant_id: str,
) -> list[ArchitectureElementRealisationBinding]:
    """Return all bindings attached to ``element_id`` for ``tenant_id``."""
    rows = (
        (
            await session.execute(
                select(ArchitectureElementRealisationBinding).where(
                    ArchitectureElementRealisationBinding.element_id == element_id,
                    ArchitectureElementRealisationBinding.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def attach_binding(
    session: AsyncSession,
    *,
    element_id: UUID,
    tenant_id: str,
    payload: ArchitectureElementRealisationBindingIn,
) -> ArchitectureElementRealisationBinding:
    """Insert a new binding row. Idempotent against the dedup unique index —
    re-attaching the same logical binding returns the existing row.
    """
    row = ArchitectureElementRealisationBinding(
        id=uuid7(),
        element_id=element_id,
        tenant_id=tenant_id,
        binding_kind=payload.binding_kind,
        container=payload.container,
        component_id=payload.component_id,
        repository_url=payload.repository_url,
        repository_branch=payload.repository_branch,
        properties=dict(payload.properties or {}),
    )
    session.add(row)
    try:
        await session.flush()
    except sqlalchemy.exc.IntegrityError:
        # The dedup unique index fired — return the pre-existing match so the
        # caller sees attach as idempotent. A transient non-integrity error is
        # deliberately NOT caught here: it must propagate rather than be
        # mis-reported as an idempotent success.
        await session.rollback()
        existing = await _find_existing_match(
            session,
            element_id=element_id,
            tenant_id=tenant_id,
            payload=payload,
        )
        if existing is not None:
            return existing
        raise
    return row


async def _find_existing_match(
    session: AsyncSession,
    *,
    element_id: UUID,
    tenant_id: str,
    payload: ArchitectureElementRealisationBindingIn,
) -> ArchitectureElementRealisationBinding | None:
    stmt = select(ArchitectureElementRealisationBinding).where(
        ArchitectureElementRealisationBinding.element_id == element_id,
        ArchitectureElementRealisationBinding.tenant_id == tenant_id,
        ArchitectureElementRealisationBinding.binding_kind == payload.binding_kind,
    )
    if payload.container is not None:
        stmt = stmt.where(ArchitectureElementRealisationBinding.container == payload.container)
    else:
        stmt = stmt.where(ArchitectureElementRealisationBinding.container.is_(None))
    if payload.component_id is not None:
        stmt = stmt.where(
            ArchitectureElementRealisationBinding.component_id == payload.component_id
        )
    else:
        stmt = stmt.where(ArchitectureElementRealisationBinding.component_id.is_(None))
    if payload.repository_url is not None:
        stmt = stmt.where(
            ArchitectureElementRealisationBinding.repository_url == payload.repository_url
        )
    else:
        stmt = stmt.where(ArchitectureElementRealisationBinding.repository_url.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


async def detach_binding(
    session: AsyncSession,
    *,
    element_id: UUID,
    tenant_id: str,
    binding_id: UUID,
) -> bool:
    """Delete one binding row by id. Returns True if deleted, False if not found.

    Both element-id-tenancy and binding-id-tenancy are checked so a caller in
    tenant A cannot delete a binding whose element belongs to tenant B by
    guessing the binding UUID.
    """
    result = await session.execute(
        delete(ArchitectureElementRealisationBinding).where(
            ArchitectureElementRealisationBinding.id == binding_id,
            ArchitectureElementRealisationBinding.element_id == element_id,
            ArchitectureElementRealisationBinding.tenant_id == tenant_id,
        )
    )
    # SQLAlchemy ``CursorResult`` exposes ``rowcount`` post-DML; ty's generic
    # ``Result`` type doesn't, so go through getattr.
    deleted = int(getattr(result, "rowcount", 0) or 0) > 0
    if deleted:
        # Core DELETE bypasses the ORM flush hook: bump the element's model.
        await bump_element_models(session, [element_id])
    return deleted


# ─────────────────────────────────────────────────────────────────────────────
# Reads used by the containers router
# ─────────────────────────────────────────────────────────────────────────────


async def aggregate_containers_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str,
    element_type: str,
) -> dict[str, dict[str, Any]]:
    """Aggregate distinct ``container`` names reachable from a project's
    effective view, with per-container scope + element count.

    Reads the structured ``architecture_element_realisation_bindings`` table
    joined to the element row for tenant + project-effective-scope filtering.
    Returns a dict keyed by container name with values
    ``{"element_count": int, "has_tenant_scope": bool, "has_project_scope": bool}``.
    """
    per_container: dict[str, dict[str, Any]] = {}
    seen_pairs: set[tuple[str, str]] = set()

    structured = (
        await session.execute(
            select(
                ArchitectureElementRealisationBinding.container,
                ArchitectureElement.project_id,
                ArchitectureElement.id,
            )
            .join(
                ArchitectureElement,
                ArchitectureElement.id == ArchitectureElementRealisationBinding.element_id,
            )
            .where(
                ArchitectureElementRealisationBinding.tenant_id == tenant_id,
                ArchitectureElementRealisationBinding.container.isnot(None),
                ArchitectureElement.element_type == element_type,
                or_(
                    ArchitectureElement.project_id.is_(None),
                    ArchitectureElement.project_id == project_id,
                ),
            )
        )
    ).all()
    for container, row_project_id, element_id in structured:
        if not container:
            continue
        name = str(container)
        pair = (name, str(element_id))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        bucket = per_container.setdefault(
            name,
            {"element_count": 0, "has_tenant_scope": False, "has_project_scope": False},
        )
        bucket["element_count"] = int(bucket["element_count"]) + 1
        if row_project_id is None:
            bucket["has_tenant_scope"] = True
        else:
            bucket["has_project_scope"] = True

    return per_container


async def container_detail(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str,
    container_name: str,
    element_type: str,
) -> dict[str, Any] | None:
    """Return ``{"name", "scope", "element_count", "valid": True}`` for one
    container, or ``None`` if no elements in the project's effective view
    bind it.

    Used by fr's project↔repo provisioning flow as the validator on attach
    (soft-fail) and primary-promote (hard-fail).
    """
    agg = await aggregate_containers_for_tenant(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        element_type=element_type,
    )
    bucket = agg.get(container_name)
    if bucket is None:
        return None
    return {
        "name": container_name,
        "scope": _scope_label(
            has_tenant=bool(bucket["has_tenant_scope"]),
            has_project=bool(bucket["has_project_scope"]),
        ),
        "element_count": int(bucket["element_count"]),
        "valid": True,
    }


def _scope_label(*, has_tenant: bool, has_project: bool) -> str:
    if has_tenant and has_project:
        return "mixed"
    if has_tenant:
        return "tenant"
    return "project"
