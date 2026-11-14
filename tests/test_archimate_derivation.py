"""ArchiMate 3.2 §5.7 relationship derivation — pure tests, one model per rule.

Every fixture is a small hand-built model whose expected derivation was worked
out from the rule statement (``p(a,b):S, q(b,c):T => r(a,c):U``), not read back
from the module. Each rule has a positive case and at least one negative case
that a looser implementation would wrongly derive. No database.
"""

from __future__ import annotations

import pytest

from tools.architecture.derivation import (
    DerivationTooLarge,
    Edge,
    derive_relationships,
)


def derive(concepts: dict[str, str], edges: list[tuple[str, str, str, str]], **kw):
    return derive_relationships(concepts, [Edge(*e) for e in edges], **kw)


def triples(result) -> set[tuple[str, str, str, str, str]]:
    return {(d.source_id, d.type, d.target_id, d.certainty, d.rule_id) for d in result.derived}


def between(result, a: str, c: str) -> set[tuple[str, str, str]]:
    return {
        (d.type, d.certainty, d.rule_id)
        for d in result.derived
        if d.source_id == a and d.target_id == c
    }


# ---------------------------------------------------------------- AM-DER-SPECIALIZATION-CHAIN specialization


def test_specialization_chain_is_specialization():
    r = derive(
        {"a": "business_actor", "b": "business_actor", "c": "business_actor"},
        [("p", "specialization", "a", "b"), ("q", "specialization", "b", "c")],
    )
    assert ("a", "specialization", "c", "valid", "AM-DER-SPECIALIZATION-CHAIN") in triples(r)


def test_two_specializations_into_the_same_parent_derive_nothing_between_children():
    r = derive(
        {"a": "business_actor", "b": "business_actor", "c": "business_actor"},
        [("p", "specialization", "a", "b"), ("q", "specialization", "c", "b")],
    )
    assert between(r, "a", "c") == set()
    assert between(r, "c", "a") == set()


# ---------------------------------------------------------------- AM-DER-STRUCTURAL-CHAIN structural chain


def test_composition_then_realization_derives_the_weaker_realization():
    r = derive(
        {"a": "application_component", "b": "application_component", "c": "application_service"},
        [("p", "composition", "a", "b"), ("q", "realization", "b", "c")],
    )
    assert between(r, "a", "c") == {("realization", "valid", "AM-DER-STRUCTURAL-CHAIN")}


def test_assignment_then_assignment_is_assignment():
    r = derive(
        {"a": "business_actor", "b": "business_role", "c": "business_process"},
        [("p", "assignment", "a", "b"), ("q", "assignment", "b", "c")],
    )
    assert between(r, "a", "c") == {("assignment", "valid", "AM-DER-STRUCTURAL-CHAIN")}


def test_needs_a_chain_not_two_wholes_of_one_part():
    # a and c both compose b: nothing structural follows between a and c.
    r = derive(
        {"a": "application_component", "b": "application_component", "c": "application_component"},
        [("p", "composition", "a", "b"), ("q", "composition", "c", "b")],
    )
    assert between(r, "a", "c") == set()


# ---------------------------------------------------------------- AM-DER-STRUCT-DEPENDENCY structural ∘ dependency


def test_assignment_then_serving_derives_serving():
    r = derive(
        {"a": "application_component", "b": "application_function", "c": "business_process"},
        [("p", "assignment", "a", "b"), ("q", "serving", "b", "c")],
    )
    assert between(r, "a", "c") == {("serving", "valid", "AM-DER-STRUCT-DEPENDENCY")}


def test_dependency_first_then_structural_is_not_valid():
    # serving(a,b) then composition(b,c) is AM-DER-POT-DEPENDENCY-STRUCT (potential) — never AM-DER-STRUCT-DEPENDENCY.
    r = derive(
        {"a": "application_service", "b": "business_process", "c": "business_process"},
        [("p", "serving", "a", "b"), ("q", "composition", "b", "c")],
        include_potential=False,
    )
    assert between(r, "a", "c") == set()


# ---------------------------------------------------------------- AM-DER-DEPENDENCY-REV-STRUCT dependency ∘ reverse structural


def test_serving_a_role_serves_the_actor_assigned_to_it():
    r = derive(
        {"a": "application_service", "b": "business_role", "c": "business_actor"},
        [("p", "serving", "a", "b"), ("q", "assignment", "c", "b")],
    )
    assert between(r, "a", "c") == {("serving", "valid", "AM-DER-DEPENDENCY-REV-STRUCT")}


def test_access_through_a_realizing_artifact():
    r = derive(
        {"a": "application_function", "b": "data_object", "c": "artifact"},
        [("p", "access", "a", "b"), ("q", "realization", "c", "b")],
    )
    assert between(r, "a", "c") == {("access", "valid", "AM-DER-DEPENDENCY-REV-STRUCT")}


def test_reverse_dependency_is_not_traversed():
    # c serves b (dependency INTO b, not structural): nothing for a -> c.
    r = derive(
        {"a": "application_service", "b": "business_role", "c": "application_service"},
        [("p", "serving", "a", "b"), ("q", "serving", "c", "b")],
    )
    assert between(r, "a", "c") == set()


# ---------------------------------------------------------------- AM-DER-INFLUENCE-CHAIN influence chain


def test_influence_chain_is_influence():
    r = derive(
        {"a": "goal", "b": "goal", "c": "goal"},
        [("p", "influence", "a", "b"), ("q", "influence", "b", "c")],
    )
    assert between(r, "a", "c") == {("influence", "valid", "AM-DER-INFLUENCE-CHAIN")}


def test_influence_then_association_derives_nothing():
    r = derive(
        {"a": "goal", "b": "goal", "c": "goal"},
        [("p", "influence", "a", "b"), ("q", "association", "b", "c")],
    )
    assert between(r, "a", "c") == set()


# ---------------------------------------------------------------- AM-DER-STRUCT-DYNAMIC structural ∘ dynamic


def test_assignment_then_triggering_derives_triggering():
    r = derive(
        {"a": "business_role", "b": "business_process", "c": "business_process"},
        [("p", "assignment", "a", "b"), ("q", "triggering", "b", "c")],
    )
    assert between(r, "a", "c") == {("triggering", "valid", "AM-DER-STRUCT-DYNAMIC")}


def test_assignment_then_flow_derives_flow():
    r = derive(
        {"a": "business_role", "b": "business_process", "c": "business_process"},
        [("p", "assignment", "a", "b"), ("q", "flow", "b", "c")],
    )
    assert between(r, "a", "c") == {("flow", "valid", "AM-DER-STRUCT-DYNAMIC")}


# ---------------------------------------------------------------- AM-DER-DYNAMIC-REV-STRUCT dynamic ∘ reverse structural


def test_triggering_a_part_triggers_its_whole():
    # a triggers b; c is composed of b  =>  a triggers c.
    r = derive(
        {"a": "business_process", "b": "business_process", "c": "business_process"},
        [("p", "triggering", "a", "b"), ("q", "composition", "c", "b")],
    )
    assert between(r, "a", "c") == {("triggering", "valid", "AM-DER-DYNAMIC-REV-STRUCT")}


def test_does_not_cross_a_reverse_dynamic_relationship():
    r = derive(
        {"a": "business_process", "b": "business_process", "c": "business_process"},
        [("p", "triggering", "a", "b"), ("q", "triggering", "c", "b")],
    )
    assert between(r, "a", "c") == set()


# ---------------------------------------------------------------- AM-DER-TRIGGERING-CHAIN triggering chain


def test_triggering_chain_is_triggering():
    r = derive(
        {"a": "business_event", "b": "business_process", "c": "business_process"},
        [("p", "triggering", "a", "b"), ("q", "triggering", "b", "c")],
    )
    assert between(r, "a", "c") == {("triggering", "valid", "AM-DER-TRIGGERING-CHAIN")}


# ---------------------------------------------------------------- potential rules


def test_serving_a_whole_potentially_serves_its_part():
    r = derive(
        {"a": "application_service", "b": "business_process", "c": "business_process"},
        [("p", "serving", "a", "b"), ("q", "composition", "b", "c")],
    )
    assert between(r, "a", "c") == {("serving", "potential", "AM-DER-POT-DEPENDENCY-STRUCT")}


def test_part_of_a_serving_whole_potentially_serves():
    # b composes a; b serves c  =>  a potentially serves c.
    r = derive(
        {"a": "application_component", "b": "application_component", "c": "business_process"},
        [("p", "composition", "b", "a"), ("q", "serving", "b", "c")],
    )
    assert between(r, "a", "c") == {("serving", "potential", "AM-DER-POT-REV-STRUCT-DEPENDENCY")}


def test_triggering_a_whole_potentially_triggers_its_part():
    r = derive(
        {"a": "business_process", "b": "business_process", "c": "business_process"},
        [("p", "triggering", "a", "b"), ("q", "composition", "b", "c")],
    )
    assert between(r, "a", "c") == {("triggering", "potential", "AM-DER-POT-DYNAMIC-STRUCT")}


def test_part_of_a_triggering_whole_potentially_triggers():
    r = derive(
        {"a": "business_process", "b": "business_process", "c": "business_process"},
        [("p", "composition", "b", "a"), ("q", "triggering", "b", "c")],
    )
    assert between(r, "a", "c") == {("triggering", "potential", "AM-DER-POT-REV-STRUCT-DYNAMIC")}


@pytest.mark.parametrize(
    ("first", "second", "rule"),
    [
        ("flow", "flow", "AM-DER-POT-FLOW-CHAIN"),
        ("triggering", "flow", "AM-DER-POT-TRIGGERING-FLOW"),
        ("flow", "triggering", "AM-DER-POT-FLOW-TRIGGERING"),
    ],
)
def test_flow_chains_are_potential_flow(first, second, rule):
    r = derive(
        {"a": "business_process", "b": "business_process", "c": "business_process"},
        [("p", first, "a", "b"), ("q", second, "b", "c")],
    )
    assert between(r, "a", "c") == {("flow", "potential", rule)}


def test_serving_chain_is_potential_serving():
    r = derive(
        {"a": "technology_service", "b": "application_component", "c": "business_process"},
        [("p", "serving", "a", "b"), ("q", "serving", "b", "c")],
    )
    assert between(r, "a", "c") == {("serving", "potential", "AM-DER-POT-SERVING-CHAIN")}


def test_access_then_association_derives_nothing():
    r = derive(
        {"a": "application_function", "b": "data_object", "c": "application_function"},
        [("p", "access", "a", "b"), ("q", "association", "b", "c")],
    )
    assert between(r, "a", "c") == set()


def test_a_specialization_potentially_inherits_its_parents_relationship():
    r = derive(
        {"a": "business_actor", "b": "business_actor", "c": "business_role"},
        [("p", "specialization", "a", "b"), ("q", "assignment", "b", "c")],
    )
    assert between(r, "a", "c") == {
        ("assignment", "potential", "AM-DER-POT-SPECIALIZATION-INHERIT")
    }


def test_a_relationship_to_a_parent_potentially_reaches_its_specialization():
    r = derive(
        {"a": "business_actor", "b": "business_role", "c": "business_role"},
        [("p", "assignment", "a", "b"), ("q", "specialization", "c", "b")],
    )
    assert between(r, "a", "c") == {("assignment", "potential", "AM-DER-POT-SPECIALIZATION-REACH")}


def test_include_potential_false_drops_every_pdr():
    r = derive(
        {"a": "business_process", "b": "business_process", "c": "business_process"},
        [("p", "flow", "a", "b"), ("q", "flow", "b", "c")],
        include_potential=False,
    )
    assert r.derived == []


# ---------------------------------------------------------------- composition over longer chains


def test_three_step_chain_carries_provenance_in_order():
    # actor -assign-> role -assign-> process -serving-> process2
    r = derive(
        {
            "a": "business_actor",
            "b": "business_role",
            "c": "business_process",
            "d": "business_process",
        },
        [
            ("r1", "assignment", "a", "b"),
            ("r2", "assignment", "b", "c"),
            ("r3", "serving", "c", "d"),
        ],
    )
    (d,) = [x for x in r.derived if (x.source_id, x.target_id) == ("a", "d")]
    assert (d.type, d.certainty, d.rule_chain, d.path) == (
        "serving",
        "valid",
        ["AM-DER-STRUCTURAL-CHAIN", "AM-DER-STRUCT-DEPENDENCY"],
        ["r1", "r2", "r3"],
    )


def test_a_potential_step_makes_the_whole_chain_potential():
    # serving -> composition (AM-DER-POT-DEPENDENCY-STRUCT) -> composition (AM-DER-POT-DEPENDENCY-STRUCT)
    r = derive(
        {
            "a": "application_service",
            "b": "business_process",
            "c": "business_process",
            "d": "business_process",
        },
        [
            ("r1", "serving", "a", "b"),
            ("r2", "composition", "b", "c"),
            ("r3", "composition", "c", "d"),
        ],
    )
    assert between(r, "a", "d") == {("serving", "potential", "AM-DER-POT-DEPENDENCY-STRUCT")}


def test_max_path_length_bounds_the_chain():
    edges = [
        ("r1", "assignment", "a", "b"),
        ("r2", "assignment", "b", "c"),
        ("r3", "serving", "c", "d"),
    ]
    concepts = {
        "a": "business_actor",
        "b": "business_role",
        "c": "business_process",
        "d": "business_process",
    }
    assert between(derive(concepts, edges, max_path_length=2), "a", "d") == set()
    assert between(derive(concepts, edges, max_path_length=3), "a", "d") != set()


def test_a_single_stored_relationship_is_not_reported_as_derived():
    r = derive({"a": "business_actor", "b": "business_role"}, [("p", "assignment", "a", "b")])
    assert r.derived == []


def test_direct_exists_marks_a_derivation_already_stored():
    r = derive(
        {"a": "business_actor", "b": "business_role", "c": "business_process"},
        [
            ("p", "assignment", "a", "b"),
            ("q", "assignment", "b", "c"),
            ("d", "assignment", "a", "c"),
        ],
    )
    (x,) = [x for x in r.derived if (x.source_id, x.target_id) == ("a", "c")]
    assert x.direct_exists is True and x.path == ["p", "q"]


def test_valid_wins_over_potential_for_the_same_triple():
    # a->c serving: valid via AM-DER-STRUCT-DEPENDENCY (assignment, serving) and potential via AM-DER-POT-SERVING-CHAIN.
    r = derive(
        {
            "a": "application_component",
            "b": "application_function",
            "s": "application_service",
            "c": "business_process",
        },
        [
            ("r1", "assignment", "a", "b"),
            ("r2", "serving", "b", "c"),
            ("r3", "serving", "a", "s"),
            ("r4", "serving", "s", "c"),
        ],
    )
    assert between(r, "a", "c") == {("serving", "valid", "AM-DER-STRUCT-DEPENDENCY")}


# ---------------------------------------------------------------- matrix filter, scope, bounds


def test_a_derivation_the_matrix_forbids_is_filtered_not_derived():
    # actor assigned to work package; work package accesses deliverable. AM-DER-STRUCT-DEPENDENCY yields
    # access(actor, deliverable), which the Archi matrix forbids.
    r = derive(
        {"a": "business_actor", "b": "work_package", "c": "deliverable"},
        [("p", "assignment", "a", "b"), ("q", "access", "b", "c")],
    )
    assert between(r, "a", "c") == set()
    (f,) = [f for f in r.filtered if (f.source_id, f.target_id) == ("a", "c")]
    assert (f.type, f.rule_id, f.path) == ("access", "AM-DER-STRUCT-DEPENDENCY", ["p", "q"])
    assert "not permitted" in f.reason


def test_junctions_and_non_archimate_edges_are_not_traversed():
    r = derive(
        {"a": "business_actor", "j": "junction", "b": "business_role", "c": "business_process"},
        [
            ("p", "assignment", "a", "j"),
            ("q", "assignment", "j", "c"),
            ("x", "depends_on", "a", "b"),
            ("y", "assignment", "b", "c"),
        ],
    )
    assert r.derived == []
    assert r.examined.relationships == 1


def test_source_and_target_filters():
    concepts = {
        "a": "business_actor",
        "b": "business_role",
        "c": "business_process",
        "d": "business_process",
    }
    edges = [
        ("r1", "assignment", "a", "b"),
        ("r2", "assignment", "b", "c"),
        ("r3", "serving", "c", "d"),
    ]
    only_b = derive(concepts, edges, source_id="b")
    assert {d.source_id for d in only_b.derived} == {"b"}
    to_d = derive(concepts, edges, target_id="d")
    assert {d.target_id for d in to_d.derived} == {"d"}
    assert {d.source_id for d in to_d.derived} == {"a", "b"}


def test_the_search_bound_refuses_instead_of_truncating():
    concepts = {f"p{i}": "business_process" for i in range(12)}
    edges = [
        (f"t{i}-{j}", "triggering", f"p{i}", f"p{j}")
        for i in range(12)
        for j in range(12)
        if i != j
    ]
    with pytest.raises(DerivationTooLarge):
        derive(concepts, edges, max_states=500)
