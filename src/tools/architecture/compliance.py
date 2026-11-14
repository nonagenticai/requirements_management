"""Standards compliance checker."""

from __future__ import annotations

from typing import Any

STANDARD_TYPE_LAYERS: dict[str, list[str] | None] = {
    "technology_standard": ["technology", "physical"],
    "application_standard": ["application"],
    "data_standard": None,
    "business_standard": ["business"],
    "guideline": None,
}

DATA_ELEMENT_TYPES: frozenset[str] = frozenset({"data_object", "business_object", "artifact"})

LINKING_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {"realization", "association", "traceability"}
)


def _element_summary(elem: dict) -> dict[str, Any]:
    return {
        "id": str(elem.get("id", "")),
        "title": elem.get("title", ""),
        "layer": elem.get("layer", ""),
        "element_type": elem.get("element_type", ""),
    }


def _standard_summary(std: dict) -> dict[str, Any]:
    return {
        "id": str(std.get("id", "")),
        "title": std.get("title", ""),
        "standard_type": std.get("standard_type", ""),
        "compliance_level": std.get("compliance_level", ""),
    }


def _applicable_elements(
    standard: dict,
    elements: list[dict],
) -> list[dict]:
    stype = standard.get("standard_type", "")
    layers = STANDARD_TYPE_LAYERS.get(stype)

    if stype == "data_standard":
        return [e for e in elements if e.get("element_type", "") in DATA_ELEMENT_TYPES]

    if layers is None:
        return list(elements)

    return [e for e in elements if e.get("layer", "") in layers]


def check_compliance(
    elements: list[dict],
    standards: list[dict],
    relationships: list[dict],
) -> dict[str, Any]:
    linked_pairs: set[tuple[str, str]] = set()
    for rel in relationships:
        if rel.get("relationship_type", "") not in LINKING_RELATIONSHIP_TYPES:
            continue
        src = str(rel["source_id"])
        tgt = str(rel["target_id"])
        linked_pairs.add((src, tgt))
        linked_pairs.add((tgt, src))

    standard_ids = {str(s["id"]) for s in standards}

    elements_linked_to_standards: set[str] = set()
    for rel in relationships:
        if rel.get("relationship_type", "") not in LINKING_RELATIONSHIP_TYPES:
            continue
        src = str(rel["source_id"])
        tgt = str(rel["target_id"])
        if src in standard_ids:
            elements_linked_to_standards.add(tgt)
        if tgt in standard_ids:
            elements_linked_to_standards.add(src)

    violations: list[dict] = []
    warnings: list[dict] = []
    compliant: list[dict] = []
    optional: list[dict] = []

    for std in standards:
        std_id = str(std["id"])
        level = std.get("compliance_level", "optional")
        status = std.get("status", "assess")

        if status in ("hold", "retired"):
            continue

        applicable = _applicable_elements(std, elements)
        if not applicable:
            continue

        linked_count = 0
        unlinked: list[dict] = []
        for elem in applicable:
            eid = str(elem["id"])
            if (eid, std_id) in linked_pairs or (std_id, eid) in linked_pairs:
                linked_count += 1
            else:
                unlinked.append(elem)

        if level == "optional":
            # Optional standards carry no compliance obligation (mirrors
            # architecture_standards.applicable_standards_for_element, which
            # already excludes them). They are kept out of compliance_score
            # entirely — a compliant one used to inflate it, a non-compliant one
            # was silently dropped from total_checks. Their adherence is
            # surfaced separately via ``optional`` / ``optional_adherence``.
            optional.append(
                {
                    "standard": _standard_summary(std),
                    "applicable_count": len(applicable),
                    "linked_count": linked_count,
                    "fully_linked": not unlinked,
                    "unlinked_elements": [_element_summary(e) for e in unlinked],
                }
            )
        elif not unlinked:
            compliant.append(
                {
                    "standard": _standard_summary(std),
                    "applicable_count": len(applicable),
                    "linked_count": linked_count,
                }
            )
        elif level == "mandatory":
            violations.append(
                {
                    "standard": _standard_summary(std),
                    "unlinked_elements": [_element_summary(e) for e in unlinked],
                    "applicable_count": len(applicable),
                    "linked_count": linked_count,
                }
            )
        elif level == "recommended":
            warnings.append(
                {
                    "standard": _standard_summary(std),
                    "unlinked_elements": [_element_summary(e) for e in unlinked],
                    "applicable_count": len(applicable),
                    "linked_count": linked_count,
                }
            )

    all_covered = elements_linked_to_standards
    uncovered = [_element_summary(e) for e in elements if str(e["id"]) not in all_covered]

    total_checks = len(violations) + len(warnings) + len(compliant)
    # Optional standards are excluded from scoring (see the loop). 0/0 is
    # undefined: no scored mandatory/recommended checks means no compliance
    # signal, so the score is None (honest "no data"), NOT a fabricated 1.0 pass.
    compliance_score = round(len(compliant) / total_checks, 4) if total_checks > 0 else None

    # Optional adherence is reported separately so the optional signal is
    # surfaced, never silently dropped. None when there are no applicable
    # optional standards (0/0), mirroring compliance_score's honesty.
    optional_fully_linked = sum(1 for o in optional if o["fully_linked"])
    optional_adherence = round(optional_fully_linked / len(optional), 4) if optional else None

    return {
        "compliance_score": compliance_score,
        "violations": violations,
        "warnings": warnings,
        "compliant": compliant,
        "optional": optional,
        "optional_adherence": optional_adherence,
        "uncovered_elements": uncovered,
    }
