"""Open Group exchange-format export and staged import over the modeling lifecycle.

Export
    A revision exports its immutable manifest; the draft head exports a manifest built by
    the SAME builder sealing uses (``modeling_models.build_manifest``), read under the
    model's ``FOR SHARE`` lock so it is one state of the model. The transform itself
    (``tools.architecture.exchange``) runs with no transaction open.

Import (plan §8.2: domain-invalid imports only in an isolated staging area)
    The document is parsed by the hardened pure importer, then diffed against the draft
    head into change-set ops — ``create`` or ``update`` of every element, relationship,
    view, view object and view connection, plus one ``organization`` op replacing the
    folder tree when the document carries a tree other than Archi's defaults and it
    differs from the model's, and (first) one ``model_exchange`` op replacing the model's
    properties / metadata / custom viewpoints when the document has any and they differ; never ``delete`` (a file is not a statement that what it
    omits is gone, so a document without folders leaves the model's tree alone). The ops are evaluated by the same in-memory evaluator as
    ``POST …/change-sets`` and stored as a change set with
    ``evaluator_ref="exchange-import"``: ``proposed`` or ``invalid``. Nothing is written to
    the model; applying is the normal change-set apply route.

    *Identity.* An imported id matches an existing row when it IS that row's id, when it
    is ``id-<row id>`` (the ``xs:ID`` rewrite export applies to ids that are not NCNames),
    or when an earlier applied import of this model mapped it to that row (its
    ``id_map``). Otherwise it becomes a ``create`` op whose local reference is the
    imported id verbatim; if that id collides with an existing row of another kind (or an
    occurrence of another view) the reference is a deterministic replacement, reported in
    ``id_resolution``.

    *Idempotency.* Same bytes against the same head give the same op list (ops are
    ordered deterministically). An update op carries only the attributes that differ, so
    re-importing the model's own export yields an EMPTY op set: export → import is a
    fixed point.

    *Metadata.* Stored on the change set's existing JSONB ``findings`` (no schema change):
    ``XI-META-001`` (advice) carries ``source_sha256``, ``source_bytes``,
    ``source_filename``, ``schema_valid``, the exchange model id/name and the id
    resolution; one ``XI-LOSS-001`` (warning) per loss, with the stable loss ``code`` in
    its evidence. Losses never block; findings of the evaluator do.

Staging losses (stable codes, all in :data:`STAGING_LOSS_CODES` and disjoint from the
importer's ``LOSS_CODES``; staging can report no other code):

- ``STAGE-VIEW-PROPERTIES`` — properties on a view; RM views store none.
- ``STAGE-PROPERTY-DUPLICATE-KEY`` — a repeated property key; RM stores one value per key
  (the last one wins).
- ``STAGE-NAME-DEFAULTED`` — an element or view without a name; RM requires a title, so
  the type (element) or ``Untitled view`` is staged.
- ``STAGE-TEXT-NORMALISED`` — a name with surrounding whitespace, which RM strips.
- ``STAGE-MODEL-HEADER`` — the document's model name/documentation differ from the
  existing model's title/description; a change set does not change model metadata.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from models.architecture import ArchitectureElement, ArchitectureRelationship
from models.modeling import DEFAULT_LANGUAGE_VERSION, ModelingChangeSet, ModelingModel
from schemas.modeling import ArchimateSnapshot, Finding
from schemas.modeling_exchange import ExchangeImportOut, ExchangeLossOut
from schemas.modeling_lifecycle import ChangeOp, ChangeSetOut
from services import change_sets as cs_svc
from services import modeling_models as models_svc
from services.idempotency import Idempotency
from settings import get_settings
from tools.architecture.exchange import (
    ExchangeError,
    ExchangeLoss,
    ExportResult,
    ImportResult,
    export_exchange_report,
    import_exchange,
)

EVALUATOR_REF = "exchange-import"
IMPORT_RULESET = "modeling/exchange-import"
META_RULE = "XI-META-001"
LOSS_RULE = "XI-LOSS-001"
UNSUPPORTED_LANGUAGE = "exchange_unsupported_language"
EXCHANGE_TOO_LARGE = "exchange_too_large"
EXCHANGE_INVALID = "exchange_invalid"
UNTITLED_VIEW = "Untitled view"

STAGING_LOSS_CODES = frozenset(
    {
        "STAGE-VIEW-PROPERTIES",
        "STAGE-PROPERTY-DUPLICATE-KEY",
        "STAGE-NAME-DEFAULTED",
        "STAGE-TEXT-NORMALISED",
        "STAGE-MODEL-HEADER",
    }
)


# ---------------------------------------------------------------------------
# Shared refusals
# ---------------------------------------------------------------------------


def unsupported_language(language: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": UNSUPPORTED_LANGUAGE,
            "message": (
                f"the Open Group exchange format carries ArchiMate models; this is a "
                f"{language} model"
            ),
        },
    )


def too_large(total: int, what: str) -> HTTPException:
    limit = get_settings().model_sync_max_concepts
    return HTTPException(
        status_code=422,
        detail={
            "code": models_svc.INPUT_TOO_LARGE,
            "message": (
                f"{what} has {total} concepts and relationships; this is synchronous up to "
                f"MODEL_SYNC_MAX_CONCEPTS={limit}"
            ),
        },
    )


def manifest_size(manifest: dict[str, Any]) -> int:
    return len(manifest.get("concepts", [])) + len(manifest.get("relationships", []))


def check_manifest_size(manifest: dict[str, Any], what: str) -> None:
    total = manifest_size(manifest)
    if total > get_settings().model_sync_max_concepts:
        raise too_large(total, what)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_manifest(manifest: dict[str, Any]) -> ExportResult:
    """Exchange XML + losses of a manifest. Pure: the one transform both exports use."""
    header = manifest["model"]
    return export_exchange_report(
        models_svc.archimate_snapshot(manifest),
        model_id=str(header["id"]),
        name=header.get("title") or "",
        documentation=header.get("description"),
    )


async def draft_manifest(
    session: AsyncSession, model_id: UUID, tenant_id: str, subject_id: str
) -> tuple[dict[str, Any], int]:
    """(canonical manifest, head) of the draft, read as one state; ends the transaction."""
    model = await models_svc.get_model(session, model_id, tenant_id, subject_id, lock="share")
    if model.language != "archimate":
        raise unsupported_language(model.language)
    total = await models_svc.count_concepts(session, model.id)
    if total > get_settings().model_sync_max_concepts:
        raise too_large(total, "the model")
    manifest = await models_svc.canonical_manifest(session, model)
    head = model.head
    await session.commit()
    return manifest, head


async def revision_manifest(
    session: AsyncSession, model_id: UUID, rev_id: UUID, tenant_id: str, subject_id: str
) -> tuple[dict[str, Any], str, int]:
    """(manifest, content_hash, seq) of a sealed revision; ends the transaction."""
    await models_svc.get_model(session, model_id, tenant_id, subject_id)
    row = await models_svc.get_revision(session, model_id, rev_id)
    manifest, content_hash, seq = row.manifest, row.content_hash, row.seq
    await session.commit()
    language = manifest.get("model", {}).get("language")
    if language != "archimate":
        raise unsupported_language(str(language))
    check_manifest_size(manifest, "the revision")
    return manifest, content_hash, seq


def loss_out(loss: ExchangeLoss) -> ExchangeLossOut:
    return ExchangeLossOut(code=loss.code, id=loss.id, xpath=loss.xpath, message=loss.message)


# ---------------------------------------------------------------------------
# Import: diff a parsed document against the head
# ---------------------------------------------------------------------------


def _blank_none(value: str | None) -> str | None:
    return value if value else None


def _canon(value: Any) -> Any:
    return models_svc.canonicalize(value) if value is not None else None


def _pairs(props: dict[str, Any]) -> Any:
    return models_svc._property_pairs(props)  # noqa: SLF001 — same rendering as manifests


def _as_uuid(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


@dataclass
class _Head:
    """The draft head's ids by kind, from its manifest."""

    concepts: dict[str, dict[str, Any]]
    relationships: dict[str, dict[str, Any]]
    views: dict[str, dict[str, Any]]
    objects: dict[str, tuple[str, dict[str, Any]]]  # id -> (view id, object)
    connections: dict[str, tuple[str, dict[str, Any]]]
    all_ids: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, manifest: dict[str, Any]) -> _Head:
        views = {v["id"]: v for v in manifest.get("views", [])}
        head = cls(
            concepts={c["id"]: c for c in manifest.get("concepts", [])},
            relationships={r["id"]: r for r in manifest.get("relationships", [])},
            views=views,
            objects={o["id"]: (vid, o) for vid, v in views.items() for o in v.get("objects", [])},
            connections={
                c["id"]: (vid, c) for vid, v in views.items() for c in v.get("connections", [])
            },
        )
        head.all_ids = (
            set(head.concepts)
            | set(head.relationships)
            | set(head.views)
            | set(head.objects)
            | set(head.connections)
        )
        return head


class _Diff:
    def __init__(
        self,
        manifest: dict[str, Any],
        raw_props: dict[str, dict[str, Any]],
        prior_map: dict[str, str],
    ) -> None:
        self.head = _Head.of(manifest)
        self.raw_props = raw_props
        self.prior_map = prior_map
        self.ops: list[ChangeOp] = []
        self.losses: list[ExchangeLoss] = []
        self.resolution: dict[str, str] = {}
        self.refs: dict[str, str] = {}  # imported id -> head id or local ref
        self.claimed: set[str] = set()

    def loss(self, code: str, ident: str | None, message: str) -> None:
        if code not in STAGING_LOSS_CODES:
            raise ValueError(f"{code!r} is not a staging loss code (STAGING_LOSS_CODES)")
        self.losses.append(ExchangeLoss(code=code, id=ident, xpath=None, message=message))

    # -- identity --

    def _candidates(self, xid: str) -> list[str]:
        out = [xid]
        if xid.startswith("id-"):
            out.append(xid[3:])
        if xid in self.prior_map:
            out.append(self.prior_map[xid])
        return [u for u in (_as_uuid(c) for c in out) if u is not None]

    def match(self, xid: str, table: dict[str, Any], view_id: str | None = None) -> str | None:
        for cand in self._candidates(xid):
            if cand in self.claimed or cand not in table:
                continue
            if view_id is not None and table[cand][0] != view_id:
                continue
            self.claimed.add(cand)
            self.refs[xid] = cand
            if cand != xid:
                self.resolution[xid] = cand
            return cand
        return None

    def local_ref(self, xid: str, kind: str) -> str:
        ref = xid
        if ref in self.head.all_ids or ref in self.refs.values():
            ref = "x-" + hashlib.sha256(f"{kind}:{xid}".encode()).hexdigest()[:40]
            self.resolution[xid] = ref
        self.refs[xid] = ref
        return ref

    def ref(self, xid: str | None) -> str | None:
        return None if xid is None else self.refs.get(xid, xid)

    # -- properties --

    def _props(
        self, xid: str, pairs: list[tuple[str, str]] | None, head_id: str | None
    ) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for key, value in pairs or []:
            if key in props:
                self.loss(
                    "STAGE-PROPERTY-DUPLICATE-KEY",
                    xid,
                    f"property {key!r} is repeated; RM keeps one value per key (the last)",
                )
            props[key] = value
        raw = self.raw_props.get(head_id or "", {})
        for key, value in list(props.items()):
            stored = raw.get(key)
            if (
                stored is not None
                and not isinstance(stored, str)
                and json.dumps(stored, sort_keys=True, ensure_ascii=False) == value
            ):
                props[key] = stored  # keep the stored JSON type of an unchanged value
        return props

    def _text(self, xid: str, value: str, what: str) -> str:
        stripped = value.strip()
        if stripped != value and stripped:
            self.loss("STAGE-TEXT-NORMALISED", xid, f"{what} whitespace was stripped")
        return stripped

    # -- kinds --

    def elements(self, snapshot: ArchimateSnapshot) -> None:
        for c in sorted(snapshot.concepts, key=lambda c: c.id):
            head_id = self.match(c.id, self.head.concepts)
            name = self._text(c.id, c.name, "element name")
            if not name:
                name = c.type.replace("_", " ").title()
                self.loss("STAGE-NAME-DEFAULTED", c.id, f"element has no name; staged as {name!r}")
            props = self._props(c.id, c.properties, head_id)
            if c.type == "junction" and c.junction_type:
                props["junction_type"] = c.junction_type
            want = {
                "title": name,
                "element_type": c.type,
                "description": _blank_none(c.documentation),
                "properties": props,
            }
            if head_id is None:
                self.ops.append(
                    ChangeOp(
                        op="create", kind="element", id=self.local_ref(c.id, "e"), attributes=want
                    )
                )
                continue
            cur = self.head.concepts[head_id]
            changed: dict[str, Any] = {}
            if want["title"] != cur["name"]:
                changed["title"] = want["title"]
            if want["element_type"] != cur["type"]:
                changed["element_type"] = want["element_type"]
            if want["description"] != _blank_none(cur.get("documentation")):
                changed["description"] = want["description"]
            if _pairs(props) != cur.get("properties") or "element_type" in changed:
                changed["properties"] = props
            if changed:
                self.ops.append(
                    ChangeOp(op="update", kind="element", id=head_id, attributes=changed)
                )

    def relationships(self, snapshot: ArchimateSnapshot) -> None:
        pending = []
        for r in sorted(snapshot.relationships, key=lambda r: r.id):
            head_id = self.match(r.id, self.head.relationships)
            pending.append((r, head_id))
            if head_id is None:
                self.local_ref(r.id, "r")
        created = {r.id for r, h in pending if h is None}
        emitted: set[str] = set()
        while pending:
            ready = [
                (r, h)
                for r, h in pending
                if not ({r.source, r.target} & (created - emitted - {r.id}))
            ] or pending  # a cycle of new relationships: emit as is; evaluation refuses it
            for r, head_id in ready:
                self._relationship(r, head_id)
                emitted.add(r.id)
            pending = [p for p in pending if p[0].id not in emitted]

    def _relationship(self, r: Any, head_id: str | None) -> None:
        props = self._props(r.id, r.properties, head_id)
        for key in ("access_type", "strength", "directed"):
            if getattr(r, key) is not None:
                props[key] = getattr(r, key)
        want = {
            "relationship_type": r.type,
            "source_id": self.ref(r.source),
            "target_id": self.ref(r.target),
            "name": r.name,
            "description": _blank_none(r.documentation),
            "properties": props,
        }
        if head_id is None:
            self.ops.append(
                ChangeOp(op="create", kind="relationship", id=self.refs[r.id], attributes=want)
            )
            return
        cur = self.head.relationships[head_id]
        current = {
            "relationship_type": cur["type"],
            "source_id": cur["source"],
            "target_id": cur["target"],
            "name": cur.get("name") or "",
            "description": _blank_none(cur.get("documentation")),
        }
        changed = {k: want[k] for k, v in current.items() if want[k] != v}
        if _pairs(props) != cur.get("properties"):
            changed["properties"] = props
        if changed:
            self.ops.append(
                ChangeOp(op="update", kind="relationship", id=head_id, attributes=changed)
            )

    def views(self, snapshot: ArchimateSnapshot) -> list[tuple[Any, str | None]]:
        out = []
        for v in sorted(snapshot.views or [], key=lambda v: v.id):
            head_id = self.match(v.id, self.head.views)
            cur = self.head.views.get(head_id or "")
            name = self._text(v.id, v.name, "view name")
            if not name:
                name = UNTITLED_VIEW
                self.loss("STAGE-NAME-DEFAULTED", v.id, f"view has no name; staged as {name!r}")
            viewpoint = v.viewpoint  # None = no viewpoint (unfiltered), stored as NULL
            if v.properties:
                self.loss(
                    "STAGE-VIEW-PROPERTIES",
                    v.id,
                    f"{len(v.properties)} view properties are not stored by RM views",
                )
            want = {
                "title": name,
                "viewpoint": viewpoint,
                "viewpoint_ref": v.viewpoint_ref,
                "description": _blank_none(v.documentation),
            }
            if cur is None:
                ref = self.local_ref(v.id, "v")
                self.ops.append(
                    ChangeOp(op="create", kind="view", id=ref, attributes=_set_only(want))
                )
                out.append((v, None))
                continue
            assert head_id is not None
            current = {
                "title": cur["name"],
                "viewpoint": cur["viewpoint"],
                "viewpoint_ref": cur.get("viewpoint_ref"),
                "description": _blank_none(cur.get("documentation")),
            }
            changed = {k: want[k] for k, val in current.items() if want[k] != val}
            if changed:
                self.ops.append(ChangeOp(op="update", kind="view", id=head_id, attributes=changed))
            out.append((v, head_id))
        return out

    def occurrences(self, views: list[tuple[Any, str | None]]) -> None:
        object_ops: list[ChangeOp] = []
        connection_ops: list[ChangeOp] = []
        for v, head_view in views:
            view_ref = self.refs[v.id]
            matched: dict[str, str | None] = {}
            for o in sorted(v.objects, key=lambda o: o.id):
                hid = self.match(o.id, self.head.objects, head_view) if head_view else None
                if hid is None:
                    self.local_ref(o.id, "o")
                matched[o.id] = hid
            # z-order: the document's sibling order is kept (z-order is per sibling group:
            # same parent). ``order`` simulates the view's object list as the ops below are
            # applied in turn. A new object is appended (no ``position``): that already puts
            # it after every sibling before it. An existing object that is not after the
            # previous imported sibling is moved right after it with ``position``. So a
            # document whose sibling order the view already has — its own export, or any
            # document imported into a new model — stages no ``position`` at all.
            head_objects = self.head.views[head_view]["objects"] if head_view else []
            order = [o["id"] for o in head_objects]
            last: dict[str | None, str] = {}
            for o in _parents_first(v.objects):
                hid = matched[o.id]
                me = hid or self.refs[o.id]
                parent = self.ref(o.parent_object_id)
                prev = last.get(parent)
                position: int | None = None
                if hid is None:
                    order.append(me)
                elif prev is not None and order.index(me) < order.index(prev):
                    order.remove(me)
                    position = order.index(prev) + 1
                    order.insert(position, me)
                last[parent] = me
                self._object(o, hid, view_ref, object_ops, position)
            cmatched: dict[str, str | None] = {}
            for c in sorted(v.connections, key=lambda c: c.id):
                hid = self.match(c.id, self.head.connections, head_view) if head_view else None
                if hid is None:
                    self.local_ref(c.id, "c")
                cmatched[c.id] = hid
            for c in _ends_first(v.connections):
                self._connection(c, cmatched[c.id], view_ref, connection_ops)
        self.ops.extend(object_ops)
        self.ops.extend(connection_ops)

    def exchange(self, snapshot: ArchimateSnapshot, head: Any) -> None:
        """One op replacing the model's exchange content, when the document has some and it
        differs (first: views may name its viewpoints)."""
        if snapshot.exchange is None:
            return
        want = snapshot.exchange.model_dump(mode="json")
        if _canon({"exchange": want}) != _canon({"exchange": head}):
            self.ops.append(
                ChangeOp(op="update", kind="model_exchange", attributes={"exchange": want})
            )

    def organizations(self, snapshot: ArchimateSnapshot, head_tree: Any) -> None:
        """One op replacing the tree, when the document has one and it differs."""
        if snapshot.organizations is None:
            return
        tree = _resolved_tree(self, snapshot.organizations)
        # Under its manifest key, so sibling-folder order counts as it does in a manifest.
        if _canon({"organizations": tree}) != _canon({"organizations": head_tree}):
            self.ops.append(
                ChangeOp(op="update", kind="organization", attributes={"organizations": tree})
            )

    def _object(
        self,
        o: Any,
        head_id: str | None,
        view_ref: str,
        out: list[ChangeOp],
        position: int | None = None,
    ) -> None:
        want = {
            "kind": o.kind,
            "concept_ref": self.ref(o.concept_id),
            "parent_object_id": self.ref(o.parent_object_id),
            "bounds": o.bounds.model_dump() if o.bounds is not None else None,
            "style": o.style,
            "name": o.name,
            "text": o.text,
            "label": o.label,
            "border_type": o.border_type,
            "documentation": o.documentation,
            "view_ref": self.ref(o.view_ref),
        }
        moved = {} if position is None else {"position": position}
        if head_id is None:
            attrs = {"view_id": view_ref, **_set_only(want), **moved}
            out.append(
                ChangeOp(op="create", kind="view_object", id=self.refs[o.id], attributes=attrs)
            )
            return
        cur = self.head.objects[head_id][1]
        current = {**cur, "concept_ref": cur.get("concept_id")}
        changed = {
            k: w
            for k, w in want.items()
            if _canon(_occ_value(k, w)) != _canon(_occ_value(k, current.get(k)))
        }
        if changed or moved:
            out.append(
                ChangeOp(
                    op="update",
                    kind="view_object",
                    id=head_id,
                    attributes={**changed, **moved},
                )
            )

    def _connection(self, c: Any, head_id: str | None, view_ref: str, out: list[ChangeOp]) -> None:
        want = {
            "kind": c.kind,
            "relationship_ref": self.ref(c.relationship_id),
            "source_object_id": self.ref(c.source_object_id),
            "target_object_id": self.ref(c.target_object_id),
            "bendpoints": [p.model_dump() for p in c.bendpoints] if c.bendpoints else None,
            "style": c.style,
            "label": c.label,
        }
        if head_id is None:
            attrs = {"view_id": view_ref, **want}
            out.append(
                ChangeOp(op="create", kind="view_connection", id=self.refs[c.id], attributes=attrs)
            )
            return
        cur = self.head.connections[head_id][1]
        current = {**cur, "relationship_ref": cur.get("relationship_id")}
        changed = {
            k: w
            for k, w in want.items()
            if _canon(_occ_value(k, w)) != _canon(_occ_value(k, current.get(k)))
        }
        if changed:
            out.append(
                ChangeOp(op="update", kind="view_connection", id=head_id, attributes=changed)
            )


def _resolved_tree(diff: _Diff, folders: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            **f.model_dump(mode="json", exclude={"items", "children"}),
            "items": [diff.ref(i) or i for i in f.items],
            "children": _resolved_tree(diff, f.children),
        }
        for f in folders
    ]


def _set_only(want: dict[str, Any]) -> dict[str, Any]:
    """A create op's attributes: the references added for exchange fidelity only when set."""
    return {
        k: v for k, v in want.items() if v is not None or k not in ("viewpoint_ref", "view_ref")
    }


def _occ_value(key: str, value: Any) -> Any:
    """Occurrence attribute for comparison: an empty style / bendpoint list is none."""
    if key in ("style", "bendpoints") and not value:
        return None
    return value


def _parents_first(objects: list[Any]) -> list[Any]:
    """Document order (z-order), except that a parent always precedes what it contains."""
    by_id = {o.id: o for o in objects}
    done: set[str] = set()
    out: list[Any] = []

    def visit(o: Any, trail: set[str]) -> None:
        if o.id in done:
            return
        parent = by_id.get(o.parent_object_id or "")
        if parent is not None and parent.id not in trail:
            visit(parent, trail | {o.id})
        done.add(o.id)
        out.append(o)

    for o in objects:
        visit(o, {o.id})
    return out


def _ends_first(connections: list[Any]) -> list[Any]:
    by_id = {c.id: c for c in connections}
    done: set[str] = set()
    out: list[Any] = []

    def visit(c: Any, trail: set[str]) -> None:
        if c.id in done:
            return
        for end in (c.source_object_id, c.target_object_id):
            dep = by_id.get(end)
            if dep is not None and dep.id not in trail:
                visit(dep, trail | {c.id})
        done.add(c.id)
        out.append(c)

    for c in sorted(connections, key=lambda c: c.id):
        visit(c, {c.id})
    return out


@dataclass
class Staged:
    ops: list[ChangeOp]
    findings: list[Finding]
    blocking: list[Finding]
    losses: list[ExchangeLoss]
    resolution: dict[str, str]


def stage(
    result: ImportResult,
    manifest: dict[str, Any],
    *,
    raw_props: dict[str, dict[str, Any]] | None = None,
    prior_map: dict[str, str] | None = None,
    model_header: tuple[str, str | None] | None = None,
) -> Staged:
    """Ops, evaluation and losses of importing ``result`` onto ``manifest``. Pure."""
    diff = _Diff(manifest, raw_props or {}, prior_map or {})
    snapshot = result.snapshot
    diff.exchange(snapshot, manifest.get("exchange"))
    diff.elements(snapshot)
    diff.relationships(snapshot)
    diff.occurrences(diff.views(snapshot))
    diff.organizations(snapshot, manifest.get("organizations"))
    if model_header is not None:
        title, description = model_header
        if (result.model_name or "") != title or _blank_none(
            result.model_documentation
        ) != _blank_none(description):
            diff.loss(
                "STAGE-MODEL-HEADER",
                result.model_id or None,
                "the document's model name/documentation differ from this model's title/"
                "description; an import does not change model metadata",
            )
    findings, blocking = cs_svc.evaluate(manifest, diff.ops)
    return Staged(
        ops=diff.ops,
        findings=findings,
        blocking=blocking,
        losses=[*result.losses, *diff.losses],
        resolution=dict(sorted(diff.resolution.items())),
    )


# ---------------------------------------------------------------------------
# Import: persistence
# ---------------------------------------------------------------------------


@dataclass
class Source:
    data: bytes
    filename: str | None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def parse(source: Source) -> ImportResult:
    try:
        return import_exchange(source.data, max_bytes=get_settings().exchange_import_max_bytes)
    except ExchangeError as exc:
        raise HTTPException(
            status_code=422, detail={"code": EXCHANGE_INVALID, "message": str(exc)}
        ) from exc


def _import_findings(source: Source, result: ImportResult, staged: Staged) -> list[dict[str, Any]]:
    meta = Finding(
        rule_id=META_RULE,
        ruleset_id=IMPORT_RULESET,
        severity="advice",
        message=(
            f"exchange import of {len(source.data)} bytes (sha256 {source.sha256}); "
            f"schema_valid={str(result.schema_valid).lower()}; {len(staged.losses)} losses"
        ),
        evidence={
            "source_sha256": source.sha256,
            "source_bytes": len(source.data),
            "source_filename": source.filename,
            "schema_valid": result.schema_valid,
            "exchange_model_id": result.model_id,
            "exchange_model_name": result.model_name,
            "language": result.language,
            "loss_count": len(staged.losses),
            "id_remapped": {k: v for k, v in sorted(result.id_map.items()) if k != v},
            "id_resolution": staged.resolution,
        },
    )
    losses = [
        Finding(
            rule_id=LOSS_RULE,
            ruleset_id=IMPORT_RULESET,
            severity="warning",
            path=loss.xpath,
            message=f"{loss.code}: {loss.message}",
            evidence={"code": loss.code, "id": loss.id, "xpath": loss.xpath},
        )
        for loss in staged.losses
    ]
    return [f.model_dump(mode="json") for f in (meta, *losses, *staged.findings)]


def _change_set(
    *,
    tenant_id: str,
    model_id: UUID,
    base_head: int,
    source: Source,
    result: ImportResult,
    staged: Staged,
    created_by: str,
) -> ModelingChangeSet:
    name = source.filename or result.model_name or "exchange document"
    return ModelingChangeSet(
        tenant_id=tenant_id,
        model_id=model_id,
        status="invalid" if staged.blocking else "proposed",
        ops=[op.model_dump(mode="json") for op in staged.ops],
        rationale=f"Open Group exchange import of {name!r} (sha256 {source.sha256})",
        evaluator_ref=EVALUATOR_REF,
        base_head=base_head,
        findings=_import_findings(source, result, staged),
        blocking_findings=[f.model_dump(mode="json") for f in staged.blocking],
        created_by=created_by,
    )


def import_out(
    cs: ModelingChangeSet,
    *,
    created: bool,
    source: Source,
    result: ImportResult,
    staged: Staged,
) -> ExchangeImportOut:
    return ExchangeImportOut(
        model_id=cs.model_id,
        model_created=created,
        change_set=ChangeSetOut.model_validate(cs),
        source_sha256=source.sha256,
        source_bytes=len(source.data),
        source_filename=source.filename,
        schema_valid=result.schema_valid,
        losses=[loss_out(loss) for loss in staged.losses],
        id_resolution=staged.resolution,
    )


def _check_import_size(result: ImportResult, head_size: int, staged: Staged | None) -> None:
    limit = get_settings().model_sync_max_concepts
    imported = len(result.snapshot.concepts) + len(result.snapshot.relationships)
    if imported > limit:
        raise too_large(imported, "the document")
    if staged is not None:
        creates = sum(
            1 for op in staged.ops if op.op == "create" and op.kind in ("element", "relationship")
        )
        if head_size + creates > limit:
            raise too_large(head_size + creates, "the model after this import")


async def _prior_import_map(session: AsyncSession, model_id: UUID) -> dict[str, str]:
    """Local ref -> row id from this model's applied imports, newest winning."""
    rows = (
        await session.execute(
            select(ModelingChangeSet.id_map)
            .where(
                ModelingChangeSet.model_id == model_id,
                ModelingChangeSet.evaluator_ref == EVALUATOR_REF,
                ModelingChangeSet.status == "applied",
            )
            .order_by(ModelingChangeSet.applied_at, ModelingChangeSet.id)
        )
    ).scalars()
    merged: dict[str, str] = {}
    for id_map in rows:
        merged.update({str(k): str(v) for k, v in (id_map or {}).items()})
    return merged


async def _raw_props(
    session: AsyncSession, model_id: UUID, tenant_id: str
) -> dict[str, dict[str, Any]]:
    """Stored properties of the model's rows (id AND tenant, like ``models._of_model``).

    ``model_id`` alone would also read a foreign row carrying the id (pre-gate data): its
    properties are not model content and must not reach an import's staging.
    """
    out: dict[str, dict[str, Any]] = {}
    for cls in (ArchitectureElement, ArchitectureRelationship):
        for rid, props in await session.execute(
            select(cls.id, cls.properties).where(
                cls.model_id == model_id, cls.tenant_id == tenant_id
            )
        ):
            if isinstance(props, dict):
                out[str(rid)] = props
    return out


async def import_into_model(
    session: AsyncSession,
    model_id: UUID,
    source: Source,
    idem: Idempotency,
    *,
    tenant_id: str,
    subject_id: str,
) -> tuple[ExchangeImportOut | None, Any]:
    """Stage an import into an existing model. Returns (out, replay) — one is None.

    A completed same-key request is replayed FIRST: the size cap below reads the head,
    which may have grown since, and a replay answers with the original response.
    """
    if (replay := await idem.lookup()) is not None:
        return None, replay
    model = await models_svc.get_mutable_model(session, model_id, tenant_id, subject_id)
    if model.language != "archimate":
        raise unsupported_language(model.language)
    await session.commit()
    result = await run_in_threadpool(parse, source)
    _check_import_size(result, 0, None)

    model = await models_svc.get_model(session, model_id, tenant_id, subject_id, lock="share")
    manifest = await models_svc.canonical_manifest(session, model)
    raw_props = await _raw_props(session, model.id, model.tenant_id)
    prior = await _prior_import_map(session, model.id)
    base_head, model_tenant, header = model.head, model.tenant_id, (model.title, model.description)
    await session.commit()  # diff and evaluate with no transaction open

    staged = await run_in_threadpool(
        lambda: stage(result, manifest, raw_props=raw_props, prior_map=prior, model_header=header)
    )
    _check_import_size(result, manifest_size(manifest), staged)

    if (replay := await idem.begin()) is not None:
        return None, replay
    cs = _change_set(
        tenant_id=model_tenant,
        model_id=model_id,
        base_head=base_head,
        source=source,
        result=result,
        staged=staged,
        created_by=subject_id or tenant_id,
    )
    session.add(cs)
    await session.flush()
    await session.refresh(cs)
    out = import_out(cs, created=False, source=source, result=result, staged=staged)
    await idem.stage(201, out, ExchangeImportOut)
    await session.commit()
    return out, None


async def import_as_new_model(
    session: AsyncSession,
    source: Source,
    idem: Idempotency,
    *,
    tenant_id: str,
    subject_id: str,
    visibility: str,
    project_id: str | None,
) -> tuple[ExchangeImportOut | None, Any]:
    """Create an ArchiMate model titled after the document and stage the import into it."""
    if (replay := await idem.lookup()) is not None:  # before any check (see import_into_model)
        return None, replay
    result = await run_in_threadpool(parse, source)
    _check_import_size(result, 0, None)
    empty: dict[str, Any] = {"concepts": [], "relationships": [], "views": []}
    staged = await run_in_threadpool(lambda: stage(result, empty))
    _check_import_size(result, 0, staged)

    if (replay := await idem.begin()) is not None:
        return None, replay
    title = (result.model_name or "").strip()[:300] or (source.filename or "")[:300].strip()
    model = ModelingModel(
        tenant_id=tenant_id,
        project_id=project_id or None,
        visibility=visibility,
        subject_id=subject_id or tenant_id,
        language="archimate",
        language_version=DEFAULT_LANGUAGE_VERSION["archimate"],
        title=title or "Imported model",
        description=_blank_none(result.model_documentation),
        head=0,
        is_default=False,
    )
    session.add(model)
    await session.flush()
    cs = _change_set(
        tenant_id=tenant_id,
        model_id=model.id,
        base_head=0,
        source=source,
        result=result,
        staged=staged,
        created_by=subject_id or tenant_id,
    )
    session.add(cs)
    await session.flush()
    await session.refresh(cs)
    out = import_out(cs, created=True, source=source, result=result, staged=staged)
    await idem.stage(201, out, ExchangeImportOut)
    await session.commit()
    return out, None
