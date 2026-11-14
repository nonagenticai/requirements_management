"""Ratchet: every 2xx ``application/json`` response documents a real schema.

fr generates its RM client from ``/openapi.json`` (orval). A 2xx body whose schema is
loose — ``{}``, ``Any``, or an object with no ``properties`` and open
``additionalProperties`` — comes out as ``unknown`` in that client, so a consumer cannot
see what the route returns. This test fails when a NEW loose response appears, and also
when an allowlisted operation becomes typed (so the list only ever shrinks).

``LOOSE_ALLOWLIST`` is the set of operations that were loose when the ratchet landed
(after the 25 operations fr consumes were typed). Type one, then delete its line.
"""

from __future__ import annotations

from typing import Any

from main import app

LOOSE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "analyze_architecture_gaps",
        "analyze_architecture_impact",
        "batch_delete_architecture_elements",
        "build_capability_heatmap",
        "build_traceability_matrix",
        "bulk_delete_all",
        "bulk_delete_decisions",
        "bulk_delete_principles",
        "bulk_delete_requirements",
        "check_adm_completeness",
        "check_standards_compliance",
        "delete_architecture_element_binding",
        "delete_benefit",
        "delete_card_type",
        "delete_pm_board_template",
        "delete_pm_capability",
        "delete_pm_deliverable_kind",
        "delete_pm_gate",
        "delete_pm_milestone_kind",
        "delete_pm_priority_level",
        "delete_pm_recipe",
        "delete_pm_wip_limit",
        "delete_project",
        "delete_project_phase",
        "delete_project_type",
        "delete_resource",
        "generate_architecture_view",
        "generate_workpackage_proposals",
        "health_check",
        "list_architecture_containers",
        "liveness_check",
        "readiness_check",
        "trace_requirement_architecture",
    }
)

# The 24 operations fr consumes that this ratchet was introduced to type (25 until R352 dropped
# delete_activity). Listed so a
# regression on one of them names the operation, not just "a new loose response".
FR_CONSUMED_TYPED: frozenset[str] = frozenset(
    {
        "analyze_architecture_conformance",
        "delete_architecture_element",
        "delete_architecture_relationship",
        "delete_architecture_standard",
        "delete_architecture_view",
        "delete_architecture_view_connection",
        "delete_architecture_view_object",
        "delete_decision",
        "delete_model",
        "delete_principle",
        "delete_programme",
        "delete_requirement",
        "delete_risk",
        "delete_toc_edge",
        "delete_toc_node",
        "delete_toc_tree",
        "delete_trace_link",
        "delete_uml_diagram",
        "delete_uml_diagram_connection",
        "delete_uml_diagram_object",
        "delete_uml_element",
        "delete_uml_interaction",
        "delete_uml_relationship",
        "get_uml_profile",
    }
)


def _spec() -> dict[str, Any]:
    return app.openapi()


def _resolve(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        assert name not in seen, f"$ref cycle at {name}"
        seen.add(name)
        schema = components[name]
    return schema


_COMPOSITES = ("allOf", "anyOf", "oneOf")


def is_loose(schema: dict[str, Any], components: dict[str, Any]) -> bool:
    """True when a generated client could only type this schema as ``unknown``.

    Composites (``allOf`` / ``anyOf`` / ``oneOf``) are judged by their members: a
    composite is loose only when EVERY non-null member is loose (or there is none), so a
    ``$ref`` wrapped in ``allOf`` or ``X | None`` counts as typed and ``Any | None`` does
    not. Own ``properties`` next to a composite also make it typed.

    Only the response's top-level shape is judged (plus array ``items``); nested property
    schemas are intentionally NOT. A body with named properties already gives the client a
    typed object, and some properties are legitimately ``Any``: e.g. the conformance
    result's code-unit ``name`` / ``container`` are copied verbatim from caller-supplied
    free-form dicts, so narrowing them would 500 on valid input.
    """
    schema = _resolve(schema, components)
    schema = {k: v for k, v in schema.items() if k not in ("title", "description")}
    if not schema:
        return True
    members = [
        m
        for key in _COMPOSITES
        for m in schema.get(key, [])
        if _resolve(m, components).get("type") != "null"
    ]
    if any(key in schema for key in _COMPOSITES):
        if schema.get("properties"):
            return False
        return all(is_loose(m, components) for m in members)
    if schema.get("type") == "array":
        return is_loose(schema.get("items", {}), components)
    if schema.get("type") == "object" or "properties" in schema or "additionalProperties" in schema:
        if schema.get("properties"):
            return False
        return schema.get("additionalProperties") in (None, True, {})
    return False


def _loose_operations() -> dict[str, str]:
    spec = _spec()
    components = spec.get("components", {}).get("schemas", {})
    loose: dict[str, str] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            for code, resp in op.get("responses", {}).items():
                if not str(code).startswith("2"):
                    continue
                schema = resp.get("content", {}).get("application/json", {}).get("schema")
                if schema is not None and is_loose(schema, components):
                    loose[op["operationId"]] = f"{method.upper()} {path} {code}"
    return loose


def test_loose_detector_is_not_vacuous() -> None:
    """Negative + positive controls: the detector must be able to say both things."""
    assert is_loose({}, {})
    assert is_loose({"title": "Response", "type": "object", "additionalProperties": True}, {})
    assert is_loose({"type": "array", "items": {}}, {})
    assert is_loose({"anyOf": [{"type": "object"}, {"type": "null"}]}, {})
    assert is_loose({"anyOf": [{"type": "null"}]}, {})
    assert is_loose({"allOf": [{}]}, {})
    assert is_loose({"allOf": [{"$ref": "#/components/schemas/L"}]}, {"L": {"type": "object"}})
    assert is_loose({"oneOf": [{"type": "object"}, {"type": "array", "items": {}}]}, {})
    assert not is_loose({"type": "object", "properties": {"id": {"type": "string"}}}, {})
    assert not is_loose({"type": "object", "additionalProperties": {"type": "integer"}}, {})
    assert not is_loose({"type": "string"}, {})
    assert not is_loose({"$ref": "#/components/schemas/X"}, {"X": {"properties": {"a": {}}}})
    typed = {"X": {"properties": {"a": {}}}}
    assert not is_loose({"allOf": [{"$ref": "#/components/schemas/X"}]}, typed)
    assert not is_loose({"anyOf": [{"$ref": "#/components/schemas/X"}, {"type": "null"}]}, typed)
    assert not is_loose({"oneOf": [{"type": "object"}, {"$ref": "#/components/schemas/X"}]}, typed)
    # Nested property schemas are not judged: an ``Any`` property inside a typed body is fine.
    assert not is_loose({"type": "object", "properties": {"free": {}}}, {})


def test_no_new_loose_2xx_json_response() -> None:
    loose = _loose_operations()
    new = {op: where for op, where in loose.items() if op not in LOOSE_ALLOWLIST}
    assert not new, (
        "2xx application/json response(s) with a loose schema (typed `unknown` in the "
        f"generated fr client). Add a response_model: {new}"
    )


def test_allowlist_only_shrinks() -> None:
    loose = _loose_operations()
    stale = sorted(LOOSE_ALLOWLIST - set(loose))
    assert not stale, f"now typed — remove from LOOSE_ALLOWLIST: {stale}"


def test_fr_consumed_operations_are_typed() -> None:
    loose = _loose_operations()
    assert not FR_CONSUMED_TYPED & set(loose), sorted(FR_CONSUMED_TYPED & set(loose))
    assert not FR_CONSUMED_TYPED & LOOSE_ALLOWLIST
    op_ids = {op["operationId"] for ops in _spec()["paths"].values() for op in ops.values()}
    assert op_ids >= FR_CONSUMED_TYPED, sorted(FR_CONSUMED_TYPED - op_ids)
