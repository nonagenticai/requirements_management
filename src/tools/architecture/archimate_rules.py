"""Pure ArchiMate 3.2 validator: ``validate(snapshot, rules) -> ValidationReport``.

One function backs every entry point — write-time integrity, persisted
validation runs and the stateless ``POST /architecture/tools/validate``
(target spec §7.3). No database, no I/O, deterministic output.

Rule families and where each comes from:

- ``AM-REF``/``AM-TYP``/``AM-CYC`` — repository integrity (this service).
- ``AM-REL`` — Archi ``InvalidRelationsChecker`` via ``ArchimateModelUtils.
  isValidRelationship``: the full relationship matrix, plus "a relationship may
  not connect to one of its own ends" (``hasDirectRelationship``), plus the
  relationship-specific attributes.
- ``AM-JCT`` — Archi ``JunctionsChecker`` and the junction branch of
  ``isValidRelationship``. ⚠️ Archi disagrees with itself here: the model utility
  exempts a Grouping/Location aggregation or composition into a junction from
  the "one relationship type per junction" rule (ArchiMate 3.2 Appendix B),
  while Hammer's checker counts every incident relationship. This module
  follows the model utility and the specification; the disagreement is pinned
  by a test so a future Archi change is noticed rather than absorbed.
- ``AM-VPT``/``AM-NST``/``AM-UNU``/``AM-EMV``/``AM-DUP`` — Archi's
  ``ViewpointChecker``, ``NestedElementsChecker``, ``UnusedElementsChecker``,
  ``UnusedRelationsChecker``, ``EmptyViewsChecker``, ``DuplicateElementChecker``
  with Archi's default severities.
- ``AM-VIEW`` — occurrence integrity (target spec §8.1).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from schemas.modeling import (
    ArchimateConceptIn,
    ArchimateRelationshipIn,
    ArchimateSnapshot,
    Finding,
    FindingTarget,
    RuleInfo,
    RulesetInfo,
    Severity,
    TargetKind,
    ValidationReport,
    build_report,
)
from tools.architecture.registry import (
    ACCESS_TYPES,
    JUNCTION_TYPES,
    RELATIONSHIP_ENDPOINT,
    Registry,
    get_registry,
)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    severity: Severity
    required: bool
    needs_views: bool
    source: str
    description: str


_ARCHI = "Archi 5.10.0"
RULES: tuple[Rule, ...] = (
    Rule(
        "AM-REF-001",
        "Unresolved reference",
        "error",
        True,
        False,
        "repository integrity",
        "Every relationship end, parent, view object concept, connection relationship and connection end resolves within the snapshot.",
    ),
    Rule(
        "AM-REF-002",
        "Duplicate identifier",
        "error",
        True,
        False,
        "repository integrity",
        "Identifiers are unique across concepts, relationships, views, objects and connections.",
    ),
    Rule(
        "AM-REF-003",
        "Relationship crosses models",
        "error",
        False,
        False,
        "target spec §7.1 (write-time only)",
        "An ArchiMate relationship and both its ends belong to one model. Enforced when a "
        "relationship is created or re-pointed; a snapshot is one model by construction, so "
        "validation runs never report it.",
    ),
    Rule(
        "AM-TYP-001",
        "Unknown or misclassified concept",
        "error",
        True,
        False,
        "ArchiMate 3.2 / Archi concept registry",
        "The concept type exists in the registry and any supplied layer matches the registry's layer for it.",
    ),
    Rule(
        "AM-TYP-002",
        "Not an ArchiMate relationship type",
        "error",
        True,
        False,
        "ArchiMate 3.2 §5",
        "Only the 11 ArchiMate relationship types are valid between ArchiMate concepts. PM scheduling, TOC and traceability links belong to their own domains.",
    ),
    Rule(
        "AM-REL-001",
        "Invalid relationship",
        "error",
        True,
        False,
        f"{_ARCHI} InvalidRelationsChecker + relationships.xml",
        "The (source type, relationship type, target type) triple is permitted by the relationship matrix.",
    ),
    Rule(
        "AM-REL-002",
        "Relationship connected to its own end",
        "error",
        True,
        False,
        f"{_ARCHI} ArchimateModelUtils.hasDirectRelationship",
        "A relationship cannot connect a concept to a relationship that already has that concept as an end, nor to itself.",
    ),
    Rule(
        "AM-REL-003",
        "Invalid relationship attribute",
        "error",
        True,
        False,
        "ArchiMate 3.2 §5.2-5.4",
        "access_type only on access (write|read|access|readwrite); strength only on influence; directed only on association; junction_type only on junctions (and|or).",
    ),
    Rule(
        "AM-JCT-001",
        "Junction mixes relationship types",
        "error",
        True,
        False,
        f"{_ARCHI} JunctionsChecker",
        "All relationships connected to a junction have the same type, except aggregation/composition from a Grouping or Location.",
    ),
    Rule(
        "AM-JCT-002",
        "Invalid relationship through a junction",
        "error",
        True,
        False,
        f"{_ARCHI} ArchimateModelUtils.isValidRelationship (junction branch)",
        "For every incoming source and outgoing target of a junction, the junction's relationship type is permitted directly between them.",
    ),
    Rule(
        "AM-CYC-001",
        "Containment cycle",
        "error",
        True,
        False,
        "repository integrity",
        "The organisational parent chain of a concept never returns to the concept.",
    ),
    Rule(
        "AM-CYC-002",
        "Specialization cycle",
        "error",
        True,
        False,
        "ArchiMate 3.2 §5.4.1",
        "A concept cannot be its own (transitive) specialization.",
    ),
    Rule(
        "AM-VIEW-001",
        "Unknown viewpoint",
        "error",
        True,
        True,
        "target spec §8.1",
        "A view's viewpoint is one of the registry's viewpoints, or unset.",
    ),
    Rule(
        "AM-VIEW-002",
        "Connection does not show its relationship",
        "error",
        True,
        True,
        "target spec §8.1",
        "A connection's source and target occurrences show the relationship's source and target, in that order.",
    ),
    Rule(
        "AM-VIEW-003",
        "Invalid visual nesting",
        "error",
        True,
        True,
        "repository integrity",
        "A view object's visual parent is an object of the same view and the parent chain has no cycle.",
    ),
    Rule(
        "AM-VPT-001",
        "Concept not allowed in viewpoint",
        "warning",
        False,
        True,
        f"{_ARCHI} ViewpointChecker",
        "Every element shown in a view is allowed by the view's viewpoint.",
    ),
    Rule(
        "AM-NST-001",
        "Nested without a valid relationship",
        "advice",
        False,
        True,
        f"{_ARCHI} NestedElementsChecker",
        "A visually nested element is connected to its visual parent by a structural or dependency relationship in the right direction.",
    ),
    Rule(
        "AM-UNU-001",
        "Element not used in any view",
        "warning",
        False,
        True,
        f"{_ARCHI} UnusedElementsChecker",
        "Every element appears in at least one view.",
    ),
    Rule(
        "AM-UNU-002",
        "Relationship not used in any view",
        "warning",
        False,
        True,
        f"{_ARCHI} UnusedRelationsChecker",
        "Every relationship appears in at least one view.",
    ),
    Rule(
        "AM-EMV-001",
        "Empty view",
        "advice",
        False,
        True,
        f"{_ARCHI} EmptyViewsChecker",
        "A view contains at least one object.",
    ),
    Rule(
        "AM-DUP-001",
        "Possible duplicate element",
        "warning",
        False,
        False,
        f"{_ARCHI} DuplicateElementChecker",
        "Two elements share a name and a type (junctions excepted). Reported, never merged.",
    ),
)
RULES_BY_ID: dict[str, Rule] = {r.rule_id: r for r in RULES}
# Rules the write gate enforces against stored rows; the pure validator cannot see them
# (they are about membership in a model, which a one-model snapshot does not carry).
WRITE_TIME_ONLY_RULES: frozenset[str] = frozenset({"AM-REF-003"})
REQUIRED_RULES: frozenset[str] = frozenset(r.rule_id for r in RULES if r.required)

# Archi NestedElementsChecker.isNestedTypeRelationship.
_NESTING_RELATIONSHIPS = frozenset(
    {"composition", "specialization", "aggregation", "assignment", "realization", "access"}
)
_STRUCTURAL_FROM_CONTAINER = frozenset({"composition", "aggregation"})


def ruleset_info(*, with_rules: bool = True) -> RulesetInfo:
    reg = get_registry()
    return RulesetInfo(
        ruleset_id=reg.ruleset_id,
        language="archimate",
        language_version=reg.language_version,
        reference=reg.reference,
        rule_count=len(RULES),
        rules=[
            RuleInfo(
                rule_id=r.rule_id,
                title=r.title,
                default_severity=r.severity,
                required=r.required,
                description=r.description,
                source=r.source,
            )
            for r in RULES
        ]
        if with_rules
        else [],
    )


class UnknownRuleError(ValueError):
    pass


def resolve_enabled(
    *, rules: Iterable[str] | None, disable: Iterable[str] | None, views_supplied: bool
) -> list[str]:
    selected = list(rules) if rules is not None else [r.rule_id for r in RULES]
    off = set(disable or ())
    unknown = sorted((set(selected) | off) - set(RULES_BY_ID))
    if unknown:
        raise UnknownRuleError(f"unknown rule ids: {', '.join(unknown)}")
    return [
        rid
        for rid in selected
        if rid not in off and (views_supplied or not RULES_BY_ID[rid].needs_views)
    ]


class _Ctx:
    def __init__(self, snap: ArchimateSnapshot, reg: Registry, enabled: set[str]) -> None:
        self.snap = snap
        self.reg = reg
        self.enabled = enabled
        self.findings: list[Finding] = []
        self.concepts: dict[str, ArchimateConceptIn] = {}
        self.relationships: dict[str, ArchimateRelationshipIn] = {}
        for c in snap.concepts:
            self.concepts.setdefault(c.id, c)
        for r in snap.relationships:
            self.relationships.setdefault(r.id, r)

    def emit(
        self,
        rule_id: str,
        targets: list[tuple[TargetKind, str]],
        message: str,
        *,
        path: str | None = None,
        evidence: dict | None = None,
    ) -> None:
        if rule_id not in self.enabled:
            return
        self.findings.append(
            Finding(
                rule_id=rule_id,
                ruleset_id=self.reg.ruleset_id,
                severity=RULES_BY_ID[rule_id].severity,
                targets=[FindingTarget(kind=k, id=i) for k, i in targets],
                path=path,
                message=message,
                evidence=evidence,
            )
        )

    def endpoint_type(self, ref: str) -> str | None:
        """Registry type of a relationship end: concept type, or the generic relationship."""
        if ref in self.concepts:
            return self.concepts[ref].type
        if ref in self.relationships:
            return RELATIONSHIP_ENDPOINT
        return None

    def kind_of(self, ref: str) -> TargetKind:
        return "relationship" if ref in self.relationships else "element"


def _check_identity(ctx: _Ctx) -> None:
    seen: dict[str, str] = {}
    items: list[tuple[TargetKind, str]] = [("element", c.id) for c in ctx.snap.concepts]
    items += [("relationship", r.id) for r in ctx.snap.relationships]
    for v in ctx.snap.views or []:
        items.append(("view", v.id))
        items += [("view_object", o.id) for o in v.objects]
        items += [("view_connection", c.id) for c in v.connections]
    for kind, ident in items:
        if ident in seen:
            ctx.emit(
                "AM-REF-002",
                [(kind, ident)],
                f"identifier {ident} is used by more than one {seen[ident]}/{kind}",
            )
        else:
            seen[ident] = kind


def _check_concepts(ctx: _Ctx) -> None:
    reg = ctx.reg
    for c in ctx.snap.concepts:
        concept = reg.concepts.get(c.type)
        if concept is None:
            ctx.emit(
                "AM-TYP-001",
                [("element", c.id)],
                f"unknown ArchiMate element type '{c.type}'",
                path="/type",
                evidence={"type": c.type},
            )
        elif c.layer is not None and c.layer != concept.layer:
            ctx.emit(
                "AM-TYP-001",
                [("element", c.id)],
                f"element type '{c.type}' belongs to layer '{concept.layer}', not '{c.layer}'",
                path="/layer",
                evidence={"type": c.type, "layer": c.layer, "expected_layer": concept.layer},
            )
        if c.junction_type is not None and (
            c.type != "junction" or c.junction_type not in JUNCTION_TYPES
        ):
            ctx.emit(
                "AM-REL-003",
                [("element", c.id)],
                f"junction_type '{c.junction_type}' is only valid on a junction, as one of {list(JUNCTION_TYPES)}",
                path="/junction_type",
                evidence={"attribute": "junction_type", "value": c.junction_type},
            )
        if c.parent_id is not None and c.parent_id not in ctx.concepts:
            ctx.emit(
                "AM-REF-001",
                [("element", c.id)],
                f"parent {c.parent_id} of element {c.id} does not resolve",
                path="/parent_id",
            )

    # AM-CYC-001: walk each parent chain once, colouring as we go.
    state: dict[str, int] = {}
    for start in ctx.concepts:
        chain: list[str] = []
        node: str | None = start
        while node is not None and node in ctx.concepts and state.get(node) is None:
            state[node] = 1
            chain.append(node)
            node = ctx.concepts[node].parent_id
        if node is not None and state.get(node) == 1:
            cycle = chain[chain.index(node) :]
            ctx.emit(
                "AM-CYC-001",
                [("element", i) for i in sorted(cycle)],
                f"parent chain forms a cycle through {len(cycle)} element(s)",
                path="/parent_id",
            )
        for n in chain:
            state[n] = 2


def _check_relationships(ctx: _Ctx) -> None:
    reg = ctx.reg
    for r in ctx.snap.relationships:
        rel_t: list[tuple[TargetKind, str]] = [("relationship", r.id)]
        if r.type not in reg.relationship_types:
            ctx.emit(
                "AM-TYP-002",
                rel_t,
                f"'{r.type}' is not an ArchiMate relationship type",
                path="/type",
                evidence={"type": r.type, "allowed": list(reg.relationship_types)},
            )
            continue
        # Attributes (AM-REL-003).
        if r.access_type is not None and (r.type != "access" or r.access_type not in ACCESS_TYPES):
            ctx.emit(
                "AM-REL-003",
                rel_t,
                f"access_type is only valid on access, as one of {list(ACCESS_TYPES)}",
                path="/access_type",
                evidence={"attribute": "access_type", "value": r.access_type},
            )
        if r.strength is not None and r.type != "influence":
            ctx.emit(
                "AM-REL-003",
                rel_t,
                "strength is only valid on influence",
                path="/strength",
                evidence={"attribute": "strength", "value": r.strength},
            )
        if r.directed is not None and r.type != "association":
            ctx.emit(
                "AM-REL-003",
                rel_t,
                "directed is only valid on association",
                path="/directed",
                evidence={"attribute": "directed", "value": r.directed},
            )

        src_t, tgt_t = ctx.endpoint_type(r.source), ctx.endpoint_type(r.target)
        for end, ref, typ in (("source", r.source, src_t), ("target", r.target, tgt_t)):
            if typ is None:
                ctx.emit(
                    "AM-REF-001",
                    rel_t,
                    f"{end} {ref} of relationship {r.id} does not resolve",
                    path=f"/{end}",
                )
        if src_t is None or tgt_t is None:
            continue
        if r.source == r.id or r.target == r.id or _connects_to_own_end(ctx, r):
            ctx.emit(
                "AM-REL-002",
                rel_t,
                f"relationship {r.id} connects to a relationship that already has that end",
                evidence={"source": r.source, "target": r.target},
            )
            continue
        if not reg.is_valid(src_t, r.type, tgt_t):
            ctx.emit(
                "AM-REL-001",
                rel_t + [(ctx.kind_of(r.source), r.source), (ctx.kind_of(r.target), r.target)],
                f"{r.type} is not permitted from {src_t} to {tgt_t}",
                evidence={
                    "source_type": src_t,
                    "relationship_type": r.type,
                    "target_type": tgt_t,
                    "allowed": sorted(reg.allowed(src_t, tgt_t)),
                },
            )

    # AM-CYC-002: specialization graph must be acyclic.
    spec = defaultdict(list)
    for r in ctx.snap.relationships:
        if r.type == "specialization" and r.source in ctx.concepts and r.target in ctx.concepts:
            spec[r.source].append(r.target)
    for cycle in _cycles(spec):
        ctx.emit(
            "AM-CYC-002",
            [("element", i) for i in sorted(cycle)],
            f"specialization forms a cycle through {len(cycle)} element(s)",
        )


def _connects_to_own_end(ctx: _Ctx, r: ArchimateRelationshipIn) -> bool:
    # Archi hasDirectRelationship: X -> R (or R -> X) where R already has X as an end.
    for a, b in ((r.source, r.target), (r.target, r.source)):
        other = ctx.relationships.get(a)
        if other is not None and b in (other.source, other.target):
            return True
    return False


def _cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    color: dict[str, int] = {}
    found: list[list[str]] = []
    for root in sorted(graph):
        if color.get(root):
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        while stack:
            node, idx = stack.pop()
            if idx == 0:
                color[node] = 1
                path.append(node)
            succ = graph.get(node, [])
            if idx < len(succ):
                stack.append((node, idx + 1))
                nxt = succ[idx]
                if color.get(nxt) == 1:
                    found.append(path[path.index(nxt) :])
                elif not color.get(nxt):
                    stack.append((nxt, 0))
            else:
                color[node] = 2
                path.pop()
    return found


def _is_container_structural(ctx: _Ctx, r: ArchimateRelationshipIn) -> bool:
    src = ctx.concepts.get(r.source)
    return (
        src is not None
        and src.type in ("grouping", "location")
        and r.type in _STRUCTURAL_FROM_CONTAINER
    )


def _check_junctions(ctx: _Ctx) -> None:
    incoming: dict[str, list[ArchimateRelationshipIn]] = defaultdict(list)
    outgoing: dict[str, list[ArchimateRelationshipIn]] = defaultdict(list)
    for r in ctx.snap.relationships:
        if r.type not in ctx.reg.relationship_types:
            continue
        outgoing[r.source].append(r)
        incoming[r.target].append(r)
    for c in ctx.snap.concepts:
        if c.type != "junction":
            continue
        rels = [r for r in incoming[c.id] + outgoing[c.id] if not _is_container_structural(ctx, r)]
        types = sorted({r.type for r in rels})
        if len(types) > 1:
            ctx.emit(
                "AM-JCT-001",
                [("element", c.id)]
                + [("relationship", r.id) for r in sorted(rels, key=lambda r: r.id)],
                f"junction {c.id} connects relationships of different types: {', '.join(types)}",
                evidence={"types": types},
            )
            continue
        for rin in incoming[c.id]:
            if _is_container_structural(ctx, rin):
                continue
            s_t = ctx.endpoint_type(rin.source)
            for rout in outgoing[c.id]:
                t_t = ctx.endpoint_type(rout.target)
                if s_t is None or t_t is None or rout.type != rin.type:
                    continue
                if not ctx.reg.is_valid(s_t, rin.type, t_t):
                    ctx.emit(
                        "AM-JCT-002",
                        [("element", c.id), ("relationship", rin.id), ("relationship", rout.id)],
                        f"{rin.type} through junction {c.id} is not permitted from {s_t} to {t_t}",
                        evidence={
                            "source_type": s_t,
                            "relationship_type": rin.type,
                            "target_type": t_t,
                        },
                    )


def _normalised_name(name: str | None) -> str:
    """Case- and whitespace-insensitive name key: ``" Order  Service"`` ~ ``"order service"``."""
    return " ".join((name or "").split()).casefold()


def _check_duplicates(ctx: _Ctx) -> None:
    # Concepts are identified by id (A12): same-name concepts are LEGAL and only
    # reported here, at warning severity — the write gate refuses errors only.
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    first_name: dict[tuple[str, str], str] = {}
    for c in ctx.snap.concepts:
        key = (c.type, _normalised_name(c.name))
        if c.type != "junction" and key[1]:
            groups[key].append(c.id)
            first_name.setdefault(key, c.name or "")
    for key, ids in sorted(groups.items()):
        typ, name = key[0], first_name[key]
        if len(ids) > 1:
            for i in sorted(ids):
                others = [o for o in sorted(ids) if o != i]
                ctx.emit(
                    "AM-DUP-001",
                    [("element", i)] + [("element", o) for o in others],
                    f"element '{name}' ({typ}) may duplicate {len(others)} other element(s)",
                    evidence={"duplicates": others},
                )


def _check_views(ctx: _Ctx) -> None:
    views = ctx.snap.views
    if views is None:
        return
    reg = ctx.reg
    used_concepts: set[str] = set()
    used_relationships: set[str] = set()
    for v in views:
        objects = {o.id: o for o in v.objects}
        connections = {c.id: c for c in v.connections}
        vt: list[tuple[TargetKind, str]] = [("view", v.id)]
        vp = None
        if v.viewpoint is not None:
            vp = reg.viewpoints.get(v.viewpoint)
            if vp is None:
                ctx.emit("AM-VIEW-001", vt, f"unknown viewpoint '{v.viewpoint}'", path="/viewpoint")
        if not v.objects:
            ctx.emit("AM-EMV-001", vt, f"view '{v.name or v.id}' is empty")

        for o in v.objects:
            ot: list[tuple[TargetKind, str]] = [("view_object", o.id)]
            if o.kind != "element":
                # Notes and groups are visual only: no concept, no viewpoint filter.
                if o.parent_object_id is not None and o.parent_object_id not in objects:
                    ctx.emit(
                        "AM-VIEW-003",
                        ot,
                        f"visual parent {o.parent_object_id} is not an object of view {v.id}",
                        path="/parent_object_id",
                    )
                continue
            concept = ctx.concepts.get(o.concept_id or "")
            if concept is None:
                ctx.emit(
                    "AM-REF-001",
                    ot,
                    f"view object {o.id} shows unresolved concept {o.concept_id}",
                    path="/concept_id",
                )
            else:
                used_concepts.add(concept.id)
                if (
                    vp is not None
                    and concept.type in reg.concepts
                    and not vp.allows_concept(concept.type)
                ):
                    ctx.emit(
                        "AM-VPT-001",
                        ot + [("element", concept.id), ("view", v.id)],
                        f"{concept.type} '{concept.name}' is not allowed in viewpoint '{vp.name}'",
                        evidence={"viewpoint": vp.id, "concept_type": concept.type},
                    )
            if o.parent_object_id is not None and o.parent_object_id not in objects:
                ctx.emit(
                    "AM-VIEW-003",
                    ot,
                    f"visual parent {o.parent_object_id} is not an object of view {v.id}",
                    path="/parent_object_id",
                )

        # Visual nesting cycles.
        for o in v.objects:
            seen = {o.id}
            p = o.parent_object_id
            while p is not None and p in objects:
                if p in seen:
                    ctx.emit(
                        "AM-VIEW-003",
                        [("view_object", o.id)],
                        f"visual nesting of {o.id} forms a cycle",
                    )
                    break
                seen.add(p)
                p = objects[p].parent_object_id

        def shown(ref: str) -> str | None:
            """The concept/relationship an end shows; None for notes, groups, lines."""
            if ref in objects:
                return objects[ref].concept_id
            if ref in connections:
                return connections[ref].relationship_id
            return None

        for cn in v.connections:
            ct: list[tuple[TargetKind, str]] = [("view_connection", cn.id)]
            if cn.kind == "line":
                # A plain line may join any two objects/connections of the view.
                missing = [
                    e
                    for e in (cn.source_object_id, cn.target_object_id)
                    if e not in objects and e not in connections
                ]
                if missing:
                    ctx.emit(
                        "AM-REF-001",
                        ct,
                        f"line {cn.id} has an end that is not an object or connection of view {v.id}",
                        evidence={"missing": missing},
                    )
                continue
            rel = ctx.relationships.get(cn.relationship_id or "")
            if rel is None:
                ctx.emit(
                    "AM-REF-001",
                    ct,
                    f"connection {cn.id} shows unresolved relationship {cn.relationship_id}",
                    path="/relationship_id",
                )
                continue
            used_relationships.add(rel.id)
            ends = (cn.source_object_id, cn.target_object_id)
            if all(e in objects or e in connections for e in ends) and any(
                shown(e) is None for e in ends
            ):
                ctx.emit(
                    "AM-VIEW-002",
                    ct + [("relationship", rel.id)],
                    f"connection {cn.id} attaches relationship {rel.id} to a note, group or line, "
                    "which shows no concept",
                )
                continue
            s, t = shown(cn.source_object_id), shown(cn.target_object_id)
            if s is None or t is None:
                ctx.emit(
                    "AM-REF-001",
                    ct,
                    f"connection {cn.id} has an end that is not an occurrence in view {v.id}",
                )
                continue
            if (s, t) != (rel.source, rel.target):
                ctx.emit(
                    "AM-VIEW-002",
                    ct + [("relationship", rel.id)],
                    f"connection {cn.id} joins occurrences of {s} and {t}, but relationship {rel.id} runs from {rel.source} to {rel.target}",
                )

        _check_nesting(ctx, v.objects, objects, v.connections)

    for c in ctx.snap.concepts:
        if c.id not in used_concepts:
            ctx.emit(
                "AM-UNU-001",
                [("element", c.id)],
                f"element '{c.name or c.id}' is not used in any view",
            )
    for r in ctx.snap.relationships:
        if r.id not in used_relationships:
            ctx.emit(
                "AM-UNU-002",
                [("relationship", r.id)],
                f"relationship {r.id} ({r.type}) is not used in any view",
            )


def _check_nesting(ctx: _Ctx, object_list, objects, connections) -> None:
    """Port of Archi NestedElementsChecker.isNestedWithoutValidRelation."""
    by_object_conn: dict[str, list] = defaultdict(list)
    for cn in connections:
        by_object_conn[cn.source_object_id].append(cn)
        by_object_conn[cn.target_object_id].append(cn)
    rels_of: dict[str, list[ArchimateRelationshipIn]] = defaultdict(list)
    for r in ctx.snap.relationships:
        rels_of[r.source].append(r)
        rels_of[r.target].append(r)

    for child in object_list:
        if child.parent_object_id is None or child.parent_object_id not in objects:
            continue
        parent = objects[child.parent_object_id]
        if parent.kind != "element" or child.kind != "element":
            continue  # nesting in/of a group or note needs no relationship
        pc, cc = ctx.concepts.get(parent.concept_id or ""), ctx.concepts.get(child.concept_id or "")
        if pc is None or cc is None or cc.type == "junction":
            continue
        bad = False
        for cn in by_object_conn[parent.id]:
            if cn.kind != "relationship" or child.id not in (
                cn.source_object_id,
                cn.target_object_id,
            ):
                continue
            rel = ctx.relationships.get(cn.relationship_id or "")
            if rel is None:
                continue
            if rel.type not in _NESTING_RELATIONSHIPS:
                bad = True
            elif rel.type == "specialization":
                bad = bad or rel.target == cc.id
            elif rel.source == cc.id:
                bad = True
            if bad:
                break
        if not bad and any(
            (r.target == cc.id or r.source == cc.id) and r.type in _NESTING_RELATIONSHIPS
            for r in rels_of[pc.id]
        ):
            continue
        ctx.emit(
            "AM-NST-001",
            [("view_object", child.id), ("element", cc.id), ("element", pc.id)],
            f"'{cc.name or cc.id}' is nested in '{pc.name or pc.id}' without a valid relationship",
        )


def validate(
    snapshot: ArchimateSnapshot,
    *,
    rules: Iterable[str] | None = None,
    disable: Iterable[str] | None = None,
) -> ValidationReport:
    """Validate a snapshot. Raises :class:`UnknownRuleError` for unknown rule ids."""
    reg = get_registry()
    enabled = resolve_enabled(
        rules=rules, disable=disable, views_supplied=snapshot.views is not None
    )
    ctx = _Ctx(snapshot, reg, set(enabled))
    _check_identity(ctx)
    _check_concepts(ctx)
    _check_relationships(ctx)
    _check_junctions(ctx)
    _check_duplicates(ctx)
    _check_views(ctx)
    views = snapshot.views or []
    examined = {
        "element": len(snapshot.concepts),
        "relationship": len(snapshot.relationships),
        "view": len(views),
        "view_object": sum(len(v.objects) for v in views),
        "view_connection": sum(len(v.connections) for v in views),
    }
    return build_report(
        ruleset_id=reg.ruleset_id,
        enabled=enabled,
        required=set(REQUIRED_RULES)
        if snapshot.views is not None
        else {r for r in REQUIRED_RULES if not RULES_BY_ID[r].needs_views},
        examined=examined,
        findings=ctx.findings,
    )
