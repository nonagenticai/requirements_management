"""Pure UML 2.5.1 validator (profile ``uml-2.5.1-p1``): ``validate(snapshot) -> ValidationReport``.

One function backs every entry point — write-time integrity, persisted
validation runs and the stateless ``POST /uml/tools/validate`` (target spec
§7.3, §8.2). No database, no I/O, deterministic output. The public API mirrors
:mod:`tools.architecture.archimate_rules` exactly.

Rule families (target spec §8.2 table, re-architecture spec §4.4):

- ``UML-REF`` — references resolve within the snapshot (to a concept of the
  expected kind), and identifiers are unique.
- ``UML-OWN`` — every concept has exactly one owner of a permitted kind, and the
  ownership chain has no cycle.
- ``UML-GEN`` — generalization is acyclic and only between kinds where the
  specific may specialize the general (UML ``Classifier::maySpecializeType``).
- ``UML-MUL`` — multiplicity bounds.
- ``UML-RLZ`` / ``UML-CON`` — interface realization and connector typing.
- ``UML-SEQ`` — interaction consistency against the stored occurrence order.
  This proves the drawing is internally consistent, not that code behaves so.
- ``UML-DIA`` — diagram occurrences (only when ``diagrams`` is supplied).
- ``UML-UNSUP`` — any construct outside the profile, so the run is never clean.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from schemas.modeling import (
    Finding,
    FindingTarget,
    RuleInfo,
    RulesetInfo,
    Severity,
    TargetKind,
    ValidationReport,
    build_report,
)
from schemas.uml import (
    UmlElementIn,
    UmlInteractionIn,
    UmlMessage,
    UmlOccurrence,
    UmlOperation,
    UmlRelationshipIn,
    UmlSnapshot,
)
from tools.uml.profile import Profile, get_profile


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    severity: Severity
    required: bool
    needs_diagrams: bool
    source: str
    description: str


_SPEC = "target spec §8.2"
RULES: tuple[Rule, ...] = (
    Rule(
        "UML-REF-001",
        "Unresolved reference",
        "error",
        True,
        False,
        _SPEC,
        "Every owner, type, interface, relationship end, lifeline type, occurrence, "
        "operation and fragment reference resolves within the snapshot to a concept of "
        "the expected kind.",
    ),
    Rule(
        "UML-REF-002",
        "Duplicate identifier",
        "error",
        True,
        False,
        "repository integrity",
        "Identifiers are unique across elements, features, relationships, interactions "
        "and their parts, diagrams, objects and connections.",
    ),
    Rule(
        "UML-OWN-001",
        "Invalid ownership",
        "error",
        True,
        False,
        _SPEC,
        "Each concept has a single owner of a kind the profile permits (a port is owned "
        "by its class or component), and the ownership chain has no cycle.",
    ),
    Rule(
        "UML-GEN-001",
        "Generalization cycle",
        "error",
        True,
        False,
        _SPEC,
        "A classifier cannot be its own (transitive) generalization.",
    ),
    Rule(
        "UML-GEN-002",
        "Incompatible generalization",
        "error",
        True,
        False,
        "UML 2.5.1 Classifier::maySpecializeType",
        "The specific (source) may specialize the general (target) only between "
        "compatible classifier kinds.",
    ),
    Rule(
        "UML-MUL-001",
        "Invalid multiplicity",
        "error",
        True,
        False,
        _SPEC,
        "A multiplicity is 'n', '*', 'n..m' or 'n..*' with 0 <= lower <= upper.",
    ),
    Rule(
        "UML-RLZ-001",
        "Interface realization without an interface",
        "error",
        True,
        False,
        _SPEC,
        "An interface realization targets an interface.",
    ),
    Rule(
        "UML-CON-001",
        "Invalid connector",
        "error",
        True,
        False,
        _SPEC,
        "Connector ends are ports. An assembly joins a required interface to a "
        "conforming provided one; a delegation joins provided to provided or required "
        "to required.",
    ),
    Rule(
        "UML-SEQ-001",
        "Occurrence order violated",
        "error",
        True,
        False,
        _SPEC,
        "A message's send occurrence precedes its receive occurrence; an execution's "
        "start and finish occurrences are on its lifeline and start does not follow finish.",
    ),
    Rule(
        "UML-SEQ-002",
        "Unmatched reply",
        "error",
        True,
        False,
        _SPEC,
        "Every reply answers an earlier, not yet answered synchronous call between the "
        "same lifelines (in the opposite direction), for the same operation when named.",
    ),
    Rule(
        "UML-SEQ-003",
        "Operation not owned by the receiver",
        "warning",
        False,
        False,
        _SPEC,
        "A non-reply message's operation is owned by the receiving lifeline's type or one "
        "of its transitive supertypes (generalization), or by an interface that type or a "
        "supertype realizes (InterfaceRealization) or provides.",
    ),
    Rule(
        "UML-SEQ-004",
        "Inconsistent combined fragment",
        "error",
        True,
        False,
        _SPEC,
        "A fragment covers at least one lifeline; its operands contain only occurrences "
        "on covered lifelines, each in at most one operand; opt/loop/break have exactly "
        "one operand; alt/opt/loop operands carry guards.",
    ),
    Rule(
        "UML-DIA-001",
        "Diagram occurrence outside the model",
        "error",
        True,
        True,
        _SPEC,
        "Each diagram object shows exactly one element or lifeline, and each connection "
        "exactly one relationship or message, of the same model.",
    ),
    Rule(
        "UML-DIA-002",
        "Connection does not show its relationship",
        "error",
        True,
        True,
        _SPEC,
        "A connection's endpoints are objects of the same diagram that show the "
        "relationship's (or message's lifelines') ends.",
    ),
    Rule(
        "UML-UNSUP-001",
        "Outside the profile",
        "error",
        True,
        False,
        _SPEC,
        "Any kind, operator, attribute or construct outside uml-2.5.1-p1 is an error, so "
        "the run is never clean.",
    ),
)
RULES_BY_ID: dict[str, Rule] = {r.rule_id: r for r in RULES}
REQUIRED_RULES: frozenset[str] = frozenset(r.rule_id for r in RULES if r.required)

_FEATURE_GROUPS = ("properties", "operations", "literals", "provided", "required")


def ruleset_info(*, with_rules: bool = True) -> RulesetInfo:
    prof = get_profile()
    return RulesetInfo(
        ruleset_id=prof.ruleset_id,
        language="uml",
        language_version=prof.language_version,
        reference=prof.reference,
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
    *, rules: Iterable[str] | None, disable: Iterable[str] | None, diagrams_supplied: bool
) -> list[str]:
    selected = list(rules) if rules is not None else [r.rule_id for r in RULES]
    off = set(disable or ())
    unknown = sorted((set(selected) | off) - set(RULES_BY_ID))
    if unknown:
        raise UnknownRuleError(f"unknown rule ids: {', '.join(unknown)}")
    return [
        rid
        for rid in selected
        if rid not in off and (diagrams_supplied or not RULES_BY_ID[rid].needs_diagrams)
    ]


_ASCII_INT = re.compile(r"[0-9]+")


def parse_multiplicity(text: str) -> tuple[int, int | None] | None:
    """``"0..1"`` -> (0, 1); ``"*"`` -> (0, None); ``"1..*"`` -> (1, None). Invalid -> None."""
    t = text.strip()
    if t == "*":
        return (0, None)
    lo_s, sep, hi_s = t.partition("..")
    if not sep:
        hi_s = lo_s
    # ASCII digits only: ``str.isdigit`` accepts "²", which ``int`` then rejects
    # with a ValueError — a 500 from user input.
    if not _ASCII_INT.fullmatch(lo_s):
        return None
    lo = int(lo_s)
    if hi_s == "*":
        return (lo, None)
    if not _ASCII_INT.fullmatch(hi_s):
        return None
    hi = int(hi_s)
    return (lo, hi) if lo <= hi else None


class _Ctx:
    def __init__(self, snap: UmlSnapshot, prof: Profile, enabled: set[str]) -> None:
        self.snap = snap
        self.prof = prof
        self.enabled = enabled
        self.findings: list[Finding] = []
        self.elements: dict[str, UmlElementIn] = {}
        self.relationships: dict[str, UmlRelationshipIn] = {}
        self.operations: dict[str, tuple[str, UmlOperation]] = {}
        self.lifelines: dict[str, str] = {}  # lifeline id -> interaction id
        self.occurrences: dict[str, UmlOccurrence] = {}
        self.messages: dict[str, UmlMessage] = {}
        self.message_interaction: dict[str, str] = {}
        for e in snap.elements:
            self.elements.setdefault(e.id, e)
            for op in e.attributes.operations:
                self.operations.setdefault(op.id, (e.id, op))
        for r in snap.relationships:
            self.relationships.setdefault(r.id, r)
        for i in snap.interactions:
            for ll in i.lifelines:
                self.lifelines.setdefault(ll.id, i.id)
            for oc in i.occurrences:
                self.occurrences.setdefault(oc.id, oc)
            for m in i.messages:
                self.messages.setdefault(m.id, m)
                self.message_interaction.setdefault(m.id, i.id)
        # Generalization graph (specific -> generals) over resolved ends.
        self.parents: dict[str, list[str]] = defaultdict(list)
        for r in snap.relationships:
            if (
                r.kind == "generalization"
                and r.source in self.elements
                and r.target in self.elements
            ):
                self.parents[r.source].append(r.target)
        # Interface realization graph (implementing classifier -> interfaces).
        self.realized: dict[str, list[str]] = defaultdict(list)
        for r in snap.relationships:
            if (
                r.kind == "interface_realization"
                and r.source in self.elements
                and r.target in self.elements
            ):
                self.realized[r.source].append(r.target)
        # Ports by owning classifier (a component provides its ports' interfaces).
        self.ports: dict[str, list[str]] = defaultdict(list)
        for e in snap.elements:
            if e.kind == "port" and e.owner_id is not None:
                self.ports[e.owner_id].append(e.id)

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
                ruleset_id=self.prof.ruleset_id,
                severity=RULES_BY_ID[rule_id].severity,
                targets=[FindingTarget(kind=k, id=i) for k, i in targets],
                path=path,
                message=message,
                evidence=evidence,
            )
        )

    def supertypes(self, element_id: str) -> list[str]:
        """Transitive generals of ``element_id`` via generalization (cycle-safe, sorted)."""
        seen: set[str] = set()
        stack = list(self.parents.get(element_id, ()))
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.parents.get(n, ()))
        seen.discard(element_id)
        return sorted(seen)

    def supported_interfaces(self, element_id: str) -> list[str]:
        """Interfaces ``element_id`` realizes or provides (cycle-safe, sorted).

        Covers ``element_id`` and its transitive generals: interface realization
        targets, ``attributes.provided``, and the provided interfaces of owned
        ports, plus the transitive generals of every such interface.
        """
        found: set[str] = set()
        for c in (element_id, *self.supertypes(element_id)):
            el = self.elements.get(c)
            direct = list(self.realized.get(c, ()))
            if el is not None:
                direct.extend(el.attributes.provided)
            for port in self.ports.get(c, ()):
                direct.extend(self.elements[port].attributes.provided)
            for i in direct:
                if i in self.elements and self.elements[i].kind == "interface":
                    found.add(i)
                    found.update(self.supertypes(i))
        found.discard(element_id)
        return sorted(found)

    def conforms(self, specific: str, general: str) -> bool:
        return specific == general or general in self.supertypes(specific)

    def unsup(
        self,
        targets: list[tuple[TargetKind, str]],
        what: str,
        value: object,
        allowed: Iterable[str] | None,
        *,
        path: str | None,
    ) -> None:
        ev: dict = {"value": value}
        msg = f"{what} '{value}' is outside profile {self.prof.profile_id}"
        if allowed is not None:
            ev["allowed"] = list(allowed)
        self.emit("UML-UNSUP-001", targets, msg, path=path, evidence=ev)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def _check_identity(ctx: _Ctx) -> None:
    # (target kind, target id, label, own id, feature owner or None, path)
    items: list[tuple[TargetKind, str, str, str, str | None, str | None]] = []
    for e in ctx.snap.elements:
        items.append(("uml_element", e.id, "element", e.id, None, None))
        for pi, p in enumerate(e.attributes.properties):
            items.append(
                ("uml_element", e.id, "property", p.id, e.id, f"/attributes/properties/{pi}/id")
            )
        for oi, op in enumerate(e.attributes.operations):
            items.append(
                ("uml_element", e.id, "operation", op.id, e.id, f"/attributes/operations/{oi}/id")
            )
            for qi, q in enumerate(op.parameters):
                items.append(
                    (
                        "uml_element",
                        e.id,
                        "parameter",
                        q.id,
                        op.id,
                        f"/attributes/operations/{oi}/parameters/{qi}/id",
                    )
                )
    for r in ctx.snap.relationships:
        items.append(("uml_relationship", r.id, "relationship", r.id, None, None))
    for i in ctx.snap.interactions:
        items.append(("interaction", i.id, "interaction", i.id, None, None))
        for label, group, parts in (
            ("lifeline", "lifelines", i.lifelines),
            ("occurrence", "occurrences", i.occurrences),
            ("message", "messages", i.messages),
            ("execution", "executions", i.executions),
            ("fragment", "fragments", i.fragments),
        ):
            for n, part in enumerate(parts):
                items.append(("interaction", i.id, label, part.id, None, f"/{group}/{n}/id"))
    for d in ctx.snap.diagrams or []:
        items.append(("diagram", d.id, "diagram", d.id, None, None))
        items += [("diagram_object", o.id, "diagram object", o.id, None, None) for o in d.objects]
        items += [
            ("diagram_connection", c.id, "diagram connection", c.id, None, None)
            for c in d.connections
        ]

    seen: dict[str, tuple[str, str | None]] = {}
    for kind, tid, label, ident, owner, path in items:
        prev = seen.get(ident)
        if prev is None:
            seen[ident] = (label, owner)
            continue
        prev_label, prev_owner = prev
        if owner is not None and prev_owner is not None and owner != prev_owner:
            ctx.emit(
                "UML-OWN-001",
                [(kind, tid)],
                f"{label} {ident} is owned by both {prev_owner} and {owner}",
                path=path,
                evidence={"id": ident, "owners": sorted([prev_owner, owner])},
            )
        else:
            ctx.emit(
                "UML-REF-002",
                [(kind, tid)],
                f"identifier {ident} is used by more than one {prev_label}/{label}",
                path=path,
                evidence={"id": ident},
            )


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------


def _check_mult(ctx: _Ctx, targets: list[tuple[TargetKind, str]], value: str | None, path: str):
    if value is None:
        return
    if parse_multiplicity(value) is None:
        ctx.emit(
            "UML-MUL-001",
            targets,
            f"multiplicity '{value}' is not 'n', '*', 'n..m' or 'n..*' with 0 <= lower <= upper",
            path=path,
            evidence={"multiplicity": value},
        )


def _check_ref(
    ctx: _Ctx,
    targets: list[tuple[TargetKind, str]],
    what: str,
    ref: str | None,
    path: str,
    *,
    expect: frozenset[str] | None = None,
) -> UmlElementIn | None:
    if ref is None:
        return None
    el = ctx.elements.get(ref)
    if el is None:
        ctx.emit("UML-REF-001", targets, f"{what} {ref} does not resolve", path=path)
        return None
    if expect is not None and el.kind in ctx.prof.element_kinds and el.kind not in expect:
        ctx.emit(
            "UML-REF-001",
            targets,
            f"{what} {ref} is a {el.kind}, expected one of {sorted(expect)}",
            path=path,
            evidence={"ref": ref, "kind": el.kind, "expected": sorted(expect)},
        )
        return None
    return el


_CLASSIFIERS = frozenset(
    {"class", "interface", "data_type", "enumeration", "primitive_type", "component", "actor"}
)
_INTERFACE = frozenset({"interface"})


def _check_elements(ctx: _Ctx) -> None:
    prof = ctx.prof
    for e in ctx.snap.elements:
        t: list[tuple[TargetKind, str]] = [("uml_element", e.id)]
        a = e.attributes
        known = e.kind in prof.element_kinds
        if not known:
            ctx.unsup(t, "element kind", e.kind, prof.element_kinds, path="/kind")
        else:
            permitted = prof.element_features[e.kind]
            for group in _FEATURE_GROUPS:
                if getattr(a, group) and group not in permitted:
                    ctx.unsup(t, f"{group} on {e.kind}", group, None, path=f"/attributes/{group}")
            if a.is_abstract and "is_abstract" not in permitted:
                ctx.unsup(t, f"is_abstract on {e.kind}", True, None, path="/attributes/is_abstract")

        for pi, p in enumerate(a.properties):
            base = f"/attributes/properties/{pi}"
            if p.visibility is not None and p.visibility not in prof.visibilities:
                ctx.unsup(
                    t, "visibility", p.visibility, prof.visibilities, path=f"{base}/visibility"
                )
            _check_ref(
                ctx,
                t,
                f"type of property {p.id}",
                p.type_ref,
                f"{base}/type_ref",
                expect=_CLASSIFIERS,
            )
            _check_mult(ctx, t, p.multiplicity, f"{base}/multiplicity")
        for oi, op in enumerate(a.operations):
            base = f"/attributes/operations/{oi}"
            if op.visibility is not None and op.visibility not in prof.visibilities:
                ctx.unsup(
                    t, "visibility", op.visibility, prof.visibilities, path=f"{base}/visibility"
                )
            _check_ref(
                ctx,
                t,
                f"return type of operation {op.id}",
                op.return_type_ref,
                f"{base}/return_type_ref",
                expect=_CLASSIFIERS,
            )
            for qi, q in enumerate(op.parameters):
                qb = f"{base}/parameters/{qi}"
                if q.direction is not None and q.direction not in prof.parameter_directions:
                    ctx.unsup(
                        t,
                        "parameter direction",
                        q.direction,
                        prof.parameter_directions,
                        path=f"{qb}/direction",
                    )
                _check_ref(
                    ctx,
                    t,
                    f"type of parameter {q.id}",
                    q.type_ref,
                    f"{qb}/type_ref",
                    expect=_CLASSIFIERS,
                )
                _check_mult(ctx, t, q.multiplicity, f"{qb}/multiplicity")
        for group in ("provided", "required"):
            for n, ref in enumerate(getattr(a, group)):
                _check_ref(
                    ctx, t, f"{group} interface", ref, f"/attributes/{group}/{n}", expect=_INTERFACE
                )

        # Ownership.
        if e.owner_id is None:
            if known and e.kind in prof.owner_required:
                ctx.emit("UML-OWN-001", t, f"{e.kind} {e.id} has no owner", path="/owner_id")
        elif e.owner_id not in ctx.elements:
            ctx.emit(
                "UML-REF-001",
                t,
                f"owner {e.owner_id} of element {e.id} does not resolve",
                path="/owner_id",
            )
        else:
            owner = ctx.elements[e.owner_id]
            allowed = prof.owner_kinds.get(e.kind)
            if (
                allowed is not None
                and owner.kind in prof.element_kinds
                and owner.kind not in allowed
                and e.owner_id != e.id
            ):
                ctx.emit(
                    "UML-OWN-001",
                    t + [("uml_element", owner.id)],
                    f"a {e.kind} cannot be owned by a {owner.kind}",
                    path="/owner_id",
                    evidence={"owner_kind": owner.kind, "allowed": sorted(allowed)},
                )

    # Ownership cycles: walk each owner chain once, colouring as we go.
    state: dict[str, int] = {}
    for start in ctx.elements:
        chain: list[str] = []
        node: str | None = start
        while node is not None and node in ctx.elements and state.get(node) is None:
            state[node] = 1
            chain.append(node)
            node = ctx.elements[node].owner_id
        if node is not None and state.get(node) == 1:
            cycle = chain[chain.index(node) :]
            ctx.emit(
                "UML-OWN-001",
                [("uml_element", i) for i in sorted(cycle)],
                f"ownership chain forms a cycle through {len(cycle)} element(s)",
                path="/owner_id",
                evidence={"cycle": sorted(cycle)},
            )
        for n in chain:
            state[n] = 2


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


def _check_relationships(ctx: _Ctx) -> None:
    prof = ctx.prof
    for r in ctx.snap.relationships:
        t: list[tuple[TargetKind, str]] = [("uml_relationship", r.id)]
        if r.kind not in prof.relationship_kinds:
            ctx.unsup(t, "relationship kind", r.kind, prof.relationship_kinds, path="/kind")
            continue
        if r.ends is not None and r.kind != "association":
            ctx.unsup(t, f"ends on {r.kind}", "ends", None, path="/ends")
        if r.connector_kind is not None and r.kind != "connector":
            ctx.unsup(
                t, f"connector_kind on {r.kind}", r.connector_kind, None, path="/connector_kind"
            )

        if r.kind == "association":
            _check_association(ctx, r, t)
            continue

        ends: list[UmlElementIn] = []
        for end, ref in (("source", r.source), ("target", r.target)):
            if ref is None:
                ctx.emit("UML-REF-001", t, f"{r.kind} {r.id} has no {end}", path=f"/{end}")
                continue
            el = _check_ref(ctx, t, f"{end} of {r.kind} {r.id}", ref, f"/{end}")
            if el is not None:
                ends.append(el)
        if r.kind == "connector" and (
            r.connector_kind is None or r.connector_kind not in prof.connector_kinds
        ):
            ctx.unsup(
                t, "connector_kind", r.connector_kind, prof.connector_kinds, path="/connector_kind"
            )
        if len(ends) != 2:
            continue
        src, tgt = ends
        if any(x.kind not in prof.element_kinds for x in ends):
            continue
        ev_t: list[tuple[TargetKind, str]] = [*t, ("uml_element", src.id), ("uml_element", tgt.id)]
        if r.kind == "generalization" and not prof.may_specialize(src.kind, tgt.kind):
            ctx.emit(
                "UML-GEN-002",
                ev_t,
                f"a {src.kind} cannot specialize a {tgt.kind}",
                evidence={
                    "specific_kind": src.kind,
                    "general_kind": tgt.kind,
                    "allowed": sorted(prof.generalization_compatible.get(src.kind, ())),
                },
            )
        elif r.kind == "interface_realization" and tgt.kind != "interface":
            ctx.emit(
                "UML-RLZ-001",
                ev_t,
                f"interface realization {r.id} targets a {tgt.kind}, not an interface",
                path="/target",
                evidence={"target_kind": tgt.kind},
            )
        elif r.kind == "connector" and r.connector_kind in prof.connector_kinds:
            _check_connector(ctx, r, src, tgt, ev_t)

    for cycle in _cycles(ctx.parents):
        ctx.emit(
            "UML-GEN-001",
            [("uml_element", i) for i in sorted(cycle)],
            f"generalization forms a cycle through {len(cycle)} element(s)",
            evidence={"cycle": sorted(cycle)},
        )


def _check_association(ctx: _Ctx, r: UmlRelationshipIn, t: list[tuple[TargetKind, str]]) -> None:
    ends = r.ends or []
    if len(ends) != 2:
        ctx.unsup(t, "association end count", len(ends), ["2"], path="/ends")
    for n, end in enumerate(ends):
        base = f"/ends/{n}"
        _check_ref(ctx, t, f"end {n} of association {r.id}", end.element_ref, f"{base}/element_ref")
        if end.aggregation not in ctx.prof.aggregation_kinds:
            ctx.unsup(
                t,
                "aggregation",
                end.aggregation,
                ctx.prof.aggregation_kinds,
                path=f"{base}/aggregation",
            )
        _check_mult(ctx, t, end.multiplicity, f"{base}/multiplicity")
    if len(ends) == 2:
        for end_name, ref, n in (("source", r.source, 0), ("target", r.target, 1)):
            if ref is not None and ref != ends[n].element_ref:
                ctx.emit(
                    "UML-REF-001",
                    t,
                    f"association {end_name} {ref} is not end {n} ({ends[n].element_ref})",
                    path=f"/{end_name}",
                )


def _check_connector(
    ctx: _Ctx,
    r: UmlRelationshipIn,
    src: UmlElementIn,
    tgt: UmlElementIn,
    ev_t: list[tuple[TargetKind, str]],
) -> None:
    non_ports = [x.id for x in (src, tgt) if x.kind != "port"]
    if non_ports:
        ctx.emit(
            "UML-CON-001",
            ev_t,
            f"connector {r.id} ends must be ports; not a port: {', '.join(non_ports)}",
            evidence={"not_ports": non_ports},
        )
        return
    sa, ta = src.attributes, tgt.attributes

    def match(provided: list[str], required: list[str]) -> bool:
        return any(ctx.conforms(p, q) for p in provided for q in required)

    if r.connector_kind == "assembly":
        ok = match(ta.provided, sa.required) or match(sa.provided, ta.required)
    else:  # delegation: same direction, either side may be the more specific
        ok = (
            match(sa.provided, ta.provided)
            or match(ta.provided, sa.provided)
            or match(sa.required, ta.required)
            or match(ta.required, sa.required)
        )
    if not ok:
        ctx.emit(
            "UML-CON-001",
            ev_t,
            f"{r.connector_kind} connector {r.id} joins ports with no compatible interfaces",
            evidence={
                "connector_kind": r.connector_kind,
                "source": {"provided": sorted(sa.provided), "required": sorted(sa.required)},
                "target": {"provided": sorted(ta.provided), "required": sorted(ta.required)},
            },
        )


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


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------


def _check_interactions(ctx: _Ctx) -> None:
    for inter in ctx.snap.interactions:
        _check_interaction(ctx, inter)


def _check_interaction(ctx: _Ctx, inter: UmlInteractionIn) -> None:
    prof = ctx.prof
    t: list[tuple[TargetKind, str]] = [("interaction", inter.id)]
    if inter.owner_id is not None:
        owner = ctx.elements.get(inter.owner_id)
        if owner is None:
            ctx.emit(
                "UML-REF-001",
                t,
                f"owner {inter.owner_id} of interaction {inter.id} does not resolve",
                path="/owner_id",
            )
        elif owner.kind in prof.element_kinds and owner.kind not in prof.interaction_owner_kinds:
            ctx.emit(
                "UML-OWN-001",
                t + [("uml_element", owner.id)],
                f"an interaction cannot be owned by a {owner.kind}",
                path="/owner_id",
                evidence={
                    "owner_kind": owner.kind,
                    "allowed": sorted(prof.interaction_owner_kinds),
                },
            )

    lifelines = {ll.id: ll for ll in inter.lifelines}
    for n, ll in enumerate(inter.lifelines):
        if ll.represents_ref is None:
            continue
        el = _check_ref(
            ctx, t, f"type of lifeline {ll.id}", ll.represents_ref, f"/lifelines/{n}/represents_ref"
        )
        if (
            el is not None
            and el.kind in prof.element_kinds
            and el.kind not in prof.lifeline_type_kinds
        ):
            ctx.unsup(
                t,
                "lifeline type kind",
                el.kind,
                sorted(prof.lifeline_type_kinds),
                path=f"/lifelines/{n}/represents_ref",
            )

    order: dict[str, int] = {}
    occ_lifeline: dict[str, str] = {}
    for n, oc in enumerate(inter.occurrences):
        order.setdefault(oc.id, n)
        if oc.lifeline not in lifelines:
            ctx.emit(
                "UML-REF-001",
                t,
                f"lifeline {oc.lifeline} of occurrence {oc.id} is not a lifeline of interaction {inter.id}",
                path=f"/occurrences/{n}/lifeline",
            )
        else:
            occ_lifeline.setdefault(oc.id, oc.lifeline)

    def occ_ref(ref: str, what: str, path: str) -> bool:
        if ref in order:
            return True
        ctx.emit(
            "UML-REF-001",
            t,
            f"{what} {ref} is not an occurrence of interaction {inter.id}",
            path=path,
        )
        return False

    resolved: list[tuple[int, UmlMessage]] = []
    for n, m in enumerate(inter.messages):
        base = f"/messages/{n}"
        if m.kind not in prof.message_kinds:
            ctx.unsup(t, "message kind", m.kind, prof.message_kinds, path=f"{base}/kind")
        s_ok = occ_ref(
            m.send_occurrence, f"send occurrence of message {m.id}", f"{base}/send_occurrence"
        )
        r_ok = occ_ref(
            m.receive_occurrence,
            f"receive occurrence of message {m.id}",
            f"{base}/receive_occurrence",
        )
        op_owner: str | None = None
        if m.operation_ref is not None:
            hit = ctx.operations.get(m.operation_ref)
            if hit is None:
                ctx.emit(
                    "UML-REF-001",
                    t,
                    f"operation {m.operation_ref} of message {m.id} does not resolve",
                    path=f"{base}/operation_ref",
                )
            else:
                op_owner = hit[0]
        if not (s_ok and r_ok):
            continue
        if order[m.send_occurrence] >= order[m.receive_occurrence]:
            ctx.emit(
                "UML-SEQ-001",
                t,
                f"message {m.id}: send occurrence {m.send_occurrence} does not precede "
                f"receive occurrence {m.receive_occurrence}",
                path=base,
                evidence={
                    "message_id": m.id,
                    "send_index": order[m.send_occurrence],
                    "receive_index": order[m.receive_occurrence],
                },
            )
        if m.send_occurrence in occ_lifeline and m.receive_occurrence in occ_lifeline:
            resolved.append((n, m))
        # UML-SEQ-003: the receiver's type, a supertype, or an interface either
        # realizes or provides owns the operation.
        if op_owner is not None and m.kind != "reply" and m.receive_occurrence in occ_lifeline:
            ll = lifelines[occ_lifeline[m.receive_occurrence]]
            rep = ll.represents_ref
            if rep is not None and rep in ctx.elements:
                supers = ctx.supertypes(rep)
                ifaces = ctx.supported_interfaces(rep)
                if op_owner != rep and op_owner not in supers and op_owner not in ifaces:
                    ctx.emit(
                        "UML-SEQ-003",
                        t + [("uml_element", rep), ("uml_element", op_owner)],
                        f"message {m.id}: operation {m.operation_ref} belongs to {op_owner}, "
                        f"not to {rep} (lifeline {ll.id}), its supertypes or the "
                        "interfaces they realize or provide",
                        path=f"{base}/operation_ref",
                        evidence={
                            "message_id": m.id,
                            "operation_owner": op_owner,
                            "receiver_type": rep,
                            "receiver_supertypes": supers,
                            "receiver_interfaces": ifaces,
                        },
                    )

    _check_replies(ctx, t, resolved, order, occ_lifeline)

    for n, ex in enumerate(inter.executions):
        base = f"/executions/{n}"
        if ex.lifeline not in lifelines:
            ctx.emit(
                "UML-REF-001",
                t,
                f"lifeline {ex.lifeline} of execution {ex.id} is not a lifeline of interaction {inter.id}",
                path=f"{base}/lifeline",
            )
        st = occ_ref(ex.start, f"start of execution {ex.id}", f"{base}/start")
        fi = occ_ref(ex.finish, f"finish of execution {ex.id}", f"{base}/finish")
        if not (st and fi) or ex.lifeline not in lifelines:
            continue
        off = [
            o for o in (ex.start, ex.finish) if o in occ_lifeline and occ_lifeline[o] != ex.lifeline
        ]
        if off:
            ctx.emit(
                "UML-SEQ-001",
                t,
                f"execution {ex.id}: occurrence(s) {', '.join(off)} are not on lifeline {ex.lifeline}",
                path=base,
                evidence={"execution_id": ex.id, "off_lifeline": off},
            )
        if order[ex.start] > order[ex.finish]:
            ctx.emit(
                "UML-SEQ-001",
                t,
                f"execution {ex.id}: start {ex.start} follows finish {ex.finish}",
                path=base,
                evidence={
                    "execution_id": ex.id,
                    "start_index": order[ex.start],
                    "finish_index": order[ex.finish],
                },
            )

    for n in range(len(inter.fragments)):
        _check_fragment(ctx, t, inter, n, lifelines, order, occ_lifeline)


def _check_replies(
    ctx: _Ctx,
    t: list[tuple[TargetKind, str]],
    resolved: list[tuple[int, UmlMessage]],
    order: dict[str, int],
    occ_lifeline: dict[str, str],
) -> None:
    sync = ctx.prof.synchronous_message_kinds
    pending: dict[tuple[str, str], list[UmlMessage]] = defaultdict(list)
    for n, m in sorted(resolved, key=lambda nm: (order[nm[1].send_occurrence], nm[0])):
        sender, receiver = occ_lifeline[m.send_occurrence], occ_lifeline[m.receive_occurrence]
        if m.kind in sync:
            pending[(sender, receiver)].append(m)
            continue
        if m.kind != "reply":
            continue
        stack = pending[(receiver, sender)]
        match: int | None = None
        for k in range(len(stack) - 1, -1, -1):
            call = stack[k]
            if order[call.receive_occurrence] >= order[m.send_occurrence]:
                continue
            if m.operation_ref is None or call.operation_ref in (None, m.operation_ref):
                match = k
                break
        if match is None:
            ctx.emit(
                "UML-SEQ-002",
                t,
                f"reply {m.id} from {sender} to {receiver} answers no earlier unanswered "
                f"synchronous call from {receiver} to {sender}",
                path=f"/messages/{n}",
                evidence={"message_id": m.id, "operation_ref": m.operation_ref},
            )
        else:
            stack.pop(match)


def _check_fragment(
    ctx: _Ctx,
    t: list[tuple[TargetKind, str]],
    inter: UmlInteractionIn,
    n: int,
    lifelines: dict,
    order: dict[str, int],
    occ_lifeline: dict[str, str],
) -> None:
    prof = ctx.prof
    fr = inter.fragments[n]
    base = f"/fragments/{n}"
    if fr.operator not in prof.fragment_operators:
        ctx.unsup(
            t, "fragment operator", fr.operator, prof.fragment_operators, path=f"{base}/operator"
        )
        return

    def seq4(msg: str, path: str, **ev: object) -> None:
        ctx.emit(
            "UML-SEQ-004",
            t,
            f"fragment {fr.id} ({fr.operator}): {msg}",
            path=path,
            evidence={"fragment_id": fr.id, **ev},
        )

    if not fr.covered:
        seq4("covers no lifeline", f"{base}/covered")
    covered: set[str] = set()
    for k, ll in enumerate(fr.covered):
        if ll not in lifelines:
            ctx.emit(
                "UML-REF-001",
                t,
                f"covered lifeline {ll} of fragment {fr.id} is not a lifeline of interaction {inter.id}",
                path=f"{base}/covered/{k}",
            )
        else:
            covered.add(ll)
    if not fr.operands:
        seq4("has no operand", f"{base}/operands")
    elif fr.operator in prof.single_operand_operators and len(fr.operands) != 1:
        seq4(
            f"must have exactly one operand, has {len(fr.operands)}",
            f"{base}/operands",
            operand_count=len(fr.operands),
        )
    placed: dict[str, int] = {}
    for j, od in enumerate(fr.operands):
        ob = f"{base}/operands/{j}"
        if fr.operator in prof.guarded_operators and not (od.guard or "").strip():
            seq4(f"operand {j} has no guard", f"{ob}/guard")
        for k, oc in enumerate(od.occurrences):
            if oc not in order:
                ctx.emit(
                    "UML-REF-001",
                    t,
                    f"occurrence {oc} in fragment {fr.id} is not an occurrence of interaction {inter.id}",
                    path=f"{ob}/occurrences/{k}",
                )
                continue
            if oc in placed and placed[oc] != j:
                seq4(
                    f"occurrence {oc} is in operands {placed[oc]} and {j}",
                    f"{ob}/occurrences/{k}",
                    occurrence=oc,
                )
            placed.setdefault(oc, j)
            ll = occ_lifeline.get(oc)
            if ll is not None and fr.covered and ll not in covered:
                seq4(
                    f"occurrence {oc} is on lifeline {ll}, which the fragment does not cover",
                    f"{ob}/occurrences/{k}",
                    occurrence=oc,
                    lifeline=ll,
                )


# ---------------------------------------------------------------------------
# Diagrams
# ---------------------------------------------------------------------------


def _relationship_ends(r: UmlRelationshipIn) -> tuple[str | None, str | None]:
    if r.kind == "association":
        ends = r.ends or []
        return (ends[0].element_ref, ends[1].element_ref) if len(ends) == 2 else (None, None)
    return (r.source, r.target)


def _check_diagrams(ctx: _Ctx) -> None:
    diagrams = ctx.snap.diagrams
    if diagrams is None:
        return
    prof = ctx.prof
    for d in diagrams:
        dk = prof.diagram_kinds.get(d.kind)
        if dk is None:
            ctx.unsup(
                [("diagram", d.id)],
                "diagram kind",
                d.kind,
                sorted(prof.diagram_kinds),
                path="/kind",
            )
        shown: dict[str, tuple[str, str]] = {}  # object id -> ("element"|"lifeline", ref)
        for o in d.objects:
            ot: list[tuple[TargetKind, str]] = [("diagram_object", o.id), ("diagram", d.id)]
            refs = [
                (k, v)
                for k, v in (("element", o.element_ref), ("lifeline", o.lifeline_id))
                if v is not None
            ]
            if len(refs) != 1:
                ctx.emit(
                    "UML-DIA-001",
                    ot,
                    f"diagram object {o.id} must show exactly one element or lifeline",
                    evidence={"refs": len(refs)},
                )
                continue
            kind, ref = refs[0]
            exists = ref in ctx.elements if kind == "element" else ref in ctx.lifelines
            if not exists:
                ctx.emit(
                    "UML-DIA-001",
                    ot,
                    f"diagram object {o.id} shows {kind} {ref}, which is not in the model",
                    path=f"/{'element_ref' if kind == 'element' else 'lifeline_id'}",
                )
                continue
            if dk is not None and kind != dk.object_ref:
                ctx.unsup(ot, f"{kind} on a {d.kind} diagram", ref, [dk.object_ref], path=None)
            shown[o.id] = (kind, ref)

        for c in d.connections:
            ct: list[tuple[TargetKind, str]] = [("diagram_connection", c.id), ("diagram", d.id)]
            refs = [
                (k, v)
                for k, v in (("relationship", c.relationship_ref), ("message", c.message_id))
                if v is not None
            ]
            if len(refs) != 1:
                ctx.emit(
                    "UML-DIA-001",
                    ct,
                    f"connection {c.id} must show exactly one relationship or message",
                    evidence={"refs": len(refs)},
                )
                continue
            kind, ref = refs[0]
            expected: tuple[str | None, str | None]
            undirected = False
            if kind == "relationship":
                rel = ctx.relationships.get(ref)
                if rel is None:
                    ctx.emit(
                        "UML-DIA-001",
                        ct,
                        f"connection {c.id} shows relationship {ref}, which is not in the model",
                        path="/relationship_ref",
                    )
                    continue
                expected = _relationship_ends(rel)
                undirected = rel.kind == "association"
            else:
                msg = ctx.messages.get(ref)
                if msg is None:
                    ctx.emit(
                        "UML-DIA-001",
                        ct,
                        f"connection {c.id} shows message {ref}, which is not in the model",
                        path="/message_id",
                    )
                    continue
                so, ro = (
                    ctx.occurrences.get(msg.send_occurrence),
                    ctx.occurrences.get(msg.receive_occurrence),
                )
                expected = (so.lifeline if so else None, ro.lifeline if ro else None)
            if dk is not None and kind != dk.connection_ref:
                ctx.unsup(ct, f"{kind} on a {d.kind} diagram", ref, [dk.connection_ref], path=None)

            missing = [x for x in (c.source_object_id, c.target_object_id) if x not in shown]
            if missing:
                ctx.emit(
                    "UML-DIA-002",
                    ct,
                    f"connection {c.id} end(s) {', '.join(missing)} are not objects of diagram {d.id}",
                    evidence={"missing": missing},
                )
                continue
            got = (shown[c.source_object_id][1], shown[c.target_object_id][1])
            if None in expected:
                continue  # the relationship/message itself is broken; reported elsewhere
            if got != expected and not (undirected and got == (expected[1], expected[0])):
                owner: tuple[TargetKind, str] = (
                    ("uml_relationship", ref)
                    if kind == "relationship"
                    else ("interaction", ctx.message_interaction[ref])
                )
                ctx.emit(
                    "UML-DIA-002",
                    ct + [owner],
                    f"connection {c.id} joins {got[0]} and {got[1]}, but {kind} {ref} runs from "
                    f"{expected[0]} to {expected[1]}",
                    evidence={"shown": list(got), "expected": list(expected)},
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate(
    snapshot: UmlSnapshot,
    *,
    rules: Iterable[str] | None = None,
    disable: Iterable[str] | None = None,
) -> ValidationReport:
    """Validate a snapshot. Raises :class:`UnknownRuleError` for unknown rule ids."""
    prof = get_profile()
    diagrams_supplied = snapshot.diagrams is not None
    enabled = resolve_enabled(rules=rules, disable=disable, diagrams_supplied=diagrams_supplied)
    ctx = _Ctx(snapshot, prof, set(enabled))
    _check_identity(ctx)
    _check_elements(ctx)
    _check_relationships(ctx)
    _check_interactions(ctx)
    _check_diagrams(ctx)
    diagrams = snapshot.diagrams or []
    inters = snapshot.interactions
    examined = {
        "uml_element": len(snapshot.elements),
        "uml_relationship": len(snapshot.relationships),
        "interaction": len(inters),
        "lifeline": sum(len(i.lifelines) for i in inters),
        "message": sum(len(i.messages) for i in inters),
        "fragment": sum(len(i.fragments) for i in inters),
        "diagram": len(diagrams),
        "diagram_object": sum(len(d.objects) for d in diagrams),
        "diagram_connection": sum(len(d.connections) for d in diagrams),
    }
    return build_report(
        ruleset_id=prof.ruleset_id,
        enabled=enabled,
        required=set(REQUIRED_RULES)
        if diagrams_supplied
        else {r for r in REQUIRED_RULES if not RULES_BY_ID[r].needs_diagrams},
        examined=examined,
        findings=ctx.findings,
    )
