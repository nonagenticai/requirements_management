"""Guardrail tests for the OpenAPI surface.

Asserts that every route has an explicit ``operation_id`` matching
``EXPECTED_OPERATION_IDS`` below and that the OpenAPI schema exposes the standard
metadata (security scheme, tag groups, error-envelope component).

If a new route is added or renamed, update the ``EXPECTED_OPERATION_IDS`` set
below in the same change.
"""

from __future__ import annotations

import pytest

from main import app

# Source of truth for operation IDs. Sorted alphabetically.
EXPECTED_OPERATION_IDS: frozenset[str] = frozenset(
    {
        "analyze_architecture_conformance",
        "analyze_architecture_gaps",
        "analyze_architecture_impact",
        "append_card_ledger",
        "batch_delete_architecture_elements",
        "build_capability_heatmap",
        "build_traceability_matrix",
        "bulk_delete_all",
        "bulk_delete_decisions",
        "bulk_delete_principles",
        "bulk_delete_requirements",
        "calculate_requirement_priority",
        "check_adm_completeness",
        "check_standards_compliance",
        "compare_architecture_element_versions",
        "compare_architecture_standard_versions",
        "compare_architecture_view_versions",
        "compare_decision_versions",
        "compare_principle_versions",
        "compare_requirement_versions",
        "create_architecture_element",
        "create_architecture_element_binding",
        "create_architecture_relationship",
        "create_architecture_standard",
        "create_architecture_view",
        "create_benefit",
        "create_card_type",
        "create_decision",
        "create_pm_board_template",
        "create_pm_capability",
        "create_pm_deliverable_kind",
        "create_pm_gate",
        "create_pm_milestone_kind",
        "create_pm_priority_level",
        "create_pm_recipe",
        "create_pm_wip_limit",
        "create_principle",
        "create_programme",
        "create_project",
        "create_project_brief_version",
        "create_project_phase",
        "create_project_type",
        "create_requirement",
        "create_resource",
        "create_risk",
        "delete_architecture_element",
        "delete_architecture_element_binding",
        "delete_architecture_relationship",
        "delete_architecture_standard",
        "delete_architecture_view",
        "delete_benefit",
        "delete_card_type",
        "delete_decision",
        "delete_pm_capability",
        "delete_pm_recipe",
        "delete_principle",
        "delete_programme",
        "delete_project",
        "delete_project_phase",
        "delete_project_type",
        "delete_requirement",
        "delete_resource",
        "delete_risk",
        "derive_architecture_relationships",
        "generate_architecture_view",
        "generate_workpackage_proposals",
        "get_architecture_container",
        "get_architecture_element",
        "get_architecture_element_quality",
        "get_architecture_element_quality_history",
        "get_architecture_element_version_info",
        "get_architecture_relationship",
        "get_architecture_standard",
        "get_architecture_standard_version_info",
        "get_architecture_view",
        "get_architecture_view_quality",
        "get_architecture_view_quality_history",
        "get_architecture_view_version_info",
        "get_benefit",
        "get_benefit_quality",
        "get_card_type",
        "get_decision",
        "get_decision_quality",
        "get_decision_quality_history",
        "get_decision_version",
        "get_entity_graph",
        "get_pm_capability",
        "get_pm_recipe",
        "get_principle",
        "get_principle_quality",
        "get_principle_quality_history",
        "get_principle_version",
        "get_programme",
        "get_project",
        "get_project_brief_version",
        "get_project_by_planka_board",
        "get_project_capabilities",
        "get_project_phase",
        "get_project_registers",
        "get_project_type",
        "get_project_type_card_types",
        "get_project_wip_limit",
        "get_requirement",
        "get_requirement_quality",
        "get_requirement_version",
        "get_resource",
        "get_risk",
        "get_risk_quality",
        "get_risk_quality_history",
        "health_check",
        "import_pm_catalogue_defaults",
        "link_project_planka_board",
        "list_card_ledger",
        "list_applicable_standards_for_element",
        "list_architecture_containers",
        "list_architecture_element_ancestors",
        "list_architecture_element_bindings",
        "list_architecture_element_children",
        "list_architecture_element_versions",
        "list_architecture_elements",
        "list_architecture_relationships",
        "list_architecture_standard_versions",
        "list_architecture_standards",
        "list_architecture_view_versions",
        "list_architecture_views",
        "list_benefits",
        "list_card_types",
        "list_decision_versions",
        "list_decisions",
        "list_pm_capabilities",
        "list_pm_recipes",
        "list_principle_conflicts",
        "list_principle_versions",
        "list_principles",
        "list_programmes",
        "list_project_brief_versions",
        "list_project_capability_events",
        "list_project_coach_actions",
        "list_project_phase_events",
        "list_project_phases",
        "list_project_type_recipes",
        "list_project_types",
        "list_projects",
        "list_requirement_versions",
        "list_requirements",
        "list_resources",
        "list_risks",
        "liveness_check",
        "patch_decision",
        "patch_principle",
        "patch_requirement",
        # G8 / R313: project copy-on-write + promotion of the governed assets.
        "project_copy_decision",
        "project_copy_principle",
        "project_copy_requirement",
        "promote_decision",
        "promote_principle",
        "promote_requirement",
        "readiness_check",
        "record_project_coach_action",
        "record_project_coach_action_applied",
        "search_all_entities",
        "search_decisions",
        "search_knowledge_base",
        "search_principles",
        "search_requirements",
        "set_project_capabilities",
        "set_project_criterion_check",
        "set_project_phase",
        "set_project_type_card_types",
        "trace_requirement_architecture",
        "update_architecture_element",
        "update_architecture_relationship",
        "update_architecture_standard",
        "update_architecture_view",
        "update_benefit",
        "update_card_type",
        "update_decision",
        "update_pm_capability",
        "update_pm_recipe",
        "update_principle",
        "update_principle_conflict",
        "update_programme",
        "update_project",
        "update_project_phase",
        "update_project_type",
        "update_requirement",
        "update_resource",
        "update_risk",
        "validate_architecture_relationship",
        # Logical Thinking Process (TOC)
        "get_toc_config",
        "upsert_toc_config",
        "create_toc_tree",
        "list_toc_trees",
        "get_toc_tree",
        "update_toc_tree",
        "delete_toc_tree",
        "get_toc_graph",
        "get_toc_tree_quality",
        "create_toc_node",
        "list_toc_nodes",
        "update_toc_node",
        "delete_toc_node",
        "create_toc_edge",
        "delete_pm_board_template",
        "delete_pm_deliverable_kind",
        "delete_pm_gate",
        "delete_pm_milestone_kind",
        "delete_pm_priority_level",
        "delete_pm_wip_limit",
        "list_toc_edges",
        "delete_toc_edge",
        "get_pm_board_template",
        "get_pm_deliverable_kind",
        "get_pm_gate",
        "get_pm_milestone_kind",
        "get_pm_priority_level",
        "get_pm_wip_limit",
        "list_pm_board_templates",
        "list_pm_deliverable_kinds",
        "list_pm_gates",
        "list_pm_milestone_kinds",
        "list_pm_priority_levels",
        "list_pm_wip_limits",
        "resolve_pm_wip_limit",
        "update_pm_board_template",
        "update_pm_deliverable_kind",
        "update_pm_gate",
        "update_pm_milestone_kind",
        "update_pm_priority_level",
        "update_pm_wip_limit",
        # ArchiMate / UML reference data and stateless validation (2026-10-26).
        "get_archimate_viewpoint",
        "get_ruleset",
        "get_uml_profile",
        "list_archimate_concepts",
        "list_archimate_viewpoints",
        "list_rulesets",
        "query_archimate_relationship_matrix",
        "validate_archimate_fragment",
        "validate_uml_fragment",
        # UML storage + rendering (2026-10-27, target spec §8.2, plan Phase 5).
        "create_uml_diagram",
        "create_uml_diagram_connection",
        "create_uml_diagram_object",
        "create_uml_element",
        "create_uml_interaction",
        "create_uml_relationship",
        "delete_uml_diagram",
        "delete_uml_diagram_connection",
        "delete_uml_diagram_object",
        "delete_uml_element",
        "delete_uml_interaction",
        "delete_uml_relationship",
        "get_uml_diagram",
        "get_uml_element",
        "get_uml_interaction",
        "get_uml_relationship",
        "list_uml_diagram_connections",
        "list_uml_diagram_objects",
        "list_uml_diagrams",
        "list_uml_elements",
        "list_uml_interactions",
        "list_uml_relationships",
        "render_model_revision_diagram",
        "render_uml_diagram",
        "update_uml_diagram",
        "update_uml_diagram_connection",
        "update_uml_diagram_object",
        "update_uml_element",
        "update_uml_interaction",
        "update_uml_relationship",
        # Modeling lifecycle part 1 (2026-10-27, target spec §7.1-7.2, §8.1).
        "create_architecture_view_connection",
        "create_architecture_view_object",
        "create_model",
        "delete_architecture_view_connection",
        "delete_architecture_view_object",
        "delete_model",
        "get_model",
        "get_model_revision",
        "list_architecture_view_connections",
        "list_architecture_view_objects",
        "list_model_revisions",
        "list_models",
        "patch_model",
        "seal_model_revision",
        "update_architecture_view_connection",
        "update_architecture_view_object",
        # Whole-model exports: PDF / SVG zip of every view or diagram, UML XMI (2026-10-30).
        "export_model_diagrams",
        "export_model_revision_diagrams",
        "export_model_revision_xmi",
        "export_model_xmi",
        # Modeling lifecycle part 2 (2026-10-27, spec §7.3-7.4, §9.5; plan §7.3).
        "export_model_exchange",
        "get_model_organizations",
        "replace_model_organizations",
        "export_model_revision_exchange",
        "import_exchange_as_new_model",
        "import_model_exchange",
        "render_architecture_view",
        "render_model_revision_view",
        "apply_change_set",
        "create_change_set",
        "create_trace_link",
        "delete_trace_link",
        "get_change_set",
        "get_trace_link",
        "get_validation_run",
        "list_change_sets",
        "list_model_publications",
        "list_model_revision_reviews",
        "list_model_validation_runs",
        "list_trace_links",
        "list_validation_findings",
        "publish_model_revision",
        "reject_change_set",
        "review_model_revision",
        "run_model_validation",
    }
)


@pytest.fixture(scope="module")
def openapi_schema() -> dict:
    """Build the OpenAPI schema once per module."""
    # custom_openapi() caches on app.openapi_schema; force a fresh build so
    # this test reflects current source even if another test built it first.
    app.openapi_schema = None
    return app.openapi()


def test_every_route_has_explicit_operation_id(openapi_schema: dict) -> None:
    """No path/method should rely on FastAPI's auto-generated operationId.

    The auto-generated format is ``<function>_<path>_<method>`` and looks
    awful in n8n-generated clients. Explicit IDs are required.
    """
    auto_generated_pattern_suffixes = ("_post", "_get", "_put", "_delete", "_patch")
    offenders: list[tuple[str, str, str]] = []
    for path, item in openapi_schema.get("paths", {}).items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            op_id: str = op.get("operationId", "")
            if (
                op_id.endswith(auto_generated_pattern_suffixes)
                and op_id not in EXPECTED_OPERATION_IDS
            ):
                offenders.append((method.upper(), path, op_id))
    assert not offenders, f"Routes without explicit operation_id (auto-generated): {offenders}"


def test_operation_ids_match_plan(openapi_schema: dict) -> None:
    """The set of operationIds in the schema must equal the plan."""
    actual: set[str] = set()
    for item in openapi_schema.get("paths", {}).values():
        for op in item.values():
            if isinstance(op, dict) and op.get("operationId"):
                actual.add(op["operationId"])

    missing = EXPECTED_OPERATION_IDS - actual
    unexpected = actual - EXPECTED_OPERATION_IDS
    assert not missing and not unexpected, (
        f"operationId drift — missing: {sorted(missing)}, unexpected: {sorted(unexpected)}"
    )


def test_operation_ids_are_unique(openapi_schema: dict) -> None:
    """Every operationId must be unique across the whole schema."""
    seen: dict[str, tuple[str, str]] = {}
    duplicates: list[tuple[str, tuple[str, str], tuple[str, str]]] = []
    for path, item in openapi_schema.get("paths", {}).items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId")
            if not op_id:
                continue
            if op_id in seen:
                duplicates.append((op_id, seen[op_id], (method.upper(), path)))
            else:
                seen[op_id] = (method.upper(), path)
    assert not duplicates, f"Duplicate operationIds: {duplicates}"


def test_bearer_auth_scheme_is_declared(openapi_schema: dict) -> None:
    """BearerAuth security scheme must be present (license-token transport)."""
    schemes = openapi_schema.get("components", {}).get("securitySchemes", {})
    assert "BearerAuth" in schemes, "BearerAuth scheme missing from OpenAPI"
    scheme = schemes["BearerAuth"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"


def test_tenant_id_header_parameter_is_declared(openapi_schema: dict) -> None:
    """The reusable TenantIdHeader parameter must be in components.parameters."""
    parameters = openapi_schema.get("components", {}).get("parameters", {})
    assert "TenantIdHeader" in parameters, "TenantIdHeader parameter missing"
    param = parameters["TenantIdHeader"]
    assert param["in"] == "header"
    assert param["name"].lower() == "x-tenant-id"


def test_tag_groups_are_defined(openapi_schema: dict) -> None:
    """Every tag used on a route must be declared in ``openapi_tags`` — and vice versa.

    This is a commercial API: undeclared tags render in /docs and /openapi.json
    with no description and in arbitrary order. Asserting the declared set
    equals the used set keeps the published tag catalog complete and prevents
    silent drift as routers are added or renamed.
    """
    used_tags: set[str] = set()
    for item in openapi_schema.get("paths", {}).values():
        for op in item.values():
            if isinstance(op, dict):
                used_tags.update(op.get("tags", []))

    declared_tags = {t["name"] for t in openapi_schema.get("tags", [])}

    undeclared = used_tags - declared_tags
    dead = declared_tags - used_tags
    assert not undeclared and not dead, (
        "openapi_tags drift — "
        f"used but not declared: {sorted(undeclared)}; "
        f"declared but unused: {sorted(dead)}"
    )
    # Every declared tag must carry a non-empty description (the whole point).
    missing_desc = [
        t["name"] for t in openapi_schema.get("tags", []) if not t.get("description", "").strip()
    ]
    assert not missing_desc, f"Declared tags missing a description: {sorted(missing_desc)}"


def test_protected_routes_have_security_requirement(openapi_schema: dict) -> None:
    """Every /requirements/* operation must require BearerAuth."""
    protected_paths = [p for p in openapi_schema.get("paths", {}) if p.startswith("/requirements")]
    assert protected_paths, "No /requirements/* paths found in schema"
    for path in protected_paths:
        for method, op in openapi_schema["paths"][path].items():
            if not isinstance(op, dict):
                continue
            security = op.get("security", [])
            assert any("BearerAuth" in entry for entry in security), (
                f"{method.upper()} {path} is missing BearerAuth security requirement"
            )


def test_public_routes_have_no_security_requirement(openapi_schema: dict) -> None:
    """Health paths must not require auth in the schema."""
    public_paths = {"/health", "/health/live", "/health/ready"}
    assert public_paths <= set(openapi_schema.get("paths", {}))  # absent paths are skipped below
    for path in public_paths:
        item = openapi_schema.get("paths", {}).get(path)
        if item is None:
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            security = op.get("security", [])
            assert not security, (
                f"Public route {method.upper()} {path} unexpectedly has security={security}"
            )


# --------------------------------------------------------------------------- #
# The published filter surface of the governance list routes.
#
# These params are what a generated client (n8n custom nodes, fr's AG-UI list
# actions) is built against, so the schema IS the contract. Until 2026-09-04 the
# contract and the behaviour disagreed silently: fr sent ``project_id`` /
# ``origin`` / ``bound_to`` / ``visible_in`` to routes that declared none of
# them, FastAPI discarded them without a word, and the caller received the whole
# tenant with a 200 believing it was scoped.
#
# This pins the DECLARED half only — cheap, no database. The behavioural half
# ("a declared param must change the result for some input") is
# ``tests/test_list_filters_are_declared_and_applied.py``. Neither is sufficient
# alone: a declared param that narrows nothing passes here, and a param applied
# but never published is invisible to the client generator.
# --------------------------------------------------------------------------- #
EXPECTED_LIST_QUERY_PARAMS: dict[str, frozenset[str]] = {
    # ``scope_project_id`` / ``include_shadowed``: the G8 project view (tests/test_g8_*).
    "/requirements": frozenset(
        {"project_id", "origin_kind", "limit", "offset", "scope_project_id", "include_shadowed"}
    ),
    "/principles": frozenset(
        {
            "active_only",
            "origin_kind",
            "project_id",
            "limit",
            "offset",
            "scope_project_id",
            "include_shadowed",
        }
    ),
    "/decisions": frozenset(
        {
            "project_id",
            "status",
            "origin_kind",
            "limit",
            "offset",
            "scope_project_id",
            "include_shadowed",
        }
    ),
}

# ⛔ fr-local, and deliberately absent from every route above. ``bound_to`` and
# ``visible_in`` are resolved against fr's own ``asset_bindings`` table, which
# this service does not have; ``card_id`` exists on no model in this estate.
# Declaring one here would publish a filter with no column behind it — the same
# defect in the other direction. Do not "complete the set".
PARAMS_THAT_BELONG_TO_FR = ("card_id", "bound_to", "visible_in")


def _query_param_names(openapi_schema: dict, path: str) -> set[str]:
    item = openapi_schema["paths"].get(path) or openapi_schema["paths"].get(path + "/")
    assert item is not None, f"GET {path} is absent from the OpenAPI schema"
    return {p["name"] for p in item["get"].get("parameters", []) if p.get("in") == "query"}


@pytest.mark.parametrize("path", sorted(EXPECTED_LIST_QUERY_PARAMS))
def test_list_routes_publish_exactly_their_filter_surface(openapi_schema: dict, path: str) -> None:
    """Equality, not containment — an extra published param is drift too."""
    actual = _query_param_names(openapi_schema, path)
    expected = EXPECTED_LIST_QUERY_PARAMS[path]
    assert actual == expected, (
        f"GET {path} query-param drift — missing: {sorted(expected - actual)}, "
        f"unexpected: {sorted(actual - expected)}. If the addition is intended, "
        "add a probe in tests/test_list_filters_are_declared_and_applied.py "
        "proving it actually changes the result, then update this set."
    )


@pytest.mark.parametrize("path", sorted(EXPECTED_LIST_QUERY_PARAMS))
@pytest.mark.parametrize("name", PARAMS_THAT_BELONG_TO_FR)
def test_fr_local_params_are_not_published_here(openapi_schema: dict, path: str, name: str) -> None:
    assert name not in _query_param_names(openapi_schema, path), (
        f"GET {path} publishes {name!r}, which no column in this service can "
        "satisfy — it belongs to fr's asset_bindings table"
    )


@pytest.mark.parametrize("path", sorted(EXPECTED_LIST_QUERY_PARAMS))
def test_origin_kind_publishes_its_closed_domain(openapi_schema: dict, path: str) -> None:
    """The pattern is part of the contract, not decoration.

    ``origin_kind``'s domain is closed by the DB CHECK ``ck_*_origin_kind``
    (``tenant``/``project``/``workflow``). Publishing the pattern is what makes a
    generated client reject a value that could never match a row, instead of
    sending it and reading the resulting empty list as "no such rows".
    """
    item = openapi_schema["paths"].get(path) or openapi_schema["paths"].get(path + "/")
    param = next(p for p in item["get"]["parameters"] if p.get("name") == "origin_kind")
    rendered = str(param.get("schema", {}))
    assert "tenant|project|workflow" in rendered, (
        f"GET {path} publishes origin_kind without its closed domain: {rendered}"
    )
