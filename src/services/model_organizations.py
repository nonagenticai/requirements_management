"""A model's folder tree (Open Group ``organizations``; Archi folders) — target spec §8.1.

Stored as JSONB on ``modeling_models.organizations`` in the snapshot shape
(``[{id, name, documentation, items[], children[]}]``; NULL = no tree supplied). It is
model content: the revision manifest carries it, and every write — ``PUT
/modeling/models/{model_id}/organizations`` or a change set's ``organization`` op —
bumps the model's head under the model lock.

Items are concept, relationship or view ids of the model. A write naming anything else is
refused (``422 model_integrity``, rule ``AM-REF-001``). An item whose row is later deleted
(by any path: route, cascade, batch, model move) is dropped whenever the tree is READ —
manifest, GET, export — so a deleted concept never appears in a folder, and the write that
deleted it already bumped the head. Writes also store the pruned tree.

Sibling folders and items keep the order they were written in — in the canonical
manifest (``organizations`` / ``children`` are order-significant keys, spec §7.2), in GET
and in export — so a reorder is a change of model content.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureElement, ArchitectureRelationship, ArchitectureView
from models.modeling import ModelingModel, bump_models
from schemas.modeling import ArchimateOrganization
from services.model_integrity import integrity_error, single_finding
from tools.architecture.registry import get_registry

_TREE = TypeAdapter(list[ArchimateOrganization] | None)


def parse_tree(raw: Any) -> list[dict[str, Any]] | None:
    """Validate a tree (raises ``ValidationError``) and return its JSON form."""
    tree = _TREE.validate_python(raw)
    return None if tree is None else [f.model_dump(mode="json") for f in tree]


def walk(tree: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    stack = list(tree or [])
    while stack:
        folder = stack.pop()
        out.append(folder)
        stack.extend(folder.get("children") or [])
    return out


def item_ids(tree: list[dict[str, Any]] | None) -> list[str]:
    return [i for f in walk(tree) for i in f.get("items") or []]


def prune(tree: list[dict[str, Any]] | None, keep: set[Any]) -> list[dict[str, Any]] | None:
    """The tree with every item not in ``keep`` removed (folders stay)."""
    if tree is None:
        return None

    def one(folder: dict[str, Any]) -> dict[str, Any]:
        return {
            **folder,
            "items": [i for i in folder.get("items") or [] if i in keep],
            "children": [one(c) for c in folder.get("children") or []],
        }

    return [one(f) for f in tree]


def substitute(tree: list[dict[str, Any]] | None, id_map: dict[str, str]) -> Any:
    """Items renamed through ``id_map`` (a change set's local references → row ids)."""
    if tree is None:
        return None

    def one(folder: dict[str, Any]) -> dict[str, Any]:
        return {
            **folder,
            "items": [id_map.get(i, i) for i in folder.get("items") or []],
            "children": [one(c) for c in folder.get("children") or []],
        }

    return [one(f) for f in tree]


async def model_content_ids(session: AsyncSession, model: ModelingModel) -> set[str]:
    """Ids of the model's concepts, relationships and views — of the model's own tenant."""
    ids: set[str] = set()
    for cls in (ArchitectureElement, ArchitectureRelationship, ArchitectureView):
        ids |= {
            str(i)
            for i in (
                await session.execute(
                    select(cls.id).where(cls.model_id == model.id, cls.tenant_id == model.tenant_id)
                )
            ).scalars()
        }
    return ids


def unresolved(tree: list[dict[str, Any]] | None, known: set[str]) -> list[str]:
    return sorted({i for i in item_ids(tree) if i not in known})


def unresolved_error(missing: list[str]) -> HTTPException:
    return integrity_error(
        [
            single_finding(
                "AM-REF-001",
                get_registry().ruleset_id,
                f"folder items do not resolve to a concept, relationship or view of the "
                f"model: {missing[:20]}",
                path="/organizations",
                evidence={"unresolved": missing[:100]},
            )
        ]
    )


def invalid_tree(exc: ValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "invalid_organizations", "message": str(exc)},
    )


async def write_tree(
    session: AsyncSession, model: ModelingModel, tree: list[dict[str, Any]] | None
) -> None:
    """Store a validated, resolved tree on a LOCKED model and bump its head (no commit)."""
    known = await model_content_ids(session, model)
    missing = unresolved(tree, known)
    if missing:
        raise unresolved_error(missing)
    model.organizations = tree
    await session.flush()
    await bump_models(session, {model.id})


UNSUPPORTED_LANGUAGE = "organizations_unsupported_language"


def _archimate_only(model: ModelingModel) -> None:
    if model.language != "archimate":
        raise HTTPException(
            status_code=409,
            detail={
                "code": UNSUPPORTED_LANGUAGE,
                "message": f"folder trees belong to ArchiMate models; this is a {model.language} model",
            },
        )


async def read_tree(
    session: AsyncSession, model_id: UUID, tenant_id: str, subject_id: str
) -> tuple[list[dict[str, Any]] | None, int]:
    """(pruned tree, head) of a model the caller can see."""
    from services import modeling_models as models_svc  # noqa: PLC0415 — import cycle

    model = await models_svc.get_model(session, model_id, tenant_id, subject_id, lock="share")
    _archimate_only(model)
    tree = prune(model.organizations, await model_content_ids(session, model))
    head = model.head
    await session.commit()
    return tree, head


async def replace_tree(
    session: AsyncSession,
    model_id: UUID,
    raw: list[Any] | None,
    *,
    tenant_id: str,
    subject_id: str,
    if_match: int | None,
) -> tuple[list[dict[str, Any]] | None, int]:
    """Replace a writable model's tree under its lock; If-Match is the expected head."""
    from services import modeling_models as models_svc  # noqa: PLC0415 — import cycle

    model = await models_svc.get_mutable_model(session, model_id, tenant_id, subject_id, lock=True)
    _archimate_only(model)
    if if_match is not None and model.head != if_match:
        raise models_svc.stale_head(model.head)
    tree = parse_tree(raw)
    await write_tree(session, model, tree)
    await session.commit()
    await session.refresh(model)
    return tree, model.head
