"""Pure tests for the UML 2.5.1 profile validator (``uml-2.5.1-p1``).

No database fixtures are used: ``validate`` is a pure function over a snapshot.
Every rule has a positive (clean) case and at least one negative case that
asserts the exact ``rule_id`` fires.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from schemas.uml import UmlSnapshot
from tools.uml.profile import get_profile
from tools.uml.rules import (
    REQUIRED_RULES,
    RULES,
    RULES_BY_ID,
    UnknownRuleError,
    parse_multiplicity,
    ruleset_info,
    validate,
)

# ---------------------------------------------------------------------------
# A full class + component + sequence model that validates clean.
# ---------------------------------------------------------------------------

_BASE: dict[str, Any] = {
    "elements": [
        {"id": "pkg", "kind": "package", "name": "orders"},
        {
            "id": "iface_repo",
            "kind": "interface",
            "name": "Repository",
            "owner_id": "pkg",
            "attributes": {
                "operations": [
                    {
                        "id": "op_save",
                        "name": "save",
                        "visibility": "public",
                        "is_abstract": True,
                        "parameters": [
                            {
                                "id": "p_save_item",
                                "name": "item",
                                "type_ref": "cls_item",
                                "direction": "in",
                                "multiplicity": "1",
                            }
                        ],
                    }
                ]
            },
        },
        {
            "id": "cls_base",
            "kind": "class",
            "name": "BaseService",
            "owner_id": "pkg",
            "attributes": {
                "is_abstract": True,
                "operations": [{"id": "op_log", "name": "log"}],
            },
        },
        {
            "id": "cls_svc",
            "kind": "class",
            "name": "OrderService",
            "owner_id": "pkg",
            "attributes": {
                "properties": [
                    {
                        "id": "prop_repo",
                        "name": "repo",
                        "type_ref": "iface_repo",
                        "multiplicity": "0..1",
                        "visibility": "private",
                    }
                ],
                "operations": [
                    {
                        "id": "op_handle",
                        "name": "handle",
                        "parameters": [{"id": "p_h", "name": "id", "type_ref": "dt_id"}],
                        "return_type_ref": "cls_item",
                    }
                ],
            },
        },
        {
            "id": "cls_item",
            "kind": "class",
            "name": "Item",
            "owner_id": "pkg",
            "attributes": {
                "properties": [
                    {
                        "id": "prop_tags",
                        "name": "tags",
                        "type_ref": "prim_str",
                        "multiplicity": "*",
                    },
                    {"id": "prop_state", "name": "state", "type_ref": "enum_status"},
                ]
            },
        },
        {"id": "cls_repo_impl", "kind": "class", "name": "SqlRepository", "owner_id": "pkg"},
        {"id": "dt_id", "kind": "data_type", "name": "OrderId", "owner_id": "pkg"},
        {
            "id": "enum_status",
            "kind": "enumeration",
            "name": "Status",
            "owner_id": "pkg",
            "attributes": {"literals": ["open", "closed"]},
        },
        {"id": "prim_str", "kind": "primitive_type", "name": "String", "owner_id": "pkg"},
        {"id": "comp_api", "kind": "component", "name": "Api", "owner_id": "pkg"},
        {"id": "comp_store", "kind": "component", "name": "Store", "owner_id": "pkg"},
        {
            "id": "port_api",
            "kind": "port",
            "name": "repoOut",
            "owner_id": "comp_api",
            "attributes": {"required": ["iface_repo"]},
        },
        {
            "id": "port_store",
            "kind": "port",
            "name": "repoIn",
            "owner_id": "comp_store",
            "attributes": {"provided": ["iface_repo"]},
        },
        {"id": "actor_user", "kind": "actor", "name": "Customer", "owner_id": "pkg"},
    ],
    "relationships": [
        {"id": "gen1", "kind": "generalization", "source": "cls_svc", "target": "cls_base"},
        {
            "id": "assoc1",
            "kind": "association",
            "ends": [
                {
                    "element_ref": "cls_svc",
                    "role": "owner",
                    "multiplicity": "1",
                    "aggregation": "composite",
                },
                {
                    "element_ref": "cls_item",
                    "role": "items",
                    "multiplicity": "0..*",
                    "navigable": True,
                },
            ],
        },
        {
            "id": "ir1",
            "kind": "interface_realization",
            "source": "cls_repo_impl",
            "target": "iface_repo",
        },
        {"id": "rl1", "kind": "realization", "source": "comp_store", "target": "cls_repo_impl"},
        {"id": "dep1", "kind": "dependency", "source": "cls_svc", "target": "dt_id"},
        {"id": "use1", "kind": "usage", "source": "comp_api", "target": "comp_store"},
        {
            "id": "conn1",
            "kind": "connector",
            "connector_kind": "assembly",
            "source": "port_api",
            "target": "port_store",
        },
    ],
    "interactions": [
        {
            "id": "int1",
            "name": "place order",
            "owner_id": "pkg",
            "lifelines": [
                {"id": "ll_user", "name": "u", "represents_ref": "actor_user"},
                {"id": "ll_svc", "name": "s", "represents_ref": "cls_svc"},
                {"id": "ll_repo", "name": "r", "represents_ref": "iface_repo"},
            ],
            "occurrences": [
                {"id": "o1", "lifeline": "ll_user"},
                {"id": "o2", "lifeline": "ll_svc"},
                {"id": "o3", "lifeline": "ll_svc"},
                {"id": "o4", "lifeline": "ll_repo"},
                {"id": "o5", "lifeline": "ll_repo"},
                {"id": "o6", "lifeline": "ll_svc"},
                {"id": "o7", "lifeline": "ll_svc"},
                {"id": "o8", "lifeline": "ll_user"},
                {"id": "o9", "lifeline": "ll_user"},
                {"id": "o10", "lifeline": "ll_svc"},
            ],
            "messages": [
                {
                    "id": "m1",
                    "kind": "synch_call",
                    "send_occurrence": "o1",
                    "receive_occurrence": "o2",
                    "operation_ref": "op_handle",
                    "arguments": ["42"],
                },
                {
                    "id": "m2",
                    "kind": "synch_call",
                    "send_occurrence": "o3",
                    "receive_occurrence": "o4",
                    "operation_ref": "op_save",
                },
                {
                    "id": "m3",
                    "kind": "reply",
                    "send_occurrence": "o5",
                    "receive_occurrence": "o6",
                    "operation_ref": "op_save",
                },
                {
                    "id": "m4",
                    "kind": "reply",
                    "send_occurrence": "o7",
                    "receive_occurrence": "o8",
                },
                # op_log is owned by cls_base, a supertype of the receiver cls_svc.
                {
                    "id": "m5",
                    "kind": "asynch_call",
                    "send_occurrence": "o9",
                    "receive_occurrence": "o10",
                    "operation_ref": "op_log",
                },
            ],
            "executions": [
                {"id": "ex1", "lifeline": "ll_svc", "start": "o2", "finish": "o7"},
                {"id": "ex2", "lifeline": "ll_repo", "start": "o4", "finish": "o5"},
            ],
            "fragments": [
                {
                    "id": "fr1",
                    "operator": "opt",
                    "covered": ["ll_svc", "ll_repo"],
                    "operands": [{"guard": "valid", "occurrences": ["o3", "o4", "o5", "o6"]}],
                }
            ],
        }
    ],
    "diagrams": [
        {
            "id": "d_class",
            "kind": "class",
            "objects": [
                {"id": "ob_svc", "element_ref": "cls_svc"},
                {"id": "ob_item", "element_ref": "cls_item"},
                {"id": "ob_base", "element_ref": "cls_base"},
            ],
            "connections": [
                # Association is undirected: drawn item -> svc is fine.
                {
                    "id": "c_assoc",
                    "relationship_ref": "assoc1",
                    "source_object_id": "ob_item",
                    "target_object_id": "ob_svc",
                },
                {
                    "id": "c_gen",
                    "relationship_ref": "gen1",
                    "source_object_id": "ob_svc",
                    "target_object_id": "ob_base",
                },
            ],
        },
        {
            "id": "d_class2",
            "kind": "class",
            "objects": [{"id": "ob2_svc", "element_ref": "cls_svc"}],
        },
        {
            "id": "d_comp",
            "kind": "component",
            "objects": [
                {"id": "ob_capi", "element_ref": "comp_api"},
                {"id": "ob_papi", "element_ref": "port_api"},
                {"id": "ob_pstore", "element_ref": "port_store"},
            ],
            "connections": [
                {
                    "id": "c_conn",
                    "relationship_ref": "conn1",
                    "source_object_id": "ob_papi",
                    "target_object_id": "ob_pstore",
                }
            ],
        },
        {
            "id": "d_seq",
            "kind": "sequence",
            "objects": [
                {"id": "ol_user", "lifeline_id": "ll_user"},
                {"id": "ol_svc", "lifeline_id": "ll_svc"},
            ],
            "connections": [
                {
                    "id": "c_m1",
                    "message_id": "m1",
                    "source_object_id": "ol_user",
                    "target_object_id": "ol_svc",
                }
            ],
        },
    ],
}


def base() -> dict[str, Any]:
    return copy.deepcopy(_BASE)


def run(d: dict[str, Any], **kw: Any):
    return validate(UmlSnapshot.model_validate(d), **kw)


def rule_ids(d: dict[str, Any]) -> list[str]:
    return [f.rule_id for f in run(d).findings]


def el(d: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(e for e in d["elements"] if e["id"] == ident)


def rel(d: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(r for r in d["relationships"] if r["id"] == ident)


def inter(d: dict[str, Any]) -> dict[str, Any]:
    return d["interactions"][0]


def msg(d: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(m for m in inter(d)["messages"] if m["id"] == ident)


def diagram(d: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(x for x in d["diagrams"] if x["id"] == ident)


def assert_fires(d: dict[str, Any], rule_id: str) -> None:
    got = rule_ids(d)
    assert rule_id in got, f"{rule_id} did not fire; got {got}"


def assert_only(d: dict[str, Any], rule_id: str) -> None:
    got = set(rule_ids(d))
    assert got == {rule_id}, f"expected only {rule_id}; got {sorted(got)}"


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_full_class_component_sequence_model_is_clean():
    report = run(base())
    assert report.findings == []
    assert report.status == "complete"
    assert report.clean is True
    assert report.ruleset_id == "uml-2.5.1-p1"
    assert set(report.enabled_rules) == set(RULES_BY_ID)
    assert report.examined["uml_element"] == 14
    assert report.examined["diagram_connection"] == 4


@pytest.mark.parametrize("rule_id", sorted(RULES_BY_ID))
def test_every_rule_is_silent_on_the_clean_model(rule_id):
    report = run(base(), rules=[rule_id])
    assert report.enabled_rules == [rule_id]
    assert [f.rule_id for f in report.findings] == []


def test_profile_and_ruleset_metadata():
    prof = get_profile()
    assert prof.profile_id == "uml-2.5.1-p1"
    assert prof.reference == {
        "tool": "OMG UML",
        "version": "2.5.1",
        "url": "https://www.omg.org/spec/UML/2.5.1",
    }
    assert set(prof.element_kinds) == {
        "package",
        "class",
        "interface",
        "data_type",
        "enumeration",
        "primitive_type",
        "component",
        "port",
        "actor",
    }
    assert set(prof.diagram_kinds) == {"class", "component", "sequence"}
    info = ruleset_info()
    assert info.language == "uml"
    assert info.language_version == "2.5.1"
    assert info.rule_count == len(RULES) == 15
    assert RULES_BY_ID["UML-SEQ-003"].severity == "warning"
    assert "UML-SEQ-003" not in REQUIRED_RULES
    assert REQUIRED_RULES == {r.rule_id for r in RULES if r.severity == "error"}
    assert ruleset_info(with_rules=False).rules == []


def test_class_in_two_diagrams_has_one_identity_and_is_clean():
    d = base()
    shown = [o["element_ref"] for x in d["diagrams"] for o in x["objects"] if o.get("element_ref")]
    assert shown.count("cls_svc") == 2
    assert run(d).clean is True


def test_unknown_element_kind_is_unsupported_and_never_clean():
    d = base()
    d["elements"].append({"id": "uc1", "kind": "use_case", "name": "Buy"})
    report = run(d)
    assert [f.rule_id for f in report.findings] == ["UML-UNSUP-001"]
    assert report.findings[0].path == "/kind"
    assert report.findings[0].targets[0].id == "uc1"
    assert report.clean is False


def test_disabling_a_required_rule_is_incomplete_and_not_clean():
    report = run(base(), disable=["UML-REF-001"])
    assert report.findings == []
    assert report.status == "incomplete"
    assert report.clean is False


def test_disabling_the_warning_rule_stays_clean():
    report = run(base(), disable=["UML-SEQ-003"])
    assert report.status == "complete"
    assert report.clean is True


def test_unknown_rule_id_raises():
    with pytest.raises(UnknownRuleError):
        run(base(), rules=["UML-NOPE-001"])
    with pytest.raises(UnknownRuleError):
        run(base(), disable=["AM-REL-001"])


def test_without_diagrams_the_diagram_rules_are_not_enabled_and_run_is_clean():
    d = base()
    d["diagrams"] = None
    report = run(d)
    assert "UML-DIA-001" not in report.enabled_rules
    assert "UML-DIA-002" not in report.enabled_rules
    assert report.status == "complete"
    assert report.clean is True


def test_output_is_deterministic():
    d = base()
    d["elements"].append({"id": "uc1", "kind": "use_case"})
    el(d, "cls_svc")["attributes"]["properties"][0]["multiplicity"] = "3..1"
    rel(d, "gen1")["target"] = "nope"
    msg(d, "m3")["operation_ref"] = "op_handle"
    first = run(d).model_dump()
    second = run(copy.deepcopy(d)).model_dump()
    assert first == second
    assert len(first["findings"]) >= 4
    severities = [f["severity"] for f in first["findings"]]
    assert severities == sorted(severities, key=["error", "warning", "advice"].index)


def test_findings_carry_ruleset_and_targets():
    d = base()
    rel(d, "ir1")["target"] = "cls_base"
    (f,) = [f for f in run(d).findings if f.rule_id == "UML-RLZ-001"]
    assert f.ruleset_id == "uml-2.5.1-p1"
    assert f.severity == "error"
    assert [t.kind for t in f.targets] == ["uml_relationship", "uml_element", "uml_element"]
    assert f.evidence == {"target_kind": "class"}


# ---------------------------------------------------------------------------
# UML-REF-001
# ---------------------------------------------------------------------------


def test_ref001_property_type_unresolved():
    d = base()
    el(d, "cls_svc")["attributes"]["properties"][0]["type_ref"] = "ghost"
    report = run(d)
    (f,) = report.findings
    assert f.rule_id == "UML-REF-001"
    assert f.path == "/attributes/properties/0/type_ref"


def test_ref001_relationship_end_missing_or_unresolved():
    d = base()
    rel(d, "dep1")["target"] = None
    assert_only(d, "UML-REF-001")
    d = base()
    rel(d, "use1")["source"] = "ghost"
    assert_only(d, "UML-REF-001")


def test_ref001_message_operation_and_occurrence_unresolved():
    d = base()
    msg(d, "m5")["operation_ref"] = "op_ghost"
    assert_only(d, "UML-REF-001")
    d = base()
    msg(d, "m5")["receive_occurrence"] = "o_ghost"
    assert_fires(d, "UML-REF-001")


def test_ref001_provided_interface_must_be_an_interface():
    d = base()
    el(d, "port_store")["attributes"]["provided"] = ["cls_item"]
    assert_fires(d, "UML-REF-001")


def test_ref001_owner_and_lifeline_type_unresolved():
    d = base()
    el(d, "cls_item")["owner_id"] = "ghost_pkg"
    assert_only(d, "UML-REF-001")
    d = base()
    inter(d)["lifelines"][0]["represents_ref"] = "ghost"
    assert_only(d, "UML-REF-001")


def test_ref001_positive_unspecified_refs_are_allowed():
    d = base()
    inter(d)["lifelines"][0]["represents_ref"] = None
    el(d, "cls_svc")["attributes"]["properties"][0]["type_ref"] = None
    assert run(d).clean is True


# ---------------------------------------------------------------------------
# UML-REF-002
# ---------------------------------------------------------------------------


def test_ref002_duplicate_element_id():
    d = base()
    d["elements"].append({"id": "cls_item", "kind": "class", "owner_id": "pkg"})
    assert_only(d, "UML-REF-002")


def test_ref002_duplicate_id_across_kinds():
    d = base()
    inter(d)["messages"][0]["id"] = "gen1"
    for x in d["diagrams"]:
        for c in x.get("connections", []):
            if c.get("message_id") == "m1":
                c["message_id"] = "gen1"
    assert_fires(d, "UML-REF-002")


# ---------------------------------------------------------------------------
# UML-OWN-001
# ---------------------------------------------------------------------------


def test_own001_ownership_cycle():
    d = base()
    d["elements"].append({"id": "pkg2", "kind": "package", "owner_id": "pkg"})
    el(d, "pkg")["owner_id"] = "pkg2"
    report = run(d)
    (f,) = report.findings
    assert f.rule_id == "UML-OWN-001"
    assert [t.id for t in f.targets] == ["pkg", "pkg2"]


def test_own001_self_owned():
    d = base()
    el(d, "pkg")["owner_id"] = "pkg"
    assert_only(d, "UML-OWN-001")


def test_own001_port_without_owner_and_class_owned_by_class():
    d = base()
    el(d, "port_api")["owner_id"] = None
    assert_only(d, "UML-OWN-001")
    d = base()
    el(d, "cls_item")["owner_id"] = "cls_svc"
    assert_only(d, "UML-OWN-001")


def test_own001_feature_owned_by_two_elements():
    d = base()
    el(d, "cls_repo_impl")["attributes"] = {"operations": [{"id": "op_save", "name": "save"}]}
    assert_only(d, "UML-OWN-001")


def test_own001_positive_port_owned_by_class():
    d = base()
    el(d, "port_api")["owner_id"] = "cls_svc"
    assert run(d).clean is True


# ---------------------------------------------------------------------------
# UML-GEN-001 / UML-GEN-002
# ---------------------------------------------------------------------------


def test_gen001_generalization_cycle():
    d = base()
    d["relationships"].append(
        {"id": "gen2", "kind": "generalization", "source": "cls_base", "target": "cls_svc"}
    )
    report = run(d)
    assert [f.rule_id for f in report.findings] == ["UML-GEN-001"]
    assert report.findings[0].evidence == {"cycle": ["cls_base", "cls_svc"]}


def test_gen001_self_generalization():
    d = base()
    d["relationships"].append(
        {"id": "g_self", "kind": "generalization", "source": "cls_item", "target": "cls_item"}
    )
    report = run(d)
    assert [f.rule_id for f in report.findings] == ["UML-GEN-001"]
    assert report.findings[0].evidence == {"cycle": ["cls_item"]}


def test_gen001_positive_diamond_is_not_a_cycle():
    d = base()
    d["elements"].append({"id": "cls_top", "kind": "class", "owner_id": "pkg"})
    d["relationships"] += [
        {"id": "g_a", "kind": "generalization", "source": "cls_item", "target": "cls_base"},
        {"id": "g_b", "kind": "generalization", "source": "cls_base", "target": "cls_top"},
        {"id": "g_c", "kind": "generalization", "source": "cls_svc", "target": "cls_top"},
    ]
    assert run(d).clean is True


def test_gen002_interface_cannot_specialize_a_class():
    d = base()
    d["relationships"].append(
        {"id": "g_bad", "kind": "generalization", "source": "iface_repo", "target": "cls_base"}
    )
    assert_only(d, "UML-GEN-002")


def test_gen002_class_cannot_specialize_a_component_or_package():
    d = base()
    d["relationships"].append(
        {"id": "g_bad", "kind": "generalization", "source": "cls_item", "target": "comp_api"}
    )
    assert_only(d, "UML-GEN-002")
    d = base()
    d["relationships"].append(
        {"id": "g_bad", "kind": "generalization", "source": "pkg", "target": "pkg"}
    )
    assert "UML-GEN-002" in rule_ids(d)


def test_gen002_positive_compatible_kinds():
    d = base()
    d["relationships"] += [
        {"id": "g1", "kind": "generalization", "source": "comp_api", "target": "cls_base"},
        {"id": "g2", "kind": "generalization", "source": "enum_status", "target": "dt_id"},
    ]
    assert run(d).clean is True


# ---------------------------------------------------------------------------
# UML-MUL-001
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["2..1", "abc", "-1", "1..", "*..1", "1..2..3", ""])
def test_mul001_invalid_multiplicity(value):
    d = base()
    el(d, "cls_svc")["attributes"]["properties"][0]["multiplicity"] = value
    report = run(d)
    (f,) = report.findings
    assert f.rule_id == "UML-MUL-001"
    assert f.path == "/attributes/properties/0/multiplicity"


def test_mul001_association_end_and_parameter():
    d = base()
    rel(d, "assoc1")["ends"][1]["multiplicity"] = "5..2"
    assert_only(d, "UML-MUL-001")
    d = base()
    el(d, "iface_repo")["attributes"]["operations"][0]["parameters"][0]["multiplicity"] = "x"
    assert_only(d, "UML-MUL-001")


@pytest.mark.parametrize(
    ("value", "parsed"),
    [("1", (1, 1)), ("0..1", (0, 1)), ("*", (0, None)), ("1..*", (1, None)), ("0", (0, 0))],
)
def test_mul001_positive_valid_multiplicities(value, parsed):
    assert parse_multiplicity(value) == parsed
    d = base()
    el(d, "cls_svc")["attributes"]["properties"][0]["multiplicity"] = value
    assert run(d).clean is True


# ---------------------------------------------------------------------------
# UML-RLZ-001
# ---------------------------------------------------------------------------


def test_rlz001_interface_realization_targets_a_class():
    d = base()
    rel(d, "ir1")["target"] = "cls_base"
    assert_only(d, "UML-RLZ-001")


def test_rlz001_positive_plain_realization_may_target_a_class():
    d = base()
    assert rel(d, "rl1")["target"] == "cls_repo_impl"
    assert run(d, rules=["UML-RLZ-001"]).findings == []


# ---------------------------------------------------------------------------
# UML-CON-001
# ---------------------------------------------------------------------------


def test_con001_connector_end_is_not_a_port():
    d = base()
    rel(d, "conn1")["target"] = "comp_store"
    d["diagrams"] = None
    report = run(d)
    (f,) = report.findings
    assert f.rule_id == "UML-CON-001"
    assert f.evidence == {"not_ports": ["comp_store"]}


def test_con001_assembly_with_incompatible_interfaces():
    d = base()
    d["elements"].append({"id": "iface_other", "kind": "interface", "owner_id": "pkg"})
    el(d, "port_store")["attributes"]["provided"] = ["iface_other"]
    assert_only(d, "UML-CON-001")


def test_con001_delegation_needs_same_direction():
    d = base()
    rel(d, "conn1")["connector_kind"] = "delegation"
    # api requires, store provides: valid assembly, invalid delegation.
    assert_only(d, "UML-CON-001")


def test_con001_positive_delegation_and_conforming_assembly():
    d = base()
    rel(d, "conn1")["connector_kind"] = "delegation"
    el(d, "port_api")["attributes"] = {"provided": ["iface_repo"]}
    assert run(d).clean is True

    # Assembly where the provided interface specializes the required one.
    d = base()
    d["elements"].append({"id": "iface_sub", "kind": "interface", "owner_id": "pkg"})
    d["relationships"].append(
        {"id": "g_if", "kind": "generalization", "source": "iface_sub", "target": "iface_repo"}
    )
    el(d, "port_store")["attributes"]["provided"] = ["iface_sub"]
    assert run(d).clean is True


# ---------------------------------------------------------------------------
# UML-SEQ-001
# ---------------------------------------------------------------------------


def test_seq001_receive_before_send():
    d = base()
    occ = inter(d)["occurrences"]
    occ[0], occ[1] = occ[1], occ[0]  # o2 (receive of m1) now precedes o1 (its send)
    report = run(d)
    assert "UML-SEQ-001" in [f.rule_id for f in report.findings]
    f = next(f for f in report.findings if f.rule_id == "UML-SEQ-001")
    assert f.targets[0].kind == "interaction"
    assert f.path == "/messages/0"
    assert f.evidence == {"message_id": "m1", "send_index": 1, "receive_index": 0}


def test_seq001_execution_start_after_finish_or_off_lifeline():
    d = base()
    inter(d)["executions"][1].update({"start": "o5", "finish": "o4"})
    assert_only(d, "UML-SEQ-001")
    d = base()
    inter(d)["executions"][0]["finish"] = "o8"  # o8 is on ll_user
    assert_only(d, "UML-SEQ-001")


def test_seq001_positive_ordered_messages():
    assert run(base(), rules=["UML-SEQ-001"]).findings == []


# ---------------------------------------------------------------------------
# UML-SEQ-002
# ---------------------------------------------------------------------------


def test_seq002_reply_without_call():
    d = base()
    i = inter(d)
    i["occurrences"] += [{"id": "o11", "lifeline": "ll_repo"}, {"id": "o12", "lifeline": "ll_user"}]
    i["messages"].append(
        {"id": "m6", "kind": "reply", "send_occurrence": "o11", "receive_occurrence": "o12"}
    )
    assert_only(d, "UML-SEQ-002")


def test_seq002_reply_for_another_operation():
    d = base()
    msg(d, "m3")["operation_ref"] = "op_handle"
    assert "UML-SEQ-002" in rule_ids(d)


def test_seq002_reply_answers_an_asynchronous_call():
    d = base()
    msg(d, "m2")["kind"] = "asynch_call"
    assert_only(d, "UML-SEQ-002")


def test_seq002_reply_before_call_is_received():
    d = base()
    # Reply m4 from svc to user is sent at o7. Move the call's receive (o2) after it.
    occ = inter(d)["occurrences"]
    o2 = occ.pop(1)
    occ.insert(occ.index(next(o for o in occ if o["id"] == "o7")) + 1, o2)
    assert "UML-SEQ-002" in rule_ids(d)


def test_seq002_positive_nested_calls_and_replies():
    assert run(base(), rules=["UML-SEQ-002"]).findings == []


# ---------------------------------------------------------------------------
# UML-SEQ-003
# ---------------------------------------------------------------------------


def test_seq003_operation_not_owned_by_receiver_is_a_warning():
    d = base()
    msg(d, "m2")["operation_ref"] = "op_handle"  # receiver ll_repo is iface_repo
    msg(d, "m3")["operation_ref"] = None
    report = run(d)
    assert [f.rule_id for f in report.findings] == ["UML-SEQ-003"]
    f = report.findings[0]
    assert f.severity == "warning"
    assert f.path == "/messages/1/operation_ref"
    assert report.clean is True  # a warning never blocks clean


def test_seq003_positive_transitive_supertype():
    d = base()
    d["elements"].append({"id": "cls_root", "kind": "class", "owner_id": "pkg"})
    el(d, "cls_root")["attributes"] = {"operations": [{"id": "op_root", "name": "root"}]}
    d["relationships"].append(
        {"id": "g_root", "kind": "generalization", "source": "cls_base", "target": "cls_root"}
    )
    msg(d, "m5")["operation_ref"] = "op_root"  # svc -> base -> root
    assert run(d).findings == []


def test_seq003_negative_without_the_generalization():
    d = base()
    d["relationships"] = [r for r in d["relationships"] if r["id"] != "gen1"]
    diagram(d, "d_class")["connections"] = [
        c for c in diagram(d, "d_class")["connections"] if c["id"] != "c_gen"
    ]
    assert_only(d, "UML-SEQ-003")


def _retype_repo_lifeline(d: dict[str, Any], rep: str) -> None:
    ll = next(x for x in inter(d)["lifelines"] if x["id"] == "ll_repo")
    ll["represents_ref"] = rep


def test_seq003_positive_receiver_realizes_the_interface():
    # m2 calls op_save (owned by iface_repo); cls_repo_impl realizes iface_repo (ir1).
    d = base()
    _retype_repo_lifeline(d, "cls_repo_impl")
    assert run(d).findings == []


def test_seq003_negative_receiver_without_the_realization():
    d = base()
    _retype_repo_lifeline(d, "cls_repo_impl")
    d["relationships"] = [r for r in d["relationships"] if r["id"] != "ir1"]
    report = run(d)
    assert [f.rule_id for f in report.findings] == ["UML-SEQ-003"]
    assert report.findings[0].evidence["receiver_interfaces"] == []


def test_seq003_positive_component_provides_the_interface():
    d = base()
    d["elements"].append(
        {
            "id": "comp_app",
            "kind": "component",
            "name": "OrderApp",
            "owner_id": "pkg",
            "attributes": {"provided": ["iface_repo"]},
        }
    )
    _retype_repo_lifeline(d, "comp_app")
    assert run(d).findings == []


def test_seq003_positive_component_provides_through_its_port():
    # comp_store owns port_store, which provides iface_repo.
    d = base()
    _retype_repo_lifeline(d, "comp_store")
    assert run(d).findings == []


def test_seq003_positive_realization_inherited_through_generalization():
    d = base()
    d["elements"].append({"id": "cls_sub", "kind": "class", "name": "Sub", "owner_id": "pkg"})
    d["relationships"].append(
        {"id": "g_sub", "kind": "generalization", "source": "cls_sub", "target": "cls_repo_impl"}
    )
    _retype_repo_lifeline(d, "cls_sub")
    assert run(d).findings == []


def test_seq003_positive_operation_on_a_general_of_the_realized_interface():
    d = base()
    d["elements"].append(
        {
            "id": "iface_root",
            "kind": "interface",
            "name": "Root",
            "owner_id": "pkg",
            "attributes": {"operations": [{"id": "op_ping", "name": "ping"}]},
        }
    )
    d["relationships"].append(
        {"id": "g_iface", "kind": "generalization", "source": "iface_repo", "target": "iface_root"}
    )
    _retype_repo_lifeline(d, "cls_repo_impl")
    msg(d, "m2")["operation_ref"] = "op_ping"
    msg(d, "m3")["operation_ref"] = None
    assert run(d).findings == []


def test_seq003_negative_operation_on_an_unrelated_interface_is_still_flagged():
    d = base()
    d["elements"].append(
        {
            "id": "iface_other",
            "kind": "interface",
            "name": "Other",
            "owner_id": "pkg",
            "attributes": {"operations": [{"id": "op_other", "name": "other"}]},
        }
    )
    _retype_repo_lifeline(d, "cls_repo_impl")
    msg(d, "m2")["operation_ref"] = "op_other"
    msg(d, "m3")["operation_ref"] = None
    report = run(d)
    assert [f.rule_id for f in report.findings] == ["UML-SEQ-003"]
    ev = report.findings[0].evidence
    assert ev["operation_owner"] == "iface_other"
    assert ev["receiver_interfaces"] == ["iface_repo"]


def test_seq003_generalization_cycle_terminates():
    d = base()
    d["relationships"].append(
        {"id": "g_cyc", "kind": "generalization", "source": "cls_base", "target": "cls_svc"}
    )
    _retype_repo_lifeline(d, "cls_svc")
    report = run(d, rules=["UML-SEQ-003"])
    assert [f.rule_id for f in report.findings] == ["UML-SEQ-003"]


# ---------------------------------------------------------------------------
# UML-SEQ-004
# ---------------------------------------------------------------------------


def test_seq004_opt_without_guard():
    d = base()
    inter(d)["fragments"][0]["operands"][0]["guard"] = None
    report = run(d)
    (f,) = report.findings
    assert f.rule_id == "UML-SEQ-004"
    assert f.path == "/fragments/0/operands/0/guard"


def test_seq004_occurrence_on_uncovered_lifeline():
    d = base()
    inter(d)["fragments"][0]["covered"] = ["ll_svc"]
    assert_only(d, "UML-SEQ-004")


def test_seq004_loop_with_two_operands_and_shared_occurrence():
    d = base()
    inter(d)["fragments"][0].update(
        operator="loop",
        operands=[
            {"guard": "a", "occurrences": ["o3", "o4"]},
            {"guard": "b", "occurrences": ["o4", "o5"]},
        ],
    )
    findings = run(d).findings
    assert {f.rule_id for f in findings} == {"UML-SEQ-004"}
    assert len(findings) == 2


def test_seq004_empty_covered_and_no_operands():
    d = base()
    inter(d)["fragments"][0].update(covered=[], operands=[])
    assert_only(d, "UML-SEQ-004")


def test_seq004_positive_par_needs_no_guard():
    d = base()
    inter(d)["fragments"][0].update(
        operator="par",
        operands=[{"occurrences": ["o3", "o4"]}, {"occurrences": ["o5", "o6"]}],
    )
    assert run(d).clean is True


# ---------------------------------------------------------------------------
# UML-DIA-001 / UML-DIA-002
# ---------------------------------------------------------------------------


def test_dia001_object_shows_element_outside_model():
    d = base()
    diagram(d, "d_class2")["objects"][0]["element_ref"] = "elsewhere"
    report = run(d)
    (f,) = report.findings
    assert f.rule_id == "UML-DIA-001"
    assert [t.kind for t in f.targets] == ["diagram_object", "diagram"]


def test_dia001_object_with_both_or_no_reference():
    d = base()
    diagram(d, "d_class2")["objects"][0]["lifeline_id"] = "ll_svc"
    assert_only(d, "UML-DIA-001")
    d = base()
    diagram(d, "d_class2")["objects"][0]["element_ref"] = None
    assert_only(d, "UML-DIA-001")


def test_dia001_connection_shows_unknown_relationship_or_message():
    d = base()
    diagram(d, "d_class")["connections"][1]["relationship_ref"] = "ghost"
    assert_only(d, "UML-DIA-001")
    d = base()
    diagram(d, "d_seq")["connections"][0]["message_id"] = "ghost"
    assert_only(d, "UML-DIA-001")


def test_dia002_reversed_directed_relationship():
    d = base()
    c = diagram(d, "d_class")["connections"][1]
    c["source_object_id"], c["target_object_id"] = c["target_object_id"], c["source_object_id"]
    assert_only(d, "UML-DIA-002")


def test_dia002_message_drawn_between_wrong_lifelines():
    d = base()
    c = diagram(d, "d_seq")["connections"][0]
    c["source_object_id"], c["target_object_id"] = "ol_svc", "ol_user"
    report = run(d)
    (f,) = report.findings
    assert f.rule_id == "UML-DIA-002"
    assert ("interaction", "int1") in [(t.kind, t.id) for t in f.targets]


def test_dia002_connection_end_not_in_diagram():
    d = base()
    diagram(d, "d_class")["connections"][1]["target_object_id"] = "ob2_svc"
    assert_only(d, "UML-DIA-002")


def test_dia_positive_undirected_association_either_way():
    d = base()
    c = diagram(d, "d_class")["connections"][0]
    c["source_object_id"], c["target_object_id"] = "ob_svc", "ob_item"
    assert run(d).clean is True


# ---------------------------------------------------------------------------
# UML-UNSUP-001
# ---------------------------------------------------------------------------


def _mutations():
    def rel_kind(d):
        rel(d, "dep1")["kind"] = "include"

    def msg_kind(d):
        msg(d, "m5")["kind"] = "lost"

    def operator(d):
        inter(d)["fragments"][0]["operator"] = "seq"

    def diagram_kind(d):
        diagram(d, "d_class2")["kind"] = "activity"

    def aggregation(d):
        rel(d, "assoc1")["ends"][0]["aggregation"] = "weak"

    def three_ends(d):
        rel(d, "assoc1")["ends"].append({"element_ref": "dt_id"})

    def literals_on_class(d):
        el(d, "cls_item")["attributes"]["literals"] = ["x"]

    def lifeline_on_class_diagram(d):
        diagram(d, "d_class2")["objects"].append({"id": "ob2_ll", "lifeline_id": "ll_svc"})

    def lifeline_typed_by_package(d):
        inter(d)["lifelines"][0]["represents_ref"] = "pkg"

    def visibility(d):
        el(d, "cls_svc")["attributes"]["properties"][0]["visibility"] = "friend"

    def connector_kind_missing(d):
        rel(d, "conn1")["connector_kind"] = None

    def ends_on_dependency(d):
        rel(d, "dep1")["ends"] = []

    return [
        rel_kind,
        msg_kind,
        operator,
        diagram_kind,
        aggregation,
        three_ends,
        literals_on_class,
        lifeline_on_class_diagram,
        lifeline_typed_by_package,
        visibility,
        connector_kind_missing,
        ends_on_dependency,
    ]


@pytest.mark.parametrize("mutate", _mutations(), ids=lambda f: f.__name__)
def test_unsup001_constructs_outside_the_profile(mutate):
    d = base()
    mutate(d)
    report = run(d)
    assert "UML-UNSUP-001" in [f.rule_id for f in report.findings]
    assert report.clean is False


def test_unsup001_positive_every_profile_kind_is_supported():
    d = base()
    prof = get_profile()
    used = {e["kind"] for e in d["elements"]}
    assert used == set(prof.element_kinds)
    assert {r["kind"] for r in d["relationships"]} == set(prof.relationship_kinds)
    assert {x["kind"] for x in d["diagrams"]} == set(prof.diagram_kinds)
    assert run(d, rules=["UML-UNSUP-001"]).findings == []


def test_multiplicity_parser_rejects_non_ascii_digits_instead_of_crashing():
    """``str.isdigit`` accepts superscripts that ``int`` rejects — that was a 500."""
    from tools.uml.rules import parse_multiplicity

    assert parse_multiplicity("1..\u00b2") is None
    assert parse_multiplicity("\u0663") is None
    assert parse_multiplicity("0..1") == (0, 1)
    assert parse_multiplicity("1..*") == (1, None)
