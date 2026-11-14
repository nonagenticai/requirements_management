"""Change sets: proposed model changes, validated in memory, applied atomically (spec §9.5).

Create
    The ops run against an in-memory copy of the draft head (its manifest, read under
    ``FOR SHARE``). The result is checked by the SAME pure validator as every write:
    findings are the full ruleset on the proposed state; the set is ``invalid`` when an
    op cannot be applied (``CS-OP-001``: unknown id, attribute that does not validate;
    ``CS-OP-002``: a relationship identical — type, source, target — to another one of a
    shared project model, which the database's edge identity index refuses) or when it
    introduces an error-severity finding the head does not have (the write gate's
    before/after rule, over every rule). Nothing is written to the model.

Apply
    Only a ``proposed`` set, and only while the model's head still equals ``base_head``
    (``412 stale_head``; the caller re-proposes on the new head). The ops run through the
    normal write services — the same gates as the HTTP routes — inside ONE
    caller-owned transaction (``services/unit_of_work.py``: the services' commit points
    only flush), so a failing op rolls every earlier op back and the set stays
    ``proposed``. The head is bumped once. The set is re-read under ``FOR UPDATE`` after
    the model lock, so a concurrent apply or reject (which takes the same two locks in the
    same order) can never be overwritten: the loser sees the winner's status (409). A
    database constraint an op still trips is ``422 model_integrity`` (``CS-OP-003``), never
    a 500; its ``path`` is ``/ops/<i>`` of the op being applied, or ``null`` for a UML set
    (persisted in one write after all ops ran in memory, so no single op is to blame).

``applied_by`` / ``rejected_by`` / ``created_by`` come from the principal, never the body.

A ``create`` op's ``id`` is a local reference: later ops may use it wherever an id is
expected, and ``id_map`` (returned on apply) maps it to the real id.

A UML model takes the UML op kinds, evaluated and applied by ``services/uml_change_sets.py``
with the same contract; an op of the other language's kinds is ``CS-OP-001``.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from models.architecture import ArchitectureElement, ArchitectureRelationship, ArchitectureView
from models.modeling import (
    HEADS,
    ArchitectureViewConnection,
    ArchitectureViewObject,
    ModelingChangeSet,
    bump_models,
)
from repositories.hierarchy import assert_parent_acyclic
from schemas.architecture import (
    ArchitectureElementIn,
    ArchitectureRelationshipIn,
    ArchitectureViewIn,
)
from schemas.modeling import (
    ArchimateExchangeContent,
    ArchimateSnapshot,
    ArchimateViewConnectionIn,
    ArchimateViewObjectIn,
    Finding,
)
from schemas.modeling_lifecycle import ChangeOp, ChangeSetIn, ViewConnectionIn, ViewObjectIn
from services import architecture_element_service as element_svc
from services import architecture_relationship_service as relationship_svc
from services import architecture_view_service as view_svc
from services import model_organizations as organizations
from services import modeling_models as models_svc
from services import uml_change_sets as uml_cs
from services import view_occurrences as occ
from services.archimate_integrity import (
    WRITE_TIME_RULES,
    _proposed_attributes,
    assert_parent_resolves,
    assert_retype_keeps_relationships_valid,
    introduced_errors,
)
from services.model_integrity import integrity_error
from services.modeling_validation import visible_model_ids
from services.unit_of_work import caller_owned_transaction
from services.view_occurrences import OCCURRENCE_RULES
from tools.architecture.archimate_rules import RULES
from tools.architecture.archimate_rules import validate as validate_archimate
from tools.architecture.exchange import exchange_content_errors

CHANGE_SET_RULESET = "modeling/change-sets"
NOT_PROPOSED = "change_set_not_proposed"
NOT_OPEN = "change_set_not_open"
OP_INVALID = "change_set_op_invalid"
# Rule ids of the ``modeling/change-sets`` ruleset (spec §9.5).
OP_UNAPPLICABLE = "CS-OP-001"  # an op cannot be applied
OP_DUPLICATE_EDGE = "CS-OP-002"  # duplicate relationship identity in a shared project model
OP_CONSTRAINT = "CS-OP-003"  # apply tripped a database constraint
CHANGE_SET_RULES = (OP_UNAPPLICABLE, OP_DUPLICATE_EDGE, OP_CONSTRAINT)
_ALL_RULES = tuple(r.rule_id for r in RULES)
# What the write gates check per write (relationships / retypes; view occurrences).
_GATE_RULES = WRITE_TIME_RULES + OCCURRENCE_RULES
_GATED_KINDS = frozenset({"element", "relationship", "view_object", "view_connection"})

_ALLOWED: dict[str, frozenset[str]] = {
    "element": frozenset(
        {
            "title",
            "element_type",
            "layer",
            "description",
            "properties",
            "parent_id",
            "status",
            "architecture_state",
        }
    ),
    "relationship": frozenset(
        {"relationship_type", "source_id", "target_id", "name", "description", "properties"}
    ),
    "view": frozenset({"title", "viewpoint", "viewpoint_ref", "description"}),
    # The whole folder tree (an update with no id); items may be local references.
    "organization": frozenset({"organizations"}),
    # The model's exchange content (an update with no id): ArchimateExchangeContent.
    "model_exchange": frozenset({"exchange"}),
    "view_object": frozenset(
        {
            "view_id",
            "kind",
            "concept_ref",
            "parent_object_id",
            "bounds",
            "style",
            "name",
            "text",
            "label",
            "border_type",
            "documentation",
            "view_ref",
            "position",  # z-order: index in the view's object list (moves the object)
        }
    ),
    "view_connection": frozenset(
        {
            "view_id",
            "kind",
            "relationship_ref",
            "source_object_id",
            "target_object_id",
            "bendpoints",
            "style",
            "label",
        }
    ),
}
# Attributes whose value is an id (and so may be a create op's local reference).
_REF_KEYS = frozenset(
    {
        "parent_id",
        "source_id",
        "target_id",
        "view_id",
        "concept_ref",
        "parent_object_id",
        "relationship_ref",
        "source_object_id",
        "target_object_id",
        "view_ref",
    }
)


def _sub(tenant_id: str, subject_id: str) -> str:
    return subject_id or tenant_id


class _OpError(Exception):
    def __init__(self, findings: list[Finding]) -> None:
        super().__init__(findings[0].message if findings else "op error")
        self.findings = findings


def _op_error(
    index: int | None, message: str, *, rule_id: str = OP_UNAPPLICABLE, **evidence: Any
) -> _OpError:
    return _OpError(
        [
            Finding(
                rule_id=rule_id,
                ruleset_id=CHANGE_SET_RULESET,
                severity="error",
                targets=[],
                path=None if index is None else f"/ops/{index}",
                message=message,
                evidence=evidence or None,
            )
        ]
    )


# Same bound as ``ViewObjectIn.position`` (le=100000), so an op the apply would refuse is
# already ``invalid`` at propose.
MAX_POSITION = 100000


def _moved(items: dict[str, Any], key: str, position: int) -> dict[str, Any]:
    """``items`` (ordered) with ``key`` moved to index ``position`` (clamped)."""
    order = [k for k in items if k != key]
    order.insert(min(position, len(order)), key)
    return {k: items[k] for k in order}


def _validated(schema: type[BaseModel], data: dict[str, Any], index: int) -> Any:
    """``schema(**data)``; a validation failure becomes the op's findings."""
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        findings = []
        for err in exc.errors():
            ctx = err.get("ctx") or {}
            if err.get("type") == "model_integrity" and "finding" in ctx:
                f = Finding.model_validate(ctx["finding"])
                f.path = f"/ops/{index}/attributes{f.path or ''}"
                findings.append(f)
            else:
                loc = "/".join(str(p) for p in err.get("loc", ()))
                findings.append(_op_error(index, f"attributes/{loc}: {err.get('msg')}").findings[0])
        raise _OpError(findings) from exc


def _pairs(props: Any) -> list[list[str]] | None:
    return models_svc._property_pairs(props)  # noqa: SLF001 — same rendering as manifests


# ---------------------------------------------------------------------------
# In-memory model
# ---------------------------------------------------------------------------


class _Working:
    """The draft head as mutable dicts keyed by id, in the manifest's snapshot shape."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.concepts = {c["id"]: dict(c) for c in manifest.get("concepts", [])}
        self.relationships = {r["id"]: dict(r) for r in manifest.get("relationships", [])}
        self.views: dict[str, dict[str, Any]] = {}
        for v in manifest.get("views", []):
            view = {k: val for k, val in v.items() if k not in ("objects", "connections")}
            view["objects"] = {o["id"]: dict(o) for o in v.get("objects", [])}
            view["connections"] = {c["id"]: dict(c) for c in v.get("connections", [])}
            self.views[v["id"]] = view
        self.organizations = manifest.get("organizations")
        self.exchange = manifest.get("exchange")

    def viewpoint_ids(self) -> set[str]:
        return {vp["id"] for vp in (self.exchange or {}).get("viewpoints") or []}

    def snapshot(self) -> ArchimateSnapshot:
        return models_svc.archimate_snapshot(
            {
                "concepts": list(self.concepts.values()),
                "relationships": list(self.relationships.values()),
                "views": [
                    {
                        **{k: v for k, v in view.items() if k not in ("objects", "connections")},
                        "objects": list(view["objects"].values()),
                        "connections": list(view["connections"].values()),
                    }
                    for view in self.views.values()
                ],
            }
        )

    def known(self, ref: str) -> bool:
        return (
            ref in self.concepts
            or ref in self.relationships
            or ref in self.views
            or self.object_view(ref) is not None
            or self.connection_view(ref) is not None
        )

    def object_view(self, ref: str) -> dict[str, Any] | None:
        return next((v for v in self.views.values() if ref in v["objects"]), None)

    def connection_view(self, ref: str) -> dict[str, Any] | None:
        return next((v for v in self.views.values() if ref in v["connections"]), None)

    def drop_relationships(self, ids: set[str]) -> None:
        """Delete relationships and, recursively, those ending on them (Archi semantics)."""
        while ids:
            for rid in ids:
                self.relationships.pop(rid, None)
            ids = {
                rid
                for rid, r in self.relationships.items()
                if r["source"] in ids or r["target"] in ids
            }

    def prune(self) -> None:
        """Mirror the database cascades: nested objects, dangling connections."""
        for view in self.views.values():
            objs = view["objects"]
            changed = True
            while changed:
                changed = False
                for oid, o in list(objs.items()):
                    shows_missing = o.get("kind", "element") == "element" and (
                        o["concept_id"] not in self.concepts
                    )
                    if shows_missing or (
                        o.get("parent_object_id") is not None and o["parent_object_id"] not in objs
                    ):
                        del objs[oid]
                        changed = True
                conns = view["connections"]
                for cid, c in list(conns.items()):
                    ends = set(objs) | set(conns)
                    if (
                        (
                            c.get("kind", "relationship") == "relationship"
                            and c["relationship_id"] not in self.relationships
                        )
                        or c["source_object_id"] not in ends
                        or c["target_object_id"] not in ends
                    ):
                        del conns[cid]
                        changed = True
            # ``view_ref`` is ON DELETE SET NULL in the database: a deleted view leaves the
            # objects that referenced it in place, without the reference (the manifest
            # omits a null ``view_ref``).
            for o in objs.values():
                if o.get("view_ref") is not None and o["view_ref"] not in self.views:
                    del o["view_ref"]

    def dangling_viewpoint_refs(self) -> dict[str, str]:
        """``{view id: viewpoint_ref}`` of views naming a viewpoint the model does not have."""
        ids = self.viewpoint_ids()
        return {
            vid: v["viewpoint_ref"]
            for vid, v in self.views.items()
            if v.get("viewpoint_ref") is not None and v["viewpoint_ref"] not in ids
        }


def _check_keys(op: ChangeOp, index: int) -> dict[str, Any]:
    attrs = dict(op.attributes or {})
    extra = sorted(set(attrs) - _ALLOWED[op.kind])
    if extra:
        raise _op_error(index, f"unknown {op.kind} attributes: {extra}", attributes=extra)
    if op.op == "update" and {"view_id"} & set(attrs):
        raise _op_error(index, "an occurrence cannot move to another view")
    return attrs


def _element_in(data: dict[str, Any], index: int) -> ArchitectureElementIn:
    # References may be local refs (not UUIDs): validated separately by the snapshot.
    return _validated(
        ArchitectureElementIn,
        {k: v for k, v in data.items() if k not in _REF_KEYS},
        index,
    )


def _rel_attributes(props: Any, index: int) -> dict[str, Any]:
    try:
        return _proposed_attributes(props if isinstance(props, dict) else {})
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise _OpError(
            [Finding.model_validate(f) for f in detail.get("findings", [])]
            or _op_error(index, str(exc.detail)).findings
        ) from exc


def _organization_in_memory(w: _Working, op: ChangeOp, index: int) -> None:
    """Replace the folder tree; every item must be a concept, relationship or view."""
    if op.op != "update" or op.id is not None:
        raise _op_error(index, "an organization op is an update of the whole tree, without id")
    attrs = _check_keys(op, index)
    if "organizations" not in attrs:
        raise _op_error(index, "an organization op needs attributes.organizations")
    try:
        tree = organizations.parse_tree(attrs["organizations"])
    except ValidationError as exc:
        raise _op_error(index, f"organizations do not validate: {exc.errors()[0]['msg']}") from exc
    missing = organizations.unresolved(tree, set(w.concepts) | set(w.relationships) | set(w.views))
    if missing:
        raise _op_error(
            index,
            f"folder items are not concepts, relationships or views of the model: {missing[:20]}",
            unresolved=missing[:100],
        )
    w.organizations = tree


def _exchange_in_memory(w: _Working, op: ChangeOp, index: int) -> None:
    """Replace the model's exchange content; it must be exportable as it is stored."""
    if op.op != "update" or op.id is not None:
        raise _op_error(index, "a model_exchange op is an update of the whole content, without id")
    attrs = _check_keys(op, index)
    raw = attrs.get("exchange")
    content = None if raw is None else _validated(ArchimateExchangeContent, raw, index)
    errors = [] if content is None else exchange_content_errors(content)
    if errors:
        raise _op_error(index, f"exchange content is not exportable: {errors[0]}")
    w.exchange = None if content is None else content.model_dump(mode="json")


def _apply_in_memory(w: _Working, op: ChangeOp, index: int, ref: str) -> None:
    if op.kind not in _ALLOWED:
        raise _op_error(index, f"{op.kind} is not an ArchiMate change kind", kind=op.kind)
    if op.kind == "organization":
        _organization_in_memory(w, op, index)
        return
    if op.kind == "model_exchange":
        _exchange_in_memory(w, op, index)
        return
    attrs = _check_keys(op, index)
    if op.op == "create":
        if w.known(ref):
            raise _op_error(index, f"create id {ref} is already in use", id=ref)
    elif not w.known(op.id or ""):
        raise _op_error(index, f"{op.kind} {op.id} is not in this model", id=op.id)

    if op.kind == "element":
        if op.op == "delete":
            if op.id not in w.concepts:
                raise _op_error(index, f"{op.id} is not an element", id=op.id)
            del w.concepts[op.id]
            w.drop_relationships(
                {rid for rid, r in w.relationships.items() if op.id in (r["source"], r["target"])}
            )
            w.prune()
            return
        cur = w.concepts.get(op.id or "") if op.op == "update" else None
        if op.op == "update" and cur is None:
            raise _op_error(index, f"{op.id} is not an element", id=op.id)
        data = {
            "title": attrs.get("title", cur["name"] if cur else None),
            "element_type": attrs.get("element_type", cur["type"] if cur else None),
            "description": attrs.get("description", cur.get("documentation") if cur else None),
            "properties": attrs.get("properties")
            if "properties" in attrs
            else (
                {"junction_type": cur["junction_type"]} if cur and cur.get("junction_type") else {}
            ),
            "status": attrs.get("status", "draft"),
            "architecture_state": attrs.get("architecture_state"),
        }
        if attrs.get("layer") is not None:
            data["layer"] = attrs["layer"]  # omitted: derived from element_type
        payload = _element_in(data, index)
        props = payload.properties or {}
        w.concepts[ref if cur is None else op.id or ref] = {
            "id": ref if cur is None else op.id,
            "type": payload.element_type,
            "name": payload.title,
            "layer": payload.layer,
            "parent_id": attrs.get("parent_id", cur.get("parent_id") if cur else None),
            "junction_type": props.get("junction_type")
            if payload.element_type == "junction"
            else None,
            "documentation": payload.description,
            "properties": _pairs({k: v for k, v in props.items() if k != "code_binding"})
            if "properties" in attrs or cur is None
            else cur.get("properties"),
        }
        return

    if op.kind == "relationship":
        if op.op == "delete":
            if op.id not in w.relationships:
                raise _op_error(index, f"{op.id} is not a relationship", id=op.id)
            w.drop_relationships({op.id or ""})
            w.prune()
            return
        cur = w.relationships.get(op.id or "") if op.op == "update" else None
        if op.op == "update" and cur is None:
            raise _op_error(index, f"{op.id} is not a relationship", id=op.id)
        if cur is None:
            missing = sorted({"relationship_type", "source_id", "target_id"} - set(attrs))
            if missing:
                raise _op_error(index, f"missing relationship attributes: {missing}")
        rel = dict(cur or {"id": ref, "name": "", "documentation": None, "properties": None})
        if "relationship_type" in attrs:
            rel["type"] = attrs["relationship_type"]
        for key, field in (("source_id", "source"), ("target_id", "target")):
            if key in attrs:
                rel[field] = str(attrs[key])
        if "name" in attrs:
            rel["name"] = attrs["name"] or ""
        if "description" in attrs:
            rel["documentation"] = attrs["description"]
        if "properties" in attrs or cur is None:
            rel.update(_rel_attributes(attrs.get("properties"), index))
            rel["properties"] = _pairs(attrs.get("properties"))
        w.relationships[rel["id"]] = rel
        return

    if op.kind == "view":
        if op.op == "delete":
            if op.id not in w.views:
                raise _op_error(index, f"{op.id} is not a view", id=op.id)
            del w.views[op.id]
            w.prune()
            return
        cur = w.views.get(op.id or "") if op.op == "update" else None
        if op.op == "update" and cur is None:
            raise _op_error(index, f"{op.id} is not a view", id=op.id)
        payload = _validated(
            ArchitectureViewIn,
            {
                "title": attrs.get("title", cur["name"] if cur else None),
                "viewpoint": attrs.get("viewpoint", cur["viewpoint"] if cur else None),
                "description": attrs.get("description", cur.get("documentation") if cur else None),
            },
            index,
        )
        view = cur or {"id": ref, "objects": {}, "connections": {}, "properties": None}
        view.update(
            {
                "name": payload.title,
                "viewpoint": payload.viewpoint,
                "documentation": payload.description,
            }
        )
        if attrs.get("viewpoint_ref") is not None:
            if attrs["viewpoint_ref"] not in w.viewpoint_ids():
                raise _op_error(
                    index, f"viewpoint_ref {attrs['viewpoint_ref']} is not a viewpoint of the model"
                )
            view["viewpoint_ref"] = attrs["viewpoint_ref"]
        elif "viewpoint_ref" in attrs:
            view.pop("viewpoint_ref", None)
        w.views[view["id"]] = view
        return

    if op.kind == "view_object":
        if op.op == "delete":
            view = w.object_view(op.id or "")
            if view is None:
                raise _op_error(index, f"{op.id} is not a view object", id=op.id)
            del view["objects"][op.id]
            w.prune()
            return
        if op.op == "create":
            view = w.views.get(str(attrs.get("view_id")))
            if view is None:
                raise _op_error(index, f"view {attrs.get('view_id')} is not in this model")
            obj: dict[str, Any] = {"id": ref}
        else:
            view = w.object_view(op.id or "")
            if view is None:
                raise _op_error(index, f"{op.id} is not a view object", id=op.id)
            obj = dict(view["objects"][op.id])
        position = attrs.get("position")
        if position is not None and (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position <= MAX_POSITION
        ):
            raise _op_error(index, f"position must be an integer from 0 to {MAX_POSITION}")
        for key, value in attrs.items():
            if key in ("view_id", "position"):
                continue
            field = "concept_id" if key == "concept_ref" else key
            obj[field] = str(value) if key in _REF_KEYS and value is not None else value
        checked = _validated(ArchimateViewObjectIn, models_svc.object_snapshot_fields(obj), index)
        if checked.view_ref is not None and checked.view_ref not in w.views:
            raise _op_error(index, f"view_ref {checked.view_ref} is not a view of this model")
        if checked.kind == "element" and obj.get("documentation") is not None:
            raise _op_error(
                index,
                "documentation is not allowed on an element occurrence (document the element)",
            )
        view["objects"][checked.id] = {
            **checked.model_dump(mode="json", exclude={"view_ref"}),
            "documentation": obj.get("documentation"),
            **({"view_ref": checked.view_ref} if checked.view_ref is not None else {}),
        }
        if position is not None:
            view["objects"] = _moved(view["objects"], checked.id, position)
        return

    # view_connection
    if op.op == "delete":
        view = w.connection_view(op.id or "")
        if view is None:
            raise _op_error(index, f"{op.id} is not a view connection", id=op.id)
        del view["connections"][op.id]
        w.prune()
        return
    if op.op == "create":
        view = w.views.get(str(attrs.get("view_id")))
        if view is None:
            raise _op_error(index, f"view {attrs.get('view_id')} is not in this model")
        conn: dict[str, Any] = {"id": ref}
    else:
        view = w.connection_view(op.id or "")
        if view is None:
            raise _op_error(index, f"{op.id} is not a view connection", id=op.id)
        conn = dict(view["connections"][op.id])
    for key, value in attrs.items():
        if key == "view_id":
            continue
        field = "relationship_id" if key == "relationship_ref" else key
        conn[field] = str(value) if key in _REF_KEYS and value is not None else value
    checked = _validated(ArchimateViewConnectionIn, conn, index)
    view["connections"][checked.id] = checked.model_dump(mode="json")


def _local_ref(op: ChangeOp, index: int) -> str:
    return op.id or f"new:{index}"


def _edge_projects(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Each relationship's ``project_id`` where ``uq_arch_rels_tenant_edge`` applies.

    The index is partial on ``project_id IS NOT NULL AND visibility = 'tenant'``; a row
    carries its model's visibility, and a relationship a change set creates gets the
    model's project. ``None``: the model is private, so the index never applies.
    """
    header = manifest.get("model") or {}
    if header.get("visibility", "tenant") != "tenant":
        return None
    projects: dict[str, Any] = {
        r["id"]: r.get("project_id")
        for r in (manifest.get("attributes") or {}).get("relationships", [])
    }
    projects[""] = header.get("project_id")  # default: what apply gives a new one
    return projects


def _check_duplicate_edge(
    w: _Working, projects: dict[str, Any], op: ChangeOp, index: int, ref: str
) -> None:
    """``CS-OP-002`` when the op leaves two relationships with one identity.

    Identity = (project, type, source, target), as the database's edge index keys it
    (ends of one id share an entity type); only relationships of a project count.
    """
    if op.kind != "relationship" or op.op == "delete":
        return
    rid = ref if op.op == "create" else op.id or ref
    rel = w.relationships.get(rid)
    if rel is None:
        return

    def project(i: str) -> Any:
        return projects.get(i, projects[""])

    if project(rid) is None:
        return
    key = (project(rid), rel.get("type"), rel.get("source"), rel.get("target"))
    other = next(
        (
            oid
            for oid, o in w.relationships.items()
            if oid != rid and (project(oid), o.get("type"), o.get("source"), o.get("target")) == key
        ),
        None,
    )
    if other is not None:
        raise _op_error(
            index,
            f"relationship {rid} duplicates relationship {other} ({key[1]} {key[2]} -> "
            f"{key[3]}) in project {key[0]}; a shared project holds one relationship per "
            "type and ends",
            rule_id=OP_DUPLICATE_EDGE,
            id=rid,
            duplicate_of=other,
        )


def evaluate(manifest: dict[str, Any], ops: list[ChangeOp]) -> tuple[list[Finding], list[Finding]]:
    """(findings, blocking) of applying ``ops`` to ``manifest`` in memory. Pure."""
    working = _Working(copy.deepcopy(manifest))
    projects = _edge_projects(manifest)
    blocking: list[Finding] = []
    step = working.snapshot()
    for index, op in enumerate(ops):
        try:
            _apply_in_memory(working, op, index, _local_ref(op, index))
            if projects is not None:
                _check_duplicate_edge(working, projects, op, index, _local_ref(op, index))
        except _OpError as exc:
            blocking.extend(exc.findings)
            continue
        if op.op != "delete" and op.kind in _GATED_KINDS:
            # Apply runs the ops one by one through the write gates, so each op must pass
            # the gate on the state the ops before it left — not only the final state (an
            # illegal edge deleted again by a later op is still refused at apply).
            nxt = working.snapshot()
            blocking.extend(introduced_errors(step, nxt, rules=_GATE_RULES))
            step = nxt
        else:
            step = working.snapshot()
    blocking.extend(_dangling_viewpoint_findings(manifest, working, ops))
    before = _Working(manifest).snapshot()
    after = working.snapshot()
    blocking.extend(introduced_errors(before, after, rules=_ALL_RULES))
    findings = [f for f in blocking if f.rule_id in CHANGE_SET_RULES]
    findings += validate_archimate(after).findings
    return findings, _dedupe(blocking)


def _dangling_viewpoint_findings(
    manifest: dict[str, Any], working: _Working, ops: list[ChangeOp]
) -> list[Finding]:
    """A view naming a viewpoint the final exchange content no longer has (AM-REF-001).

    Checked on the FINAL state, not at the exchange op: an import replaces the exchange
    content first and then updates the views that name its viewpoints. Blamed on the last
    ``model_exchange`` op (the only op that can remove a viewpoint); refs already dangling
    before the change set are not this change set's to answer for.
    """
    dangling = working.dangling_viewpoint_refs()
    for vid, ref in _Working(manifest).dangling_viewpoint_refs().items():
        if dangling.get(vid) == ref:
            del dangling[vid]
    if not dangling:
        return []
    exchange_ops = [i for i, op in enumerate(ops) if op.kind == "model_exchange"]
    return _op_error(
        exchange_ops[-1] if exchange_ops else None,
        f"{len(dangling)} view(s) name a viewpoint the model's exchange content does not have",
        rule_id="AM-REF-001",
        views=[{"view_id": vid, "viewpoint_ref": ref} for vid, ref in sorted(dangling.items())],
    ).findings


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    out = []
    for f in findings:
        key = f.model_dump_json()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def create_change_set(
    session: AsyncSession,
    model_id: UUID,
    payload: ChangeSetIn,
    *,
    tenant_id: str,
    subject_id: str,
) -> ModelingChangeSet:
    await models_svc.get_mutable_model(session, model_id, tenant_id, subject_id)
    model = await models_svc.get_model(session, model_id, tenant_id, subject_id, lock="share")
    if model.head != payload.base_head:
        raise models_svc.stale_head(model.head)
    manifest = await models_svc.canonical_manifest(session, model)
    model_pk, model_tenant = model.id, model.tenant_id
    evaluator = uml_cs.evaluate if model.language == "uml" else evaluate
    await session.commit()  # validate with no transaction open

    # CPU-bound and pure: off the event loop.
    findings, blocking = await run_in_threadpool(evaluator, manifest, payload.ops)
    cs = ModelingChangeSet(
        tenant_id=model_tenant,
        model_id=model_pk,
        status="invalid" if blocking else "proposed",
        ops=[op.model_dump(mode="json") for op in payload.ops],
        rationale=payload.rationale,
        evaluator_ref=payload.evaluator_ref,
        base_head=payload.base_head,
        findings=[f.model_dump(mode="json") for f in findings],
        blocking_findings=[f.model_dump(mode="json") for f in blocking],
        created_by=_sub(tenant_id, subject_id),
    )
    session.add(cs)
    await session.commit()
    await session.refresh(cs)
    return cs


async def get_change_set(
    session: AsyncSession, change_set_id: UUID, tenant_id: str, subject_id: str
) -> ModelingChangeSet:
    row = (
        await session.execute(
            select(ModelingChangeSet).where(
                ModelingChangeSet.id == change_set_id,
                ModelingChangeSet.model_id.in_(visible_model_ids(tenant_id, subject_id)),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="change set not found")
    return row


async def list_change_sets(
    session: AsyncSession, model_id: UUID, *, status: str | None, limit: int, offset: int
) -> tuple[list[ModelingChangeSet], int]:
    stmt = select(ModelingChangeSet).where(ModelingChangeSet.model_id == model_id)
    if status:
        stmt = stmt.where(ModelingChangeSet.status == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(ModelingChangeSet.created_at.desc(), ModelingChangeSet.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def _locked(
    session: AsyncSession, change_set_id: UUID, tenant_id: str, subject_id: str, *, lock: bool
) -> tuple[ModelingChangeSet, Any]:
    """The change set and its model, both row-locked, in that order: model, then set.

    The set is read once to find its model, then RE-READ under ``FOR UPDATE`` after the
    model lock (``populate_existing``: never the copy the first read cached), so its
    status is the committed one of whoever held the locks before us.
    """
    cs = await get_change_set(session, change_set_id, tenant_id, subject_id)
    model = await models_svc.get_mutable_model(
        session, cs.model_id, tenant_id, subject_id, lock=lock
    )
    cs = (
        await session.execute(
            select(ModelingChangeSet)
            .where(ModelingChangeSet.id == cs.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return cs, model


async def reject_change_set(
    session: AsyncSession, change_set_id: UUID, reason: str, *, tenant_id: str, subject_id: str
) -> ModelingChangeSet:
    cs, _model = await _locked(session, change_set_id, tenant_id, subject_id, lock=True)
    if cs.status not in ("proposed", "invalid"):
        raise HTTPException(
            status_code=409,
            detail={"code": NOT_OPEN, "message": f"a {cs.status} change set cannot be rejected"},
        )
    cs.status = "rejected"
    cs.reject_reason = reason
    cs.rejected_by = _sub(tenant_id, subject_id)
    cs.rejected_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(cs)
    return cs


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


async def _view(session: AsyncSession, view_id: UUID) -> ArchitectureView:
    view = await session.get(ArchitectureView, view_id)
    assert view is not None  # FK: an occurrence's view exists
    return view


async def _end_type(session: AsyncSession, ref: UUID) -> str:
    if await session.get(ArchitectureElement, ref) is not None:
        return "architecture_elements"
    return "architecture_relationships"


async def _in_model(session: AsyncSession, cls: Any, ref: Any, model_id: UUID, index: int) -> Any:
    row = await session.get(cls, _uuid(ref))
    if row is None or row.model_id != model_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "model_integrity",
                "message": f"op {index}: {ref} is not in this model",
                "findings": [
                    _op_error(index, f"{ref} is not in this model").findings[0].model_dump()
                ],
            },
        )
    return row


async def _apply_op(
    session: AsyncSession,
    model: Any,
    op: ChangeOp,
    index: int,
    id_map: dict[str, str],
    *,
    tenant_id: str,
    subject_id: str,
) -> None:
    attrs = {
        k: (id_map.get(str(v), v) if k in _REF_KEYS and v is not None else v)
        for k, v in (op.attributes or {}).items()
    }
    target = id_map.get(op.id or "", op.id)
    kw = {"tenant_id": tenant_id, "subject_id": subject_id}

    if op.kind == "organization":
        tree = organizations.parse_tree(attrs.get("organizations"))
        await organizations.write_tree(session, model, organizations.substitute(tree, id_map))
        return

    if op.kind == "model_exchange":
        raw = attrs.get("exchange")
        model.exchange = (
            None
            if raw is None
            else ArchimateExchangeContent.model_validate(raw).model_dump(mode="json")
        )
        await session.flush()
        await bump_models(session, {model.id})
        return

    if op.kind == "element":
        if op.op == "delete":
            await _in_model(session, ArchitectureElement, target, model.id, index)
            await element_svc.delete(session, _uuid(target), tenant_id, subject_id)
            return
        row = (
            await _in_model(session, ArchitectureElement, target, model.id, index)
            if op.op == "update"
            else None
        )
        base: dict[str, Any] = (
            {
                "title": row.title,
                "description": row.description,
                "project_id": row.project_id,
                "element_type": row.element_type,
                "properties": row.properties,
                "status": row.status,
                "architecture_state": row.architecture_state,
                "parent_id": row.parent_id,
                "is_synthetic": row.is_synthetic,
                "version": row.version,
                "version_status": row.version_status,
                "supersedes": row.supersedes,
                "superseded_by": row.superseded_by,
                "version_notes": row.version_notes,
            }
            if row is not None
            else {"project_id": model.project_id}
        )
        payload = ArchitectureElementIn.model_validate({**base, **attrs})
        element_id = row.id if row is not None else None
        await assert_parent_acyclic(
            session,
            model=ArchitectureElement,
            tenant_id=tenant_id,
            element_id=element_id,
            parent_id=payload.parent_id,
        )
        await assert_parent_resolves(session, parent_id=payload.parent_id, **kw)
        if row is None:
            out = await element_svc.create(
                session,
                payload,
                tenant_id,
                subject_id,
                visibility=model.visibility,
                extra_columns={"model_id": model.id},
            )
            if op.id:
                id_map[op.id] = str(out.id)
        else:
            await assert_retype_keeps_relationships_valid(
                session, element_id=row.id, new_type=payload.element_type, **kw
            )
            await element_svc.update(session, row.id, payload, tenant_id, subject_id)
        return

    if op.kind == "relationship":
        if op.op == "delete":
            await _in_model(session, ArchitectureRelationship, target, model.id, index)
            await relationship_svc.delete(session, _uuid(target), tenant_id, subject_id)
            return
        changes = {k: v for k, v in attrs.items()}
        for key, etype in (
            ("source_id", "source_entity_type"),
            ("target_id", "target_entity_type"),
        ):
            if key in changes:
                changes[key] = _uuid(changes[key])
                changes[etype] = await _end_type(session, changes[key])
        if op.op == "update":
            await _in_model(session, ArchitectureRelationship, target, model.id, index)
            await relationship_svc.update(session, _uuid(target), changes, **kw)
            return
        out = await relationship_svc.create(
            session,
            ArchitectureRelationshipIn.model_validate({**changes, "project_id": model.project_id}),
            tenant_id,
            subject_id,
            visibility=model.visibility,
        )
        if op.id:
            id_map[op.id] = str(out.id)
        return

    if op.kind == "view":
        if op.op == "delete":
            await _in_model(session, ArchitectureView, target, model.id, index)
            await view_svc.delete(session, _uuid(target), tenant_id, subject_id)
            return
        row = (
            await _in_model(session, ArchitectureView, target, model.id, index)
            if op.op == "update"
            else None
        )
        base = (
            {
                "title": row.title,
                "description": row.description,
                "project_id": row.project_id,
                "viewpoint": row.viewpoint,
                "element_ids": row.element_ids,
                "relationship_ids": row.relationship_ids,
                "layout_data": row.layout_data,
                "version": row.version,
                "version_status": row.version_status,
                "supersedes": row.supersedes,
                "superseded_by": row.superseded_by,
                "version_notes": row.version_notes,
            }
            if row is not None
            else {"project_id": model.project_id}
        )
        viewpoint_ref = attrs.pop("viewpoint_ref", row.viewpoint_ref if row else None)
        payload = ArchitectureViewIn.model_validate({**base, **attrs})
        if row is None:
            out = await view_svc.create(
                session,
                payload,
                tenant_id,
                subject_id,
                visibility=model.visibility,
                extra_columns={"model_id": model.id, "viewpoint_ref": viewpoint_ref},
            )
            if op.id:
                id_map[op.id] = str(out.id)
        else:
            row.viewpoint_ref = viewpoint_ref
            await view_svc.update(session, row.id, payload, tenant_id, subject_id)
        return

    if op.kind == "view_object":
        if op.op == "create":
            view = await _in_model(session, ArchitectureView, attrs.get("view_id"), model.id, index)
            data: dict[str, Any] = {}
        else:
            obj = await _in_model(session, ArchitectureViewObject, target, model.id, index)
            view = await _view(session, obj.view_id)
            data = {
                "kind": obj.kind,
                "concept_ref": obj.concept_ref,
                "parent_object_id": obj.parent_object_id,
                "bounds": obj.bounds,
                "style": obj.style,
                "name": obj.name,
                "text": obj.text,
                "label": obj.label,
                "border_type": obj.border_type,
                "documentation": obj.documentation,
                "view_ref": obj.view_ref,
            }
            if op.op == "delete":
                await occ.delete_object(session, view, obj.id)
                return
        payload = ViewObjectIn.model_validate(
            {**data, **{k: v for k, v in attrs.items() if k != "view_id"}}
        )
        if op.op == "create":
            out = await occ.create_object(session, view, payload, **kw)
            if op.id:
                id_map[op.id] = str(out.id)
        else:
            await occ.update_object(session, view, _uuid(target), payload, **kw)
        return

    if op.op == "create":
        view = await _in_model(session, ArchitectureView, attrs.get("view_id"), model.id, index)
        data = {}
    else:
        conn = await _in_model(session, ArchitectureViewConnection, target, model.id, index)
        view = await _view(session, conn.view_id)
        data = {
            "kind": conn.kind,
            "relationship_ref": conn.relationship_ref,
            "source_object_id": conn.source_object_id,
            "target_object_id": conn.target_object_id,
            "bendpoints": conn.bendpoints,
            "style": conn.style,
            "label": conn.label,
        }
        if op.op == "delete":
            await occ.delete_connection(session, view, conn.id)
            return
    payload = ViewConnectionIn.model_validate(
        {**data, **{k: v for k, v in attrs.items() if k != "view_id"}}
    )
    if op.op == "create":
        out = await occ.create_connection(session, view, payload, **kw)
        if op.id:
            id_map[op.id] = str(out.id)
    else:
        await occ.update_connection(session, view, _uuid(target), payload, **kw)


async def apply_change_set(
    session: AsyncSession, change_set_id: UUID, *, tenant_id: str, subject_id: str
) -> ModelingChangeSet:
    cs, model = await _locked(session, change_set_id, tenant_id, subject_id, lock=True)
    if cs.status != "proposed":
        raise HTTPException(
            status_code=409,
            detail={"code": NOT_PROPOSED, "message": f"a {cs.status} change set cannot be applied"},
        )
    if model.head != cs.base_head:
        raise models_svc.stale_head(model.head)
    id_map: dict[str, str] = {}
    # The op being applied, for blame. A UML set is persisted in ONE write after every op
    # ran in memory, so a constraint it trips belongs to no single op: ``None``.
    current: int | None = None
    try:
        async with caller_owned_transaction(session):
            if model.language == "uml":
                ops = [ChangeOp.model_validate(raw) for raw in cs.ops]
                await uml_cs.apply_all(session, model, ops, id_map, subject_id=subject_id)
            for index, raw in enumerate(cs.ops if model.language != "uml" else []):
                current = index
                op = ChangeOp.model_validate(raw)
                await _apply_op(
                    session, model, op, index, id_map, tenant_id=tenant_id, subject_id=subject_id
                )
    except ValidationError as exc:
        # The in-memory check validated these attributes; a stored row that no longer
        # validates (e.g. a legacy vocabulary value) is refused, never half-applied.
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": OP_INVALID, "message": f"change set op does not validate: {exc}"},
        ) from exc
    except IntegrityError as exc:
        # A constraint the in-memory evaluation cannot see (or a row written since) —
        # e.g. the edge identity index. Every op is rolled back; the set stays proposed.
        await session.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        where = (
            ""
            if current is not None
            else "; a UML change set is persisted in one write, so no single op is named"
        )
        raise integrity_error(
            _op_error(
                current,
                "applying the change set violates a database constraint"
                + (f" ({constraint})" if constraint else "")
                + where,
                rule_id=OP_CONSTRAINT,
                constraint=constraint,
            ).findings
        ) from exc
    except BaseException:
        await session.rollback()
        raise
    cs.status = "applied"
    cs.applied_by = _sub(tenant_id, subject_id)
    cs.applied_at = datetime.now(UTC)
    cs.id_map = id_map
    cs.applied_head = session.info.get(HEADS, {}).get(model.id, model.head)
    await session.commit()
    await session.refresh(cs)
    return cs
