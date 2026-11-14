"""Write-time ArchiMate integrity (target spec §7.3, layer 1).

Every ArchiMate write — a relationship create/update, an element retype, a
parent change — is checked the same way:

1. load the write's *neighbourhood* from the database: the concepts it
   touches, every visible ArchiMate relationship incident on them, the far
   ends of those relationships, one more hop through any junction (AM-JCT-*
   need the junction's other edges), and the specialization chain above any
   specialization end (AM-CYC-002);
2. run the pure ``archimate_rules.validate`` on that neighbourhood as it is
   now and as it would be after the write;
3. refuse with ``422 model_integrity`` the error findings that exist only
   AFTER — what this write breaks.

Step 3 is deliberate: 2,548 of 7,806 production element edges (2026-10-26)
predate the matrix gate and are illegal ArchiMate. Refusing every write that
merely touches one of them would freeze those elements; reporting them is a
validation run's job. A write may never make the model worse.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.architecture import ArchitectureElement, ArchitectureRelationship
from repositories.visibility import can_mutate, is_visible_to, scope
from schemas.modeling import (
    ArchimateConceptIn,
    ArchimateRelationshipIn,
    ArchimateSnapshot,
    Finding,
)
from services.model_integrity import integrity_error, single_finding
from settings import get_settings
from tools.architecture.archimate_rules import validate as validate_archimate
from tools.architecture.registry import ELEMENT_TYPE_TO_LAYER, get_registry

# Entity types whose rows are ArchiMate concepts. A relationship may be the end
# of another relationship where the language permits it (finding A10).
ARCHIMATE_ENDPOINT_TYPES = frozenset({"architecture_elements", "architecture_relationships"})

# Rules a single write can be refused on. Unused/duplicate/view rules belong to
# validation runs, not to one write.
WRITE_TIME_RULES = (
    "AM-TYP-001",
    "AM-TYP-002",
    "AM-REL-001",
    "AM-REL-002",
    "AM-REL-003",
    "AM-JCT-001",
    "AM-JCT-002",
    "AM-CYC-002",
)
NEW_RELATIONSHIP = "new-relationship"

# Bound on neighbourhood loading. A write whose neighbourhood exceeds it is
# refused rather than checked against a truncated graph.
_MAX_NEIGHBOURHOOD = 5000
_MAX_SPECIALIZATION_DEPTH = 100


def concept_of(row: ArchitectureElement, *, element_type: str | None = None) -> ArchimateConceptIn:
    props = row.properties if isinstance(row.properties, dict) else {}
    etype = element_type or row.element_type
    return ArchimateConceptIn(
        id=str(row.id),
        type=etype,
        name=row.title or "",
        layer=ELEMENT_TYPE_TO_LAYER.get(etype, row.layer) if element_type else row.layer,
        junction_type=_text(props.get("junction_type")) if etype == "junction" else None,
    )


def relationship_of(row: ArchitectureRelationship) -> ArchimateRelationshipIn:
    props = row.properties if isinstance(row.properties, dict) else {}
    return ArchimateRelationshipIn(
        id=str(row.id),
        type=row.relationship_type,
        source=str(row.source_id),
        target=str(row.target_id),
        **_stored_attributes(props),
    )


# Relationship ``properties`` are free-form JSONB; only these keys feed the
# rules, each with the type ArchimateRelationshipIn declares.
_ATTRIBUTE_TYPES: dict[str, str] = {
    "access_type": "a string",
    "strength": "a string of at most 50 characters",
    "directed": "a boolean",
}


def _text(value: Any, limit: int | None = None) -> str | None:
    if isinstance(value, str) and (limit is None or len(value) <= limit):
        return value
    return None


def _attribute(key: str, value: Any) -> Any:
    """``value`` if it has the schema's type, else None."""
    if key == "directed":
        return value if isinstance(value, bool) else None
    return _text(value, 50 if key == "strength" else None)


def _stored_attributes(props: dict[str, Any]) -> dict[str, Any]:
    # Lenient for stored rows: a legacy edge with an off-schema attribute is
    # read as if the attribute were absent, so it cannot fail writes elsewhere
    # in its neighbourhood.
    return {k: _attribute(k, props.get(k)) for k in _ATTRIBUTE_TYPES}


def _proposed_attributes(props: dict[str, Any]) -> dict[str, Any]:
    # Strict for the write being checked: an off-schema attribute is a 422.
    bad = [
        k for k in _ATTRIBUTE_TYPES if props.get(k) is not None and _attribute(k, props[k]) is None
    ]
    if bad:
        raise integrity_error(
            [
                single_finding(
                    "AM-REL-003",
                    get_registry().ruleset_id,
                    f"properties.{k} must be {_ATTRIBUTE_TYPES[k]}",
                    path=f"/properties/{k}",
                )
                for k in bad
            ]
        )
    return {k: props.get(k) for k in _ATTRIBUTE_TYPES}


def is_archimate_relationship(row: ArchitectureRelationship) -> bool:
    return (
        row.relationship_type in get_registry().relationship_types
        and row.source_entity_type in ARCHIMATE_ENDPOINT_TYPES
        and row.target_entity_type in ARCHIMATE_ENDPOINT_TYPES
    )


class _Neighbourhood:
    def __init__(self, session: AsyncSession, *, tenant_id: str, subject_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.subject_id = subject_id or tenant_id
        self.system_tenant_id = get_settings().system_tenant_id
        self.elements: dict[str, ArchitectureElement] = {}
        self.relationships: dict[str, ArchitectureRelationship] = {}

    def visible(self, row: Any) -> bool:
        return is_visible_to(
            row,
            tenant_id=self.tenant_id,
            subject_id=self.subject_id,
            system_tenant_id=self.system_tenant_id,
        )

    def _check_size(self, loaded: int = 0) -> None:
        if loaded + len(self.elements) + len(self.relationships) > _MAX_NEIGHBOURHOOD:
            raise integrity_error(
                [
                    single_finding(
                        "AM-REF-001",
                        get_registry().ruleset_id,
                        f"the write touches more than {_MAX_NEIGHBOURHOOD} concepts and "
                        "relationships; refusing rather than checking a truncated model",
                    )
                ]
            )

    async def add_elements(self, ids: set[str]) -> None:
        missing = {i for i in ids if i not in self.elements}
        if not missing:
            return
        rows = (
            (
                await self.session.execute(
                    select(ArchitectureElement).where(
                        ArchitectureElement.id.in_([UUID(i) for i in missing])
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if self.visible(row):
                self.elements[str(row.id)] = row
        self._check_size()

    async def add_relationships(self, ids: set[str]) -> None:
        missing = {i for i in ids if i not in self.relationships}
        if not missing:
            return
        rows = (
            (
                await self.session.execute(
                    select(ArchitectureRelationship).where(
                        ArchitectureRelationship.id.in_([UUID(i) for i in missing])
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if self.visible(row) and is_archimate_relationship(row):
                self.relationships[str(row.id)] = row
        self._check_size()

    async def incident(self, ids: set[str]) -> list[ArchitectureRelationship]:
        if not ids:
            return []
        uuids = [UUID(i) for i in ids]
        rows = (
            (
                await self.session.execute(
                    scope(
                        select(ArchitectureRelationship).where(
                            or_(
                                ArchitectureRelationship.source_id.in_(uuids),
                                ArchitectureRelationship.target_id.in_(uuids),
                            ),
                            ArchitectureRelationship.source_entity_type.in_(
                                ARCHIMATE_ENDPOINT_TYPES
                            ),
                            ArchitectureRelationship.target_entity_type.in_(
                                ARCHIMATE_ENDPOINT_TYPES
                            ),
                        ),
                        ArchitectureRelationship,
                        tenant_id=self.tenant_id,
                        subject_id=self.subject_id,
                    ).limit(_MAX_NEIGHBOURHOOD + 1)
                )
            )
            .scalars()
            .all()
        )
        # A hub past the bound is refused here, before its edges are held.
        self._check_size(len({str(r.id) for r in rows} - self.relationships.keys()))
        return [r for r in rows if is_archimate_relationship(r)]

    async def _add_far_ends(self, rels: list[ArchitectureRelationship]) -> None:
        for r in rels:
            self.relationships.setdefault(str(r.id), r)
        el_ids: set[str] = set()
        rel_ids: set[str] = set()
        for r in rels:
            for etype, ref in (
                (r.source_entity_type, r.source_id),
                (r.target_entity_type, r.target_id),
            ):
                (el_ids if etype == "architecture_elements" else rel_ids).add(str(ref))
        await self.add_elements(el_ids)
        await self.add_relationships(rel_ids)
        self._check_size()

    async def add_edited(self, rel_id: str) -> None:
        """The relationship being updated, with its stored (old) ends."""
        await self.add_relationships({rel_id})
        row = self.relationships.get(rel_id)
        if row is not None:
            await self._add_far_ends([row])

    async def expand(self, seed_ids: set[str]) -> None:
        """Incident edges of the seeds, their far ends, one hop through junctions."""
        await self._add_far_ends(await self.incident(seed_ids))
        junctions = {i for i, e in self.elements.items() if e.element_type == "junction"}
        await self._add_far_ends(await self.incident(junctions))

    async def add_specialization_chain(self, start_ids: set[str]) -> None:
        frontier = {i for i in start_ids if i in self.elements}
        seen = set(frontier)
        for _ in range(_MAX_SPECIALIZATION_DEPTH):
            if not frontier:
                return
            rels = [
                r for r in await self.incident(frontier) if r.relationship_type == "specialization"
            ]
            await self._add_far_ends(rels)
            nxt = {
                str(r.target_id)
                for r in rels
                if str(r.source_id) in frontier and r.target_entity_type == "architecture_elements"
            }
            frontier = nxt - seen
            seen |= nxt

    def snapshot(
        self,
        *,
        retype: tuple[str, str] | None = None,
        replace: ArchimateRelationshipIn | None = None,
        drop: str | None = None,
    ) -> ArchimateSnapshot:
        concepts = [
            concept_of(e, element_type=retype[1] if retype and retype[0] == i else None)
            for i, e in self.elements.items()
        ]
        rels = [relationship_of(r) for i, r in self.relationships.items() if i != drop]
        if replace is not None:
            rels.append(replace)
        return ArchimateSnapshot(concepts=concepts, relationships=rels)


async def resolve_visible(
    session: AsyncSession,
    *,
    tenant_id: str,
    subject_id: str,
    element_ids: set[str],
    relationship_ids: set[str],
) -> tuple[dict[str, ArchitectureElement], dict[str, ArchitectureRelationship]]:
    """The rows among ``element_ids`` / ``relationship_ids`` the caller can see.

    The same resolution every ArchiMate write uses: an id of another tenant or of
    another subject's private row is absent, exactly like a missing one.
    """
    hood = _Neighbourhood(session, tenant_id=tenant_id, subject_id=subject_id)
    await hood.add_elements(element_ids)
    await hood.add_relationships(relationship_ids)
    return hood.elements, hood.relationships


# Rules that can fire more than once, in different ways, on ONE relationship: their
# targets alone do not tell an old finding from a new one, so the key also carries
# the offending attribute/value (AM-REL-003) or the ends (AM-REL-002). Otherwise a
# row with one stale finding could be updated into a different violation unrefused.
_EVIDENCE_KEYED_RULES = frozenset({"AM-REL-002", "AM-REL-003"})


def _key(f: Finding) -> tuple:
    key: tuple = (f.rule_id, tuple(sorted(t.id for t in f.targets)))
    if f.rule_id in _EVIDENCE_KEYED_RULES:
        evidence = json.dumps(f.evidence or {}, sort_keys=True, default=str)
        key += (f.path, evidence)
    return key


def _junction_types(f: Finding) -> tuple[str, frozenset[str]]:
    return f.targets[0].id, frozenset((f.evidence or {}).get("types", ()))


def _new_errors(
    before: ArchimateSnapshot,
    after: ArchimateSnapshot,
    *,
    rules: tuple[str, ...] = WRITE_TIME_RULES,
) -> list[Finding]:
    old_findings = validate_archimate(before, rules=rules).findings
    old = {_key(f) for f in old_findings if f.rule_id != "AM-JCT-001"}
    # A junction that was already mixed stays a finding of runs; a write is
    # refused only if it adds a NEW type to the mix. Reducing the mix (a type
    # set that is a subset of one the junction had before) is not new.
    old_junctions: dict[str, list[frozenset[str]]] = {}
    for f in old_findings:
        if f.rule_id == "AM-JCT-001":
            jid, types = _junction_types(f)
            old_junctions.setdefault(jid, []).append(types)

    def introduced(f: Finding) -> bool:
        if f.rule_id == "AM-JCT-001":
            jid, types = _junction_types(f)
            return not any(types <= prev for prev in old_junctions.get(jid, ()))
        return _key(f) not in old

    return [
        f
        for f in validate_archimate(after, rules=rules).findings
        if f.severity == "error" and introduced(f)
    ]


# The before/after diff as a public entry point (view occurrences use it with view rules).
introduced_errors = _new_errors


def _unresolved(end: str, ref: Any, noun: str) -> Exception:
    return integrity_error(
        [
            single_finding(
                "AM-REF-001",
                get_registry().ruleset_id,
                f"{end} {noun} {ref} does not resolve to a visible ArchiMate concept",
                path=f"/{end}_id",
                evidence={f"{end}_id": str(ref)},
            )
        ]
    )


def _assert_one_model(
    hood: _Neighbourhood, ends: dict[str, str], source_type: str, target_type: str
) -> None:
    """AM-REF-003: both ends of an ArchiMate relationship are in one model (spec §7.1).

    The relationship itself joins its source end's model (``models/modeling.py``), so
    this is the whole rule.
    """
    models = {}
    for end, etype in (("source", source_type), ("target", target_type)):
        row = (hood.elements if etype == "architecture_elements" else hood.relationships)[ends[end]]
        models[end] = row.model_id
    if None in models.values() or models["source"] == models["target"]:
        return
    raise integrity_error(
        [
            single_finding(
                "AM-REF-003",
                get_registry().ruleset_id,
                f"source {ends['source']} is in model {models['source']} but target "
                f"{ends['target']} is in model {models['target']}; a relationship and its "
                "ends belong to one model",
                targets=[
                    (
                        "relationship" if source_type != "architecture_elements" else "element",
                        ends["source"],
                    ),
                    (
                        "relationship" if target_type != "architecture_elements" else "element",
                        ends["target"],
                    ),
                ],
                path="/target_id",
                evidence={
                    "source_model_id": str(models["source"]),
                    "target_model_id": str(models["target"]),
                },
            )
        ]
    )


async def check_relationship_write(
    session: AsyncSession,
    *,
    tenant_id: str,
    subject_id: str,
    source_entity_type: str,
    source_id: Any,
    target_entity_type: str,
    target_id: Any,
    relationship_type: str,
    properties: dict[str, Any] | None,
    relationship_id: Any = None,
    properties_sent: bool = True,
) -> None:
    """Refuse an ArchiMate relationship write that would make the model worse.

    Edges with a non-ArchiMate end (requirement, principle, decision, PM, TOC)
    are other domains' links and are not checked here. An unresolved or
    invisible ArchiMate end is ``422 AM-REF-001`` — another subject's private
    row answers exactly like a missing one, so existence never leaks.
    """
    if not (
        source_entity_type in ARCHIMATE_ENDPOINT_TYPES
        and target_entity_type in ARCHIMATE_ENDPOINT_TYPES
    ):
        return
    hood = _Neighbourhood(session, tenant_id=tenant_id, subject_id=subject_id)
    ends: dict[str, str] = {}
    for end, etype, ref in (
        ("source", source_entity_type, source_id),
        ("target", target_entity_type, target_id),
    ):
        ref = str(ref)
        if etype == "architecture_elements":
            await hood.add_elements({ref})
            if ref not in hood.elements:
                raise _unresolved(end, ref, "architecture element")
        else:
            row = await session.get(ArchitectureRelationship, UUID(ref))
            if row is None or not hood.visible(row):
                raise _unresolved(end, ref, "relationship")
            if not is_archimate_relationship(row):
                raise integrity_error(
                    [
                        single_finding(
                            "AM-TYP-002",
                            get_registry().ruleset_id,
                            f"{end} relationship {ref} is a {row.relationship_type} link "
                            "between non-ArchiMate entities; an ArchiMate relationship "
                            "cannot end on it",
                            path=f"/{end}_id",
                            evidence={"relationship_type": row.relationship_type},
                        )
                    ]
                )
            hood.relationships[ref] = row
            await hood.add_elements({str(row.source_id), str(row.target_id)})
        ends[end] = ref

    _assert_one_model(hood, ends, source_entity_type, target_entity_type)

    rel_id = str(relationship_id) if relationship_id is not None else NEW_RELATIONSHIP
    if relationship_id is not None:
        # An update: hold the edited row with its OLD ends, so findings it
        # already carries are in "before" even when both ends move and it is
        # no longer incident on the new ones.
        await hood.add_edited(rel_id)
    await hood.expand(set(ends.values()))
    if relationship_type == "specialization":
        await hood.add_specialization_chain({ends["target"]})

    # ``properties_sent=False``: an update kept the stored properties, which
    # are read like any other stored row rather than refused as client input.
    props = properties if isinstance(properties, dict) else {}
    attributes = _proposed_attributes(props) if properties_sent else _stored_attributes(props)
    proposed = ArchimateRelationshipIn(
        id=rel_id,
        type=relationship_type,
        source=ends["source"],
        target=ends["target"],
        **attributes,
    )
    errors = _new_errors(
        hood.snapshot(),
        hood.snapshot(replace=proposed, drop=rel_id),
    )
    if errors:
        for f in errors:
            # The placeholder is not an identifier the caller can use.
            f.targets = [t for t in f.targets if t.id != NEW_RELATIONSHIP]
        raise integrity_error(errors)


async def assert_parent_resolves(
    session: AsyncSession,
    *,
    parent_id: UUID | None,
    tenant_id: str,
    subject_id: str,
) -> None:
    """A ``parent_id`` must name an element the caller can see (finding A06)."""
    if parent_id is None:
        return
    row = await session.get(ArchitectureElement, parent_id)
    if row is None or not is_visible_to(
        row,
        tenant_id=tenant_id,
        subject_id=subject_id or tenant_id,
        system_tenant_id=get_settings().system_tenant_id,
    ):
        raise integrity_error(
            [
                single_finding(
                    "AM-REF-001",
                    get_registry().ruleset_id,
                    f"parent {parent_id} does not resolve to a visible architecture element",
                    path="/parent_id",
                    evidence={"parent_id": str(parent_id)},
                )
            ]
        )


async def assert_retype_keeps_relationships_valid(
    session: AsyncSession,
    *,
    element_id: UUID,
    new_type: str,
    tenant_id: str,
    subject_id: str,
) -> None:
    """Refuse an ``element_type`` change that breaks the element's neighbourhood (A06).

    Covers edges to elements AND to relationships, and the junctions the
    element feeds. Silent when the element is missing, invisible or not the
    caller's to change, so the update itself answers 404/403 as before.
    """
    sub = subject_id or tenant_id
    element = await session.get(ArchitectureElement, element_id)
    if (
        element is None
        or element.element_type == new_type
        or not is_visible_to(
            element,
            tenant_id=tenant_id,
            subject_id=sub,
            system_tenant_id=get_settings().system_tenant_id,
        )
        or not can_mutate(element, subject_id=sub, tenant_id=tenant_id)
    ):
        return
    hood = _Neighbourhood(session, tenant_id=tenant_id, subject_id=subject_id)
    eid = str(element_id)
    hood.elements[eid] = element
    await hood.expand({eid})
    errors = _new_errors(hood.snapshot(), hood.snapshot(retype=(eid, new_type)))
    if errors:
        raise integrity_error(errors)
