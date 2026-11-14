"""ArchiMate 3.2 relationship validator — one triple at a time.

Answers "may ``source`` have a ``relationship_type`` relationship to
``target``?" by looking the triple up in Archi 5.10.0's relationship matrix
(``tools.architecture.registry``). It used to answer from element categories
and layer order, which disagreed with the matrix on 5,146 of 35,739 type-level
triples (2,367 accepted that Archi forbids, 2,779 rejected that Archi permits)
and returned ``valid=True`` for TOC and PM-scheduling edges between ArchiMate
elements (findings A02, A03 of the 2026-10-26 ArchiMate plan).

Whole-model checks (junctions, views, duplicates, …) live in
``tools.architecture.archimate_rules``; this module is the single-triple entry
point the write path and ``/architecture/tools/validate-relationship`` use.

Pure computation — no database, no async.
"""

from __future__ import annotations

from schemas.architecture import MetamodelValidationResult
from tools.architecture.registry import RELATIONSHIP_ENDPOINT, get_registry


def validate_relationship(
    source_layer: str,
    source_element_type: str,
    target_layer: str,
    target_element_type: str,
    relationship_type: str,
) -> MetamodelValidationResult:
    """Validate one ArchiMate relationship triple against the Archi matrix.

    Either end may be the generic relationship endpoint (``"relationship"``,
    with any layer) — ArchiMate lets e.g. an association or aggregation point
    at another relationship. A supplied layer must agree with the registry's
    layer for the type; an unknown type or a non-ArchiMate relationship type is
    always invalid, never waved through.

    Returns:
        ``{"valid": bool, "violations": [...], "suggestions": [...]}`` where
        ``suggestions`` lists the relationship types the matrix does permit.
    """
    reg = get_registry()
    rel = relationship_type.strip().lower()
    ends = {
        "source": (source_layer.strip().lower(), source_element_type.strip().lower()),
        "target": (target_layer.strip().lower(), target_element_type.strip().lower()),
    }
    violations: list[str] = []

    if rel not in reg.relationship_types:
        violations.append(
            f"'{rel}' is not an ArchiMate relationship type; valid types: "
            f"{', '.join(sorted(reg.relationship_types))}. PM-scheduling, TOC and "
            "traceability links are validated by their own domains, not between "
            "ArchiMate elements"
        )

    for end, (layer, etype) in ends.items():
        if etype == RELATIONSHIP_ENDPOINT:
            continue
        concept = reg.concepts.get(etype)
        if concept is None:
            violations.append(f"unknown {end} element type '{etype}'")
        elif layer != concept.layer:
            violations.append(
                f"{end} element type '{etype}' belongs to layer '{concept.layer}', not '{layer}'"
            )

    if violations:
        return MetamodelValidationResult(valid=False, violations=violations, suggestions=[])

    src_t, tgt_t = ends["source"][1], ends["target"][1]
    allowed = reg.allowed(src_t, tgt_t)
    if rel in allowed:
        return MetamodelValidationResult(valid=True, violations=[], suggestions=[])
    return MetamodelValidationResult(
        valid=False,
        violations=[
            f"{rel} is not permitted from {src_t} to {tgt_t} "
            f"(ArchiMate {reg.language_version}, Archi {reg.reference['version']} matrix)"
        ],
        suggestions=[f"use {r}" for r in sorted(allowed)],
    )
