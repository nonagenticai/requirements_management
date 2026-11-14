"""ArchiMate 3.2 relationship derivation — which relationships a model implies.

Source: ArchiMate 3.2 Specification (The Open Group, C226), §5.7 "Derivation
Rules", as read: valid derivations for specialization, structural, dependency
and dynamic relationships, and potential derivations. The rule statements in
:data:`RULES` paraphrase §5.7 as ``p(a,b):S`` / ``q(b,c):T`` ⇒ ``r(a,c):U``.

⚠️ The rule ids (``AM-DER-*`` valid, ``AM-DER-POT-*`` potential) are this
service's own descriptive, stable ids. They make NO claim to the spec's rule
numbering: the 3.2 HTML sits behind an Open Group SSO login
(``pubs.opengroup.org`` → ``identity.opengroup.org``, checked 2026-10-30), so the
normative text could not be re-read when this module was written. The rules
marked "uncertain" in :data:`RULES` are the ones to check against §5.7 first:

* AM-DER-INFLUENCE-CHAIN (influence ∘ influence ⇒ influence, strength
  undetermined) and AM-DER-TRIGGERING-CHAIN (triggering ∘ triggering ⇒
  triggering) are stated as valid; if §5.7 classes either as potential, move it.
* AM-DER-POT-SPECIALIZATION-INHERIT / AM-DER-POT-SPECIALIZATION-REACH
  (inheritance across a specialization) are treated as potential.

Classes (§5.1–5.4, 5.7): structural = composition, aggregation, assignment,
realization, ordered strongest→weakest in that order (AM-DER-STRUCTURAL-CHAIN derives
the weakest);
dependency = serving, access, influence, association; dynamic = triggering,
flow; specialization is its own class.

What this module does NOT do (deliberately):

* No traversal through junctions (§5.5.1): a path a→J→c through a junction is a
  junction-mediated relationship, validated by ``archimate_rules``' junction
  checks, not a derivation. Junction concepts are skipped as intermediates and
  as endpoints.
* No derivation through relationships that point at relationships
  (association/aggregation to a relationship): those endpoints are not
  concepts and are skipped.
* No attribute derivation: the derived access mode / influence strength is not
  computed (§5.7 leaves influence strength undetermined).

Every derived relationship is checked against the Archi 5.10.0 relationship
matrix (``tools.architecture.registry``). One the matrix forbids is never
emitted as derived; it is returned in ``filtered`` with the reason, so the
caller can see the rule fired and why it was dropped.

Pure computation — no database, no async.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from schemas.tools import (
    DerivationExamined,
    DerivedRelationship,
    DeriveRelationshipsResult,
    FilteredDerivation,
)
from tools.architecture.registry import get_registry

# Strongest first (§5.7: "the weakest of S and T" is derived).
STRUCTURAL: tuple[str, ...] = ("composition", "aggregation", "assignment", "realization")
DEPENDENCY: frozenset[str] = frozenset({"serving", "access", "influence", "association"})
DYNAMIC: frozenset[str] = frozenset({"triggering", "flow"})
SPECIALIZATION = "specialization"

DEFAULT_MAX_PATH_LENGTH = 4
# Chain states explored before refusing. Above it the request is refused, never
# answered with the states explored so far (a truncated derivation set reads as
# "nothing else is implied").
DEFAULT_MAX_STATES = 100_000

Certainty = Literal["valid", "potential"]

# rule id -> (certainty, statement). The statement is what a reviewer checks
# against §5.7; ``uncertain`` marks a rule whose classification needs that check.
RULES: dict[str, tuple[Certainty, str]] = {
    "AM-DER-SPECIALIZATION-CHAIN": (
        "valid",
        "p(a,b):specialization, q(b,c):specialization => r(a,c):specialization",
    ),
    "AM-DER-STRUCTURAL-CHAIN": (
        "valid",
        "p(a,b):S, q(b,c):T, S,T structural => r(a,c): the weakest of S and T",
    ),
    "AM-DER-STRUCT-DEPENDENCY": ("valid", "p(a,b):S structural, q(b,c):T dependency => r(a,c):T"),
    "AM-DER-DEPENDENCY-REV-STRUCT": (
        "valid",
        "p(a,b):S dependency, q(c,b):T structural => r(a,c):S",
    ),
    "AM-DER-INFLUENCE-CHAIN": (
        "valid",
        "p(a,b):influence, q(b,c):influence => r(a,c):influence, strength undetermined (uncertain)",
    ),
    "AM-DER-STRUCT-DYNAMIC": ("valid", "p(a,b):S structural, q(b,c):T dynamic => r(a,c):T"),
    "AM-DER-DYNAMIC-REV-STRUCT": ("valid", "p(a,b):S dynamic, q(c,b):T structural => r(a,c):S"),
    "AM-DER-TRIGGERING-CHAIN": (
        "valid",
        "p(a,b):triggering, q(b,c):triggering => r(a,c):triggering (uncertain)",
    ),
    "AM-DER-POT-DEPENDENCY-STRUCT": (
        "potential",
        "p(a,b):S dependency, q(b,c):T structural => r(a,c):S",
    ),
    "AM-DER-POT-REV-STRUCT-DEPENDENCY": (
        "potential",
        "p(b,a):S structural, q(b,c):T dependency => r(a,c):T",
    ),
    "AM-DER-POT-DYNAMIC-STRUCT": ("potential", "p(a,b):S dynamic, q(b,c):T structural => r(a,c):S"),
    "AM-DER-POT-REV-STRUCT-DYNAMIC": (
        "potential",
        "p(b,a):S structural, q(b,c):T dynamic => r(a,c):T",
    ),
    "AM-DER-POT-FLOW-CHAIN": ("potential", "p(a,b):flow, q(b,c):flow => r(a,c):flow"),
    "AM-DER-POT-TRIGGERING-FLOW": ("potential", "p(a,b):triggering, q(b,c):flow => r(a,c):flow"),
    "AM-DER-POT-FLOW-TRIGGERING": ("potential", "p(a,b):flow, q(b,c):triggering => r(a,c):flow"),
    "AM-DER-POT-SERVING-CHAIN": ("potential", "p(a,b):serving, q(b,c):serving => r(a,c):serving"),
    "AM-DER-POT-SPECIALIZATION-INHERIT": (
        "potential",
        "p(a,b):specialization, q(b,c):T not specialization => r(a,c):T (uncertain)",
    ),
    "AM-DER-POT-SPECIALIZATION-REACH": (
        "potential",
        "p(a,b):T not specialization, q(c,b):specialization => r(a,c):T (uncertain)",
    ),
}


class DerivationTooLarge(Exception):
    """The chain search exceeded ``max_states``; the caller must narrow the scope."""

    def __init__(self, explored: int, limit: int) -> None:
        super().__init__(f"derivation explored {explored} chain states, above the limit {limit}")
        self.explored = explored
        self.limit = limit


@dataclass(frozen=True)
class Edge:
    id: str
    type: str
    source: str
    target: str


# A chain state: ``fwd`` = a relationship r(a, here) of ``type``; ``rev`` = here is
# structurally related TO a (a is a part/realizer-target of here), i.e. the chain
# so far is r(here, a). ``rev`` states only ever prefix a derivation (AM-DER-POT-REV-STRUCT-DEPENDENCY/AM-DER-POT-REV-STRUCT-DYNAMIC).
Kind = Literal["fwd", "rev"]


def _weakest(s: str, t: str) -> str:
    return s if STRUCTURAL.index(s) > STRUCTURAL.index(t) else t


def compose(kind: Kind, left: str, edge_type: str, forward: bool) -> tuple[Kind, str, str] | None:
    """One composition step: the chain so far, then one more stored relationship.

    ``forward`` is True when the step walks the edge source→target (q(b,c)) and
    False when it walks target→source (q(c,b)). Returns ``(kind, type, rule_id)``
    of the new chain, or ``None`` when no rule applies.
    """
    left_struct, right_struct = left in STRUCTURAL, edge_type in STRUCTURAL
    if kind == "rev":
        if not forward:
            return (
                ("rev", _weakest(left, edge_type), "AM-DER-STRUCTURAL-CHAIN")
                if right_struct
                else None
            )
        if edge_type in DEPENDENCY:
            return "fwd", edge_type, "AM-DER-POT-REV-STRUCT-DEPENDENCY"
        if edge_type in DYNAMIC:
            return "fwd", edge_type, "AM-DER-POT-REV-STRUCT-DYNAMIC"
        return None
    if not forward:
        if right_struct and left in DEPENDENCY:
            return "fwd", left, "AM-DER-DEPENDENCY-REV-STRUCT"
        if right_struct and left in DYNAMIC:
            return "fwd", left, "AM-DER-DYNAMIC-REV-STRUCT"
        if edge_type == SPECIALIZATION and left != SPECIALIZATION:
            return "fwd", left, "AM-DER-POT-SPECIALIZATION-REACH"
        return None
    if left == SPECIALIZATION:
        return (
            ("fwd", SPECIALIZATION, "AM-DER-SPECIALIZATION-CHAIN")
            if edge_type == SPECIALIZATION
            else (
                "fwd",
                edge_type,
                "AM-DER-POT-SPECIALIZATION-INHERIT",
            )
        )
    if left_struct and right_struct:
        return "fwd", _weakest(left, edge_type), "AM-DER-STRUCTURAL-CHAIN"
    if left_struct and edge_type in DEPENDENCY:
        return "fwd", edge_type, "AM-DER-STRUCT-DEPENDENCY"
    if left_struct and edge_type in DYNAMIC:
        return "fwd", edge_type, "AM-DER-STRUCT-DYNAMIC"
    if left == edge_type == "influence":
        return "fwd", "influence", "AM-DER-INFLUENCE-CHAIN"
    if left == edge_type == "triggering":
        return "fwd", "triggering", "AM-DER-TRIGGERING-CHAIN"
    if left in DEPENDENCY and right_struct:
        return "fwd", left, "AM-DER-POT-DEPENDENCY-STRUCT"
    if left in DYNAMIC and right_struct:
        return "fwd", left, "AM-DER-POT-DYNAMIC-STRUCT"
    if left == edge_type == "flow":
        return "fwd", "flow", "AM-DER-POT-FLOW-CHAIN"
    if (left, edge_type) == ("triggering", "flow"):
        return "fwd", "flow", "AM-DER-POT-TRIGGERING-FLOW"
    if (left, edge_type) == ("flow", "triggering"):
        return "fwd", "flow", "AM-DER-POT-FLOW-TRIGGERING"
    if left == edge_type == "serving":
        return "fwd", "serving", "AM-DER-POT-SERVING-CHAIN"
    return None


def derive_relationships(
    concepts: Mapping[str, str],
    relationships: Iterable[Edge],
    *,
    source_id: str | None = None,
    target_id: str | None = None,
    max_path_length: int = DEFAULT_MAX_PATH_LENGTH,
    include_potential: bool = True,
    max_states: int = DEFAULT_MAX_STATES,
) -> DeriveRelationshipsResult:
    """Every relationship derivable from chains of 2..``max_path_length`` stored ones.

    ``concepts`` maps concept id → ArchiMate element type. Relationships whose
    type is not an ArchiMate relationship type, or whose ends are not
    (non-junction) concepts, are not traversed. Paths are simple (no concept is
    visited twice). One result per (source, type, target): a valid derivation
    beats a potential one, then the shortest path, then the lexicographically
    smallest path — so the answer is deterministic.

    Raises :class:`DerivationTooLarge` rather than returning a partial set.
    """
    if max_path_length < 2:
        raise ValueError("max_path_length must be at least 2")
    reg = get_registry()
    rel_types = set(reg.relationship_types)
    nodes = {cid: t for cid, t in concepts.items() if t != "junction" and t in reg.concepts}

    edges = sorted(
        (
            e
            for e in relationships
            if e.type in rel_types and e.source in nodes and e.target in nodes
        ),
        key=lambda e: e.id,
    )
    out_edges: dict[str, list[Edge]] = {}
    in_edges: dict[str, list[Edge]] = {}
    direct: set[tuple[str, str, str]] = set()
    for e in edges:
        out_edges.setdefault(e.source, []).append(e)
        in_edges.setdefault(e.target, []).append(e)
        direct.add((e.source, e.type, e.target))

    best: dict[tuple[str, str, str], DerivedRelationship] = {}
    filtered: dict[tuple[str, str, str], FilteredDerivation] = {}
    states = 0

    def rank(d: DerivedRelationship) -> tuple[int, int, list[str]]:
        return (0 if d.certainty == "valid" else 1, len(d.path), d.path)

    def record(start: str, here: str, rtype: str, certainty: Certainty, rules: list[str], path):
        if here == start or (target_id is not None and here != target_id):
            return
        key = (start, rtype, here)
        common = {
            "type": rtype,
            "source_id": start,
            "target_id": here,
            "certainty": certainty,
            "rule_id": rules[-1],
            "rule_chain": list(rules),
            "path": list(path),
            "direct_exists": key in direct,
        }
        if not reg.is_valid(nodes[start], rtype, nodes[here]):
            cand = FilteredDerivation(
                **common,
                reason=(
                    f"{rtype} is not permitted from {nodes[start]} to {nodes[here]} "
                    f"(ArchiMate {reg.language_version}, Archi {reg.reference['version']} matrix)"
                ),
            )
            if key not in filtered or rank(cand) < rank(filtered[key]):
                filtered[key] = cand
            return
        cand = DerivedRelationship(**common)
        if key not in best or rank(cand) < rank(best[key]):
            best[key] = cand

    def walk(start, kind, rtype, here, certainty, rules, path, visited):
        nonlocal states
        states += 1
        if states > max_states:
            raise DerivationTooLarge(states, max_states)
        if kind == "fwd" and len(path) >= 2:
            record(start, here, rtype, certainty, rules, path)
        if len(path) >= max_path_length:
            return
        steps = [(e, True, e.target) for e in out_edges.get(here, [])]
        steps += [(e, False, e.source) for e in in_edges.get(here, [])]
        for e, forward, nxt in steps:
            if nxt in visited or e.id in path:
                continue
            step = compose(kind, rtype, e.type, forward)
            if step is None:
                continue
            nkind, ntype, rule = step
            ncert: Certainty = (
                "potential"
                if certainty == "potential" or RULES[rule][0] == "potential"
                else "valid"
            )
            if ncert == "potential" and not include_potential:
                continue
            walk(start, nkind, ntype, nxt, ncert, [*rules, rule], [*path, e.id], visited | {nxt})

    starts = [source_id] if source_id is not None else sorted(nodes)
    for start in starts:
        if start not in nodes:
            continue
        for e in out_edges.get(start, []):
            walk(start, "fwd", e.type, e.target, "valid", [], [e.id], {start, e.target})
        for e in in_edges.get(start, []):
            if e.type in STRUCTURAL:
                walk(start, "rev", e.type, e.source, "valid", [], [e.id], {start, e.source})

    def order(d: DerivedRelationship) -> tuple[str, str, str]:
        return (d.source_id, d.target_id, d.type)

    return DeriveRelationshipsResult(
        ruleset_id=reg.ruleset_id,
        language_version=reg.language_version,
        max_path_length=max_path_length,
        include_potential=include_potential,
        derived=sorted(best.values(), key=order),
        filtered=sorted(filtered.values(), key=order),
        examined=DerivationExamined(concepts=len(nodes), relationships=len(edges), states=states),
    )
