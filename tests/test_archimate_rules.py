"""The ArchiMate registry and the pure validator (``tools.architecture.archimate_rules``).

Two kinds of evidence, deliberately kept apart (plan §12):

* ``ARCHI_FIXTURES`` are triples read by hand out of Archi 5.10.0's own
  ``relationships.xml`` (commit f757b06d…), NOT derived from the generated
  registry. A test that compared the registry with itself would prove only
  internal consistency; these prove the import kept Archi's meaning.
* Every rule gets a firing case and a clean case, so a rule that silently
  stops firing goes red.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from schemas.architecture import ALL_ELEMENT_TYPES, ArchitectureViewpoint
from schemas.modeling import ArchimateSnapshot
from tools.architecture.archimate_rules import (
    REQUIRED_RULES,
    RULES,
    WRITE_TIME_ONLY_RULES,
    UnknownRuleError,
    ruleset_info,
    validate,
)
from tools.architecture.registry import (
    ARCHI_VIEWPOINT_IDS,
    DATA_FILE,
    EXTENSION_VIEWPOINTS,
    get_registry,
)

REG = get_registry()

# (source, target, letters) copied from relationships.xml at release_5.10.0.
# Keys: a access, c composition, f flow, g aggregation, i assignment,
# n influence, o association, r realization, s specialization, t triggering,
# v serving.
ARCHI_FIXTURES = [
    ("application_component", "data_object", "ao"),
    ("business_actor", "business_role", "fiotv"),
    ("grouping", "relationship", "cgo"),
    ("junction", "junction", "acfginorstv"),
    ("location", "business_actor", "cfgiortv"),
    ("requirement", "goal", "nor"),
    ("business_actor", "relationship", "o"),
    ("data_object", "application_process", "o"),
    ("node", "artifact", "aio"),
]
KEY = {
    "a": "access",
    "c": "composition",
    "f": "flow",
    "g": "aggregation",
    "i": "assignment",
    "n": "influence",
    "o": "association",
    "r": "realization",
    "s": "specialization",
    "t": "triggering",
    "v": "serving",
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_census_matches_archi_5_10_0():
    assert REG.reference["commit"] == "f757b06d5f75b5565786bb0679c75450cc3a577f"
    assert REG.language_version == "3.2"
    assert len(REG.concepts) == 61
    assert len(REG.relationship_types) == 11
    # 61 concepts + the generic relationship endpoint, as both source and target.
    assert len(REG.matrix) == 62
    assert all(len(row) == 62 for row in REG.matrix.values())
    assert len(ARCHI_VIEWPOINT_IDS) == 25
    assert {"requirement", "grouping", "location", "junction"} <= set(REG.concepts)


@pytest.mark.parametrize(("source", "target", "letters"), ARCHI_FIXTURES)
def test_registry_matches_hand_read_archi_cells(source, target, letters):
    assert REG.allowed(source, target) == frozenset(KEY[c] for c in letters)


def test_license_notice_is_carried_with_the_data():
    raw = json.loads(DATA_FILE.read_text())
    assert "Permission is hereby granted" in raw["license_notice"]


def test_service_vocabularies_are_the_registry():
    assert ALL_ELEMENT_TYPES == frozenset(REG.concepts)
    assert set(ArchitectureViewpoint.__args__) == ARCHI_VIEWPOINT_IDS | set(EXTENSION_VIEWPOINTS)


def test_viewpoints_always_allow_junction_and_grouping():
    # Archi Viewpoint.defaultList.
    for vp in REG.viewpoints.values():
        assert {"junction", "grouping"} <= vp.allowed_concepts, vp.id


def test_viewpoint_macros_are_expanded():
    # application_cooperation is "$ApplicationElements$ + Location" in viewpoints.xml.
    vp = REG.viewpoints["application_cooperation"]
    assert REG.types_in_layer("application") | {"location"} <= vp.allowed_concepts
    assert not any(c.startswith("$") for v in REG.viewpoints.values() for c in v.allowed_concepts)


# ---------------------------------------------------------------------------
# Validator helpers
# ---------------------------------------------------------------------------


def snap(concepts=(), relationships=(), views=None) -> ArchimateSnapshot:
    return ArchimateSnapshot.model_validate(
        {"concepts": list(concepts), "relationships": list(relationships), "views": views}
    )


def c(id_, type_, name=None, **kw):
    return {"id": id_, "type": type_, "name": name if name is not None else id_, **kw}


def r(id_, type_, source, target, **kw):
    return {"id": id_, "type": type_, "source": source, "target": target, **kw}


def rule_ids(report) -> set[str]:
    return {f.rule_id for f in report.findings}


ACTOR, ROLE, PROC = (
    c("actor", "business_actor"),
    c("role", "business_role"),
    c("proc", "business_process"),
)
GOOD = [r("r1", "assignment", "actor", "role"), r("r2", "assignment", "role", "proc")]


def test_a_valid_model_without_views_is_clean_and_view_rules_are_not_enabled():
    report = validate(snap([ACTOR, ROLE, PROC], GOOD))
    assert report.clean, report.findings
    assert report.status == "complete"
    assert "AM-UNU-001" not in report.enabled_rules
    assert "AM-REL-001" in report.enabled_rules


def test_every_rule_has_unique_id_and_archi_default_severity():
    ids = [x.rule_id for x in RULES]
    assert len(ids) == len(set(ids))
    sev = {x.rule_id: x.severity for x in RULES}
    assert sev["AM-REL-001"] == sev["AM-JCT-001"] == "error"
    assert (
        sev["AM-VPT-001"]
        == sev["AM-UNU-001"]
        == sev["AM-UNU-002"]
        == sev["AM-DUP-001"]
        == "warning"
    )
    assert sev["AM-NST-001"] == sev["AM-EMV-001"] == "advice"
    assert ruleset_info().rule_count == len(RULES)


# One (snapshot, rule) per rule that must fire. Each case is otherwise valid.
FIRING = {
    "AM-REF-001": snap([ACTOR], [r("x", "association", "actor", "ghost")]),
    "AM-REF-002": snap([ACTOR, c("actor", "business_role")]),
    "AM-TYP-001": snap([c("x", "business_widget")]),
    "AM-TYP-002": snap([ACTOR, ROLE], [r("x", "toc_conflict", "actor", "role")]),
    "AM-REL-001": snap([c("d", "data_object"), PROC], [r("x", "flow", "d", "proc")]),
    "AM-REL-002": snap(
        [ACTOR, ROLE],
        [r("r1", "assignment", "actor", "role"), r("x", "association", "actor", "r1")],
    ),
    "AM-REL-003": snap([ACTOR, ROLE], [r("x", "assignment", "actor", "role", access_type="read")]),
    "AM-JCT-001": snap(
        [ACTOR, c("j", "junction"), PROC],
        [r("x", "assignment", "actor", "j"), r("y", "serving", "j", "proc")],
    ),
    "AM-JCT-002": snap(
        [c("d", "data_object"), c("j", "junction"), PROC],
        [r("x", "flow", "d", "j"), r("y", "flow", "j", "proc")],
    ),
    "AM-CYC-001": snap(
        [c("a", "business_actor", parent_id="b"), c("b", "business_actor", parent_id="a")]
    ),
    "AM-CYC-002": snap(
        [c("a", "business_actor"), c("b", "business_actor")],
        [r("x", "specialization", "a", "b"), r("y", "specialization", "b", "a")],
    ),
    "AM-VIEW-001": snap(
        [ACTOR],
        views=[{"id": "v", "viewpoint": "nope", "objects": [{"id": "o", "concept_id": "actor"}]}],
    ),
    "AM-VIEW-002": snap(
        [ACTOR, ROLE],
        [r("r1", "assignment", "actor", "role")],
        views=[
            {
                "id": "v",
                "objects": [
                    {"id": "oa", "concept_id": "actor"},
                    {"id": "or", "concept_id": "role"},
                ],
                "connections": [
                    {
                        "id": "cn",
                        "relationship_id": "r1",
                        "source_object_id": "or",
                        "target_object_id": "oa",
                    }
                ],
            }
        ],
    ),
    "AM-VIEW-003": snap(
        [ACTOR],
        views=[
            {"id": "v", "objects": [{"id": "o", "concept_id": "actor", "parent_object_id": "zz"}]}
        ],
    ),
    "AM-VPT-001": snap(
        [c("n", "node")],
        views=[
            {"id": "v", "viewpoint": "organization", "objects": [{"id": "o", "concept_id": "n"}]}
        ],
    ),
    "AM-NST-001": snap(
        [ACTOR, ROLE],
        views=[
            {
                "id": "v",
                "objects": [
                    {"id": "oa", "concept_id": "actor"},
                    {"id": "or", "concept_id": "role", "parent_object_id": "oa"},
                ],
            }
        ],
    ),
    "AM-UNU-001": snap([ACTOR], views=[]),
    "AM-UNU-002": snap([ACTOR, ROLE], [GOOD[0]], views=[]),
    "AM-EMV-001": snap([], views=[{"id": "v", "objects": []}]),
    "AM-DUP-001": snap([c("a", "business_actor", "Same"), c("b", "business_actor", "Same")]),
}


def test_every_rule_has_a_firing_case():
    # Write-time-only rules fire at the write gate (tests/test_modeling_lifecycle.py).
    assert set(FIRING) | WRITE_TIME_ONLY_RULES == {x.rule_id for x in RULES}
    assert not set(FIRING) & WRITE_TIME_ONLY_RULES


@pytest.mark.parametrize("rule_id", sorted(FIRING))
def test_rule_fires(rule_id):
    report = validate(FIRING[rule_id])
    assert rule_id in rule_ids(report), [f.model_dump() for f in report.findings]


@pytest.mark.parametrize("rule_id", sorted(FIRING))
def test_disabling_a_rule_silences_it(rule_id):
    report = validate(FIRING[rule_id], disable=[rule_id])
    assert rule_id not in rule_ids(report)
    assert rule_id not in report.enabled_rules
    if rule_id in REQUIRED_RULES:
        assert report.status == "incomplete"
        assert report.clean is False


def test_a_fully_viewed_model_is_clean():
    report = validate(
        snap(
            [ACTOR, ROLE],
            [GOOD[0]],
            views=[
                {
                    "id": "v",
                    "viewpoint": "organization",
                    "objects": [
                        {"id": "oa", "concept_id": "actor"},
                        {"id": "or", "concept_id": "role"},
                    ],
                    "connections": [
                        {
                            "id": "cn",
                            "relationship_id": "r1",
                            "source_object_id": "oa",
                            "target_object_id": "or",
                        }
                    ],
                }
            ],
        )
    )
    assert report.findings == []
    assert report.clean


def test_nesting_backed_by_a_structural_relationship_is_not_advised():
    report = validate(
        snap(
            [ACTOR, ROLE],
            [GOOD[0]],
            views=[
                {
                    "id": "v",
                    "objects": [
                        {"id": "oa", "concept_id": "actor"},
                        {"id": "or", "concept_id": "role", "parent_object_id": "oa"},
                    ],
                }
            ],
        )
    )
    assert "AM-NST-001" not in rule_ids(report)


def test_a_relationship_can_end_on_a_relationship_where_archi_allows_it():
    # Grouping --aggregation--> (relationship): "cgo" in the matrix.
    report = validate(
        snap(
            [ACTOR, ROLE, c("g", "grouping")],
            [GOOD[0], r("x", "aggregation", "g", "r1")],
        )
    )
    assert report.clean, report.findings


def test_junction_with_one_type_and_valid_pairs_is_clean():
    report = validate(
        snap(
            [ACTOR, c("actor2", "business_actor"), c("j", "junction", junction_type="or"), ROLE],
            [
                r("x", "assignment", "actor", "j"),
                r("y", "assignment", "actor2", "j"),
                r("z", "assignment", "j", "role"),
            ],
        )
    )
    assert report.clean, report.findings


def test_grouping_aggregation_into_a_junction_is_exempt_from_homogeneity():
    """ArchiMate 3.2 Appendix B (and Archi's ArchimateModelUtils) exempt a
    Grouping/Location aggregation or composition into a junction from the
    one-type rule. ⚠️ Archi's Hammer ``JunctionsChecker`` does NOT exempt it —
    Archi disagrees with itself at 5.10.0. This service follows the model
    utility and the specification; if a future Archi aligns Hammer, revisit."""
    report = validate(
        snap(
            [c("g", "grouping"), ACTOR, c("j", "junction"), ROLE],
            [
                r("x", "aggregation", "g", "j"),
                r("y", "assignment", "actor", "j"),
                r("z", "assignment", "j", "role"),
            ],
        )
    )
    assert "AM-JCT-001" not in rule_ids(report)


def test_specialization_between_different_types_is_invalid():
    report = validate(snap([ACTOR, ROLE], [r("x", "specialization", "actor", "role")]))
    assert "AM-REL-001" in rule_ids(report)


def test_duplicate_junctions_are_not_duplicates():
    report = validate(snap([c("j1", "junction", "J"), c("j2", "junction", "J")]))
    assert "AM-DUP-001" not in rule_ids(report)


def test_duplicate_names_are_matched_case_and_whitespace_insensitively():
    """A12: same-name concepts are legal; the checker reports them as WARNINGS only."""
    report = validate(
        snap(
            [c("a", "business_actor", "Order  Service"), c("b", "business_actor", " order service")]
        )
    )
    dups = [f for f in report.findings if f.rule_id == "AM-DUP-001"]
    assert len(dups) == 2, report.findings
    assert {f.severity for f in dups} == {"warning"}
    evidence = [f.evidence for f in dups]
    assert all(e is not None for e in evidence), dups
    assert {e["duplicates"][0] for e in evidence if e is not None} == {"a", "b"}
    assert not [f for f in report.findings if f.severity == "error"]


def test_same_name_different_type_is_not_a_duplicate():
    report = validate(snap([c("a", "business_actor", "Same"), c("b", "business_role", "Same")]))
    assert "AM-DUP-001" not in rule_ids(report)


def test_unnamed_concepts_are_not_duplicates():
    report = validate(snap([c("a", "business_actor", ""), c("b", "business_actor", "  ")]))
    assert "AM-DUP-001" not in rule_ids(report)


def test_distinct_names_are_not_duplicates():
    report = validate(snap([c("a", "business_actor", "Clerk"), c("b", "business_actor", "Clerks")]))
    assert "AM-DUP-001" not in rule_ids(report)


def test_unknown_rule_id_is_an_error():
    with pytest.raises(UnknownRuleError):
        validate(snap(), rules=["AM-NOPE-999"])


def test_output_is_deterministic():
    case = FIRING["AM-JCT-001"]
    assert validate(case).model_dump() == validate(case).model_dump()


def test_findings_are_ordered_errors_first():
    report = validate(
        snap(
            [
                c("x", "business_widget"),
                c("a", "business_actor", "S"),
                c("b", "business_actor", "S"),
            ]
        )
    )
    severities = [f.severity for f in report.findings]
    assert severities == sorted(severities, key=["error", "warning", "advice"].index)


# --- Visual-only diagram objects: notes, groups, plain lines --------------------------------


def _visual_view(objects, connections=()):
    return [
        {
            "id": "v",
            "viewpoint": "organization",
            "objects": objects,
            "connections": list(connections),
        }
    ]


def test_notes_groups_and_lines_are_not_concepts_and_keep_a_model_clean():
    report = validate(
        snap(
            [ACTOR, ROLE],
            [GOOD[0]],
            views=_visual_view(
                [
                    {"id": "grp", "kind": "group", "name": "Team"},
                    {"id": "oa", "concept_id": "actor", "parent_object_id": "grp"},
                    {"id": "or", "concept_id": "role", "parent_object_id": "grp"},
                    {
                        "id": "note",
                        "kind": "note",
                        "text": "explains things",
                        "border_type": "dogear",
                    },
                    {"id": "n2", "kind": "note", "text": "inside", "parent_object_id": "oa"},
                ],
                [
                    {
                        "id": "cn",
                        "relationship_id": "r1",
                        "source_object_id": "oa",
                        "target_object_id": "or",
                    },
                    {
                        "id": "l1",
                        "kind": "line",
                        "source_object_id": "note",
                        "target_object_id": "oa",
                    },
                    {
                        "id": "l2",
                        "kind": "line",
                        "source_object_id": "note",
                        "target_object_id": "cn",
                    },
                    {
                        "id": "l3",
                        "kind": "line",
                        "source_object_id": "grp",
                        "target_object_id": "note",
                        "label": "see",
                    },
                ],
            ),
        )
    )
    assert report.findings == [], report.findings
    assert report.clean
    assert report.examined["element"] == 2


def test_nesting_an_element_in_a_group_needs_no_relationship():
    report = validate(
        snap(
            [ACTOR],
            views=_visual_view(
                [
                    {"id": "grp", "kind": "group", "name": "G"},
                    {"id": "oa", "concept_id": "actor", "parent_object_id": "grp"},
                ]
            ),
        )
    )
    assert "AM-NST-001" not in rule_ids(report)
    assert report.clean, report.findings


def test_a_line_does_not_mark_anything_used_and_is_not_checked_against_relationships():
    report = validate(
        snap(
            [ACTOR, ROLE],
            [GOOD[0]],
            views=_visual_view(
                [{"id": "oa", "concept_id": "actor"}, {"id": "or", "concept_id": "role"}],
                [{"id": "l", "kind": "line", "source_object_id": "or", "target_object_id": "oa"}],
            ),
        )
    )
    ids = rule_ids(report)
    assert "AM-VIEW-002" not in ids
    assert "AM-UNU-002" in ids  # r1 is still not shown by any relationship connection


def test_a_line_with_a_dangling_end_is_an_unresolved_reference():
    report = validate(
        snap(
            [ACTOR],
            views=_visual_view(
                [{"id": "oa", "concept_id": "actor"}],
                [
                    {
                        "id": "l",
                        "kind": "line",
                        "source_object_id": "oa",
                        "target_object_id": "ghost",
                    }
                ],
            ),
        )
    )
    assert any(f.rule_id == "AM-REF-001" and f.targets[0].id == "l" for f in report.findings)


def test_a_relationship_connection_attached_to_a_note_is_view_002():
    report = validate(
        snap(
            [ACTOR, ROLE],
            [GOOD[0]],
            views=_visual_view(
                [{"id": "oa", "concept_id": "actor"}, {"id": "n", "kind": "note", "text": "x"}],
                [
                    {
                        "id": "cn",
                        "relationship_id": "r1",
                        "source_object_id": "oa",
                        "target_object_id": "n",
                    }
                ],
            ),
        )
    )
    assert "AM-VIEW-002" in rule_ids(report)


def test_note_or_group_with_a_missing_visual_parent_is_view_003():
    report = validate(
        snap(
            views=_visual_view(
                [{"id": "n", "kind": "note", "text": "x", "parent_object_id": "nope"}]
            )
        )
    )
    assert "AM-VIEW-003" in rule_ids(report)


@pytest.mark.parametrize(
    "obj",
    [
        {"id": "o", "kind": "element"},
        {"id": "o", "kind": "note", "concept_id": "actor"},
        {"id": "o", "kind": "group", "concept_id": "actor"},
        {"id": "o", "kind": "sticker", "concept_id": "actor"},
    ],
)
def test_view_object_concept_id_is_required_iff_element(obj):
    with pytest.raises(ValidationError):
        snap([ACTOR], views=_visual_view([obj]))


@pytest.mark.parametrize(
    "conn",
    [
        {"id": "x", "source_object_id": "a", "target_object_id": "b"},
        {
            "id": "x",
            "kind": "line",
            "relationship_id": "r1",
            "source_object_id": "a",
            "target_object_id": "b",
        },
    ],
)
def test_connection_relationship_id_is_required_iff_relationship(conn):
    with pytest.raises(ValidationError):
        snap(views=_visual_view([], [conn]))
