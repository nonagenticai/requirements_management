"""A UML model's draft head in memory, the write operations on it, and the write gate. Pure.

Every UML write — an HTTP route, or one op of a change set — is the same three steps:
load the model as a :class:`UmlDraft`, apply ONE operation to a copy of it (``put`` /
``delete``, cascades included), and refuse the write with ``422 model_integrity`` when
:func:`introduced_errors` finds an error the operation would introduce. The persisted
rows are then made to equal the new draft (``services/uml_store.py``), so the database
cascades and the in-memory cascades can never disagree, and a change set evaluated here
is exactly what applying it does.

Cascades (UML composite ownership and occurrence semantics):

* deleting an element deletes what it owns (transitively), the interactions it owns, every
  relationship with an end on a deleted element, and every diagram occurrence of them;
* deleting a relationship, interaction or diagram deletes its occurrences; replacing an
  interaction document deletes the occurrences of lifelines/messages it no longer has;
* deleting a diagram object deletes the connections attached to it.

Any OTHER reference to a deleted concept (a property type, a provided interface, a
lifeline's type, a message's operation, …) is not silently dropped: it would become an
unresolved reference, so the delete is refused (``UML-REF-001``) until the referrer is
edited — the same write-time rule every other write obeys.

**What the gate refuses** (target spec §7.3 layer 1): every error-severity finding the
write introduces, of every rule of ``uml-2.5.1-p1`` — EXCEPT ``UML-UNSUP-001`` on
constructs inside a supported row (a feature group a kind does not allow, an unknown
visibility, direction, aggregation, message kind or fragment operator, an n-ary
association, a lifeline typed by an unsupported kind). Those are stored as authored and
reported by validation runs, so the run is never clean (plan §9: "unknown constructs must
return an explicit unsupported-capability diagnostic"). An unsupported KIND of a row
(element, relationship, diagram, connector kind) and an occurrence that does not fit its
diagram's kind ARE refused: the row itself would be outside the profile.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from schemas.modeling import Finding
from schemas.uml import UmlSnapshot
from schemas.uml_api import (
    UmlDiagramConnectionIn,
    UmlDiagramObjectIn,
    UmlDiagramUpdate,
    UmlElementUpdate,
    UmlInteractionUpdate,
    UmlRelationshipUpdate,
)
from tools.uml.rules import validate as validate_uml

UmlKind = Literal[
    "uml_element",
    "uml_relationship",
    "interaction",
    "diagram",
    "diagram_object",
    "diagram_connection",
]
UML_KINDS: tuple[str, ...] = (
    "uml_element",
    "uml_relationship",
    "interaction",
    "diagram",
    "diagram_object",
    "diagram_connection",
)
# The (update) body schema of each kind; a create body adds the model scope.
UPDATE_SCHEMAS: dict[str, type[BaseModel]] = {
    "uml_element": UmlElementUpdate,
    "uml_relationship": UmlRelationshipUpdate,
    "interaction": UmlInteractionUpdate,
    "diagram": UmlDiagramUpdate,
    "diagram_object": UmlDiagramObjectIn,
    "diagram_connection": UmlDiagramConnectionIn,
}
OCCURRENCE_KINDS = frozenset({"diagram_object", "diagram_connection"})
_ROW_KIND_PATHS = frozenset({"/kind", "/connector_kind"})
_ROW_TARGETS = frozenset({"uml_element", "uml_relationship", "diagram"})
_INTERACTION_PARTS = ("lifelines", "occurrences", "messages", "executions", "fragments")


def payload_dict(kind: str, row_id: str, payload: BaseModel, diagram_id: str | None) -> dict:
    """The draft (snapshot-shaped) dict of ``payload`` for row ``row_id``."""
    # ``position`` is not row content: it is the row's place in the draft's order
    # (:meth:`UmlDraft.put`).
    data = payload.model_dump(mode="json", exclude={"model_id", "project_id", "position"})
    out: dict[str, Any] = {"id": row_id, **data}
    if kind in OCCURRENCE_KINDS:
        out["diagram_id"] = diagram_id
    return out


def body_fields(row: dict[str, Any]) -> dict[str, Any]:
    """A draft dict as an update body (everything but its identity and diagram)."""
    return {k: v for k, v in row.items() if k not in ("id", "diagram_id")}


@dataclass
class UmlDraft:
    elements: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: dict[str, dict[str, Any]] = field(default_factory=dict)
    interactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # {id, kind, name, documentation}; occurrences are flat below, keyed by their own id.
    diagrams: dict[str, dict[str, Any]] = field(default_factory=dict)
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    connections: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- shape ---------------------------------------------------------------------

    def table(self, kind: str) -> dict[str, dict[str, Any]]:
        return {
            "uml_element": self.elements,
            "uml_relationship": self.relationships,
            "interaction": self.interactions,
            "diagram": self.diagrams,
            "diagram_object": self.objects,
            "diagram_connection": self.connections,
        }[kind]

    def copy(self) -> UmlDraft:
        return copy.deepcopy(self)

    def kind_of(self, row_id: str) -> str | None:
        return next((k for k in UML_KINDS if row_id in self.table(k)), None)

    def size(self) -> int:
        """What a synchronous check or render walks: every row, and every part of every
        interaction document (lifelines, occurrences, messages, executions, fragments)."""
        return (
            len(self.elements)
            + len(self.relationships)
            + interactions_size(self.interactions.values())
            + len(self.diagrams)
            + len(self.objects)
            + len(self.connections)
        )

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> UmlDraft:
        docs = {
            d["id"]: d.get("documentation")
            for d in (manifest.get("attributes") or {}).get("diagrams", [])
        }
        draft = cls(
            elements={e["id"]: dict(e) for e in manifest.get("elements", [])},
            relationships={r["id"]: dict(r) for r in manifest.get("relationships", [])},
            interactions={i["id"]: dict(i) for i in manifest.get("interactions", [])},
        )
        for d in manifest.get("diagrams") or []:
            draft.diagrams[d["id"]] = {
                "id": d["id"],
                "kind": d["kind"],
                "name": d.get("name", ""),
                "documentation": docs.get(d["id"]),
            }
            for o in d.get("objects", []):
                draft.objects[o["id"]] = {**o, "diagram_id": d["id"]}
            for c in d.get("connections", []):
                draft.connections[c["id"]] = {**c, "diagram_id": d["id"]}
        return copy.deepcopy(draft)

    def manifest_parts(self) -> dict[str, Any]:
        """``elements`` / ``relationships`` / ``interactions`` / ``diagrams`` exactly in the
        ``UmlSnapshot`` shape, plus ``attributes`` (stored fields the snapshot has no slot
        for: a diagram's documentation)."""
        return {
            **self._snapshot_parts(),
            "attributes": {
                "diagrams": [
                    {"id": d["id"], "documentation": d.get("documentation")}
                    for d in self.diagrams.values()
                ]
            },
        }

    def _snapshot_parts(self) -> dict[str, Any]:
        def strip(row: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in row.items() if k != "diagram_id"}

        return {
            "elements": [copy.deepcopy(e) for e in self.elements.values()],
            "relationships": [copy.deepcopy(r) for r in self.relationships.values()],
            "interactions": [copy.deepcopy(i) for i in self.interactions.values()],
            "diagrams": [
                {
                    "id": d["id"],
                    "kind": d["kind"],
                    "name": d.get("name", ""),
                    "objects": [
                        strip(o) for o in self.objects.values() if o["diagram_id"] == d["id"]
                    ],
                    "connections": [
                        strip(c) for c in self.connections.values() if c["diagram_id"] == d["id"]
                    ],
                }
                for d in self.diagrams.values()
            ],
        }

    def snapshot(self) -> UmlSnapshot:
        return UmlSnapshot.model_validate(self._snapshot_parts())

    # -- operations ----------------------------------------------------------------

    def put(self, kind: str, row: dict[str, Any], position: int | None = None) -> None:
        """Create or replace one row (snapshot-shaped dict with ``id``).

        A new row goes last; a replaced one keeps its place. A diagram object's order is
        its z-order: ``position`` moves it to that index of its diagram's object list.
        """
        self.table(kind)[row["id"]] = copy.deepcopy(row)
        if kind == "diagram_object" and position is not None:
            self.place(row["id"], position)
        if kind == "interaction":
            self.prune()  # a replaced document may drop shown lifelines/messages

    def place(self, object_id: str, position: int) -> None:
        """Move a diagram object to index ``position`` (clamped) of its diagram's objects."""
        diagram = self.objects[object_id]["diagram_id"]
        order = [k for k, o in self.objects.items() if o["diagram_id"] == diagram]
        order.remove(object_id)
        order.insert(min(position, len(order)), object_id)
        slots = iter(order)
        placed: dict[str, dict[str, Any]] = {}
        for k, o in self.objects.items():
            key = next(slots) if o["diagram_id"] == diagram else k
            placed[key] = self.objects[key]
        self.objects = placed

    def positions(self) -> dict[str, int]:
        """Each diagram object's index in its diagram's object list (its stored position)."""
        seen: dict[str, int] = {}
        out: dict[str, int] = {}
        for k, o in self.objects.items():
            out[k] = seen.get(o["diagram_id"], 0)
            seen[o["diagram_id"]] = out[k] + 1
        return out

    def delete(self, kind: str, row_id: str) -> None:
        if kind == "uml_element":
            gone = self._owned_closure(row_id)
            for e in gone:
                self.elements.pop(e, None)
            for iid in [i for i, d in self.interactions.items() if d.get("owner_id") in gone]:
                del self.interactions[iid]
            for rid in [r for r, d in self.relationships.items() if _ends(d) & gone]:
                del self.relationships[rid]
        else:
            self.table(kind).pop(row_id, None)
        self.prune()

    def _owned_closure(self, root: str) -> set[str]:
        gone = {root}
        grew = True
        while grew:
            grew = False
            for eid, e in self.elements.items():
                if eid not in gone and e.get("owner_id") in gone:
                    gone.add(eid)
                    grew = True
        return gone

    def prune(self) -> None:
        """Drop occurrences whose diagram, concept, lifeline, message or end object is gone."""
        lifelines = {ll["id"] for i in self.interactions.values() for ll in i.get("lifelines", [])}
        messages = {m["id"] for i in self.interactions.values() for m in i.get("messages", [])}
        for oid, o in list(self.objects.items()):
            if (
                o["diagram_id"] not in self.diagrams
                or (o.get("element_ref") is not None and o["element_ref"] not in self.elements)
                or (o.get("lifeline_id") is not None and o["lifeline_id"] not in lifelines)
            ):
                del self.objects[oid]
        for cid, c in list(self.connections.items()):
            if (
                c["diagram_id"] not in self.diagrams
                or (
                    c.get("relationship_ref") is not None
                    and c["relationship_ref"] not in self.relationships
                )
                or (c.get("message_id") is not None and c["message_id"] not in messages)
                or c["source_object_id"] not in self.objects
                or c["target_object_id"] not in self.objects
            ):
                del self.connections[cid]


def interactions_size(interactions: Iterable[dict[str, Any]]) -> int:
    """Interactions plus the parts of their documents."""
    return sum(1 + sum(len(i.get(part) or []) for part in _INTERACTION_PARTS) for i in interactions)


def manifest_size(manifest: dict[str, Any]) -> int:
    """:meth:`UmlDraft.size` of a UML manifest (a sealed revision)."""
    diagrams = manifest.get("diagrams") or []
    return (
        len(manifest.get("elements", []))
        + len(manifest.get("relationships", []))
        + interactions_size(manifest.get("interactions", []))
        + len(diagrams)
        + sum(len(d.get("objects", [])) + len(d.get("connections", [])) for d in diagrams)
    )


def _ends(rel: dict[str, Any]) -> set[str]:
    refs = [rel.get("source"), rel.get("target")]
    refs += [e.get("element_ref") for e in rel.get("ends") or [] if isinstance(e, dict)]
    return {r for r in refs if isinstance(r, str)}


# ---------------------------------------------------------------------------
# The write gate
# ---------------------------------------------------------------------------


def is_gated(f: Finding) -> bool:
    """True if a write may be refused for ``f`` (see the module docstring)."""
    if f.severity != "error":
        return False
    if f.rule_id != "UML-UNSUP-001":
        return True
    first = f.targets[0].kind if f.targets else None
    if first in ("diagram_object", "diagram_connection"):
        return True
    return first in _ROW_TARGETS and f.path in _ROW_KIND_PATHS


def _key(f: Finding) -> tuple:
    """A finding's identity for "introduced": rule, targets and position — not its message
    or evidence, which restate values (an already-invalid multiplicity edited to another
    invalid one is the same finding, not a new one)."""
    return (f.rule_id, tuple(sorted((t.kind, t.id) for t in f.targets)), f.path)


def introduced_errors(
    before: UmlSnapshot, after: UmlSnapshot, *, gated_only: bool = True
) -> list[Finding]:
    """Error findings of ``after`` that ``before`` does not have (all rules, both sides).

    ``gated_only``: keep only what a single write is refused for (:func:`is_gated`);
    ``False`` keeps every new error (a change set's "introduces an error the head does not
    already have", spec §9.5).
    """
    old = {_key(f) for f in validate_uml(before).findings}
    return [
        f
        for f in validate_uml(after).findings
        if f.severity == "error" and (is_gated(f) if gated_only else True) and _key(f) not in old
    ]


def gate(before: UmlDraft, after: UmlDraft) -> list[Finding]:
    """The errors a write from ``before`` to ``after`` is refused for (CPU-bound; callers
    run it in a worker thread)."""
    return introduced_errors(before.snapshot(), after.snapshot())


def ref_positions(kind: str, data: dict[str, Any]) -> Iterable[tuple[Any, Any]]:
    """``(container, key)`` of every row/feature reference in a body of ``kind``.

    Used to substitute a change set's local references (``id_map``) wherever an id is
    expected — top-level refs and the nested ones (types, interfaces, lifeline types).
    """
    top = {
        "uml_element": ("owner_id",),
        "uml_relationship": ("source", "target"),
        "interaction": ("owner_id",),
        "diagram": (),
        "diagram_object": ("element_ref", "diagram_id"),
        "diagram_connection": (
            "relationship_ref",
            "source_object_id",
            "target_object_id",
            "diagram_id",
        ),
    }[kind]
    for key in top:
        if key in data:
            yield data, key
    if kind == "uml_relationship":
        for end in data.get("ends") or []:
            if isinstance(end, dict) and "element_ref" in end:
                yield end, "element_ref"
    elif kind == "interaction":
        for ll in data.get("lifelines") or []:
            if isinstance(ll, dict) and "represents_ref" in ll:
                yield ll, "represents_ref"
    elif kind == "uml_element":
        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            return
        for p in attrs.get("properties") or []:
            if isinstance(p, dict) and "type_ref" in p:
                yield p, "type_ref"
        for op in attrs.get("operations") or []:
            if not isinstance(op, dict):
                continue
            if "return_type_ref" in op:
                yield op, "return_type_ref"
            for q in op.get("parameters") or []:
                if isinstance(q, dict) and "type_ref" in q:
                    yield q, "type_ref"
        for group in ("provided", "required"):
            refs = attrs.get(group)
            if isinstance(refs, list):
                for n in range(len(refs)):
                    yield refs, n


def map_refs(kind: str, data: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    """A deep copy of ``data`` with every reference in ``id_map`` replaced."""
    out = copy.deepcopy(data)
    for container, key in list(ref_positions(kind, out)):
        value = container[key]
        if isinstance(value, str) and value in id_map:
            container[key] = id_map[value]
    return out
