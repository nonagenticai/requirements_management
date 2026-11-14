"""Trace-link end kinds (target spec §7.4) and the tables they live in."""

TRACE_KIND_TABLES: dict[str, str] = {
    "requirement": "requirements",
    "principle": "principles",
    "decision": "decisions",
    "element": "architecture_elements",
    "relationship": "architecture_relationships",
    "view": "architecture_views",
    "uml_element": "uml_elements",
    "uml_relationship": "uml_relationships",
    "interaction": "uml_interactions",
    "diagram": "uml_diagrams",
}
TRACE_KIND_BY_TABLE: dict[str, str] = {v: k for k, v in TRACE_KIND_TABLES.items()}
UML_TRACE_KINDS = ("uml_element", "uml_relationship", "interaction", "diagram")
TRACE_KINDS = tuple(TRACE_KIND_TABLES)
