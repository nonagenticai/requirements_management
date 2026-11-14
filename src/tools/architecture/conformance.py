"""Architecture conformance — as-designed (ArchiMate) vs as-built (code-graph).

Pure functions: lists of dicts in, dict out. No DB, no I/O, no app imports.
The router loads the ArchiMate ``elements`` + ``relationships`` from this
service and the realized ``code_units`` (fetched from coding-analysis) and
passes them here, exactly like ``tools/integration/req_arch_trace.py``.

Bridge model (v1, *component* grain — the grain coding-analysis exposes with
dependency facts):

    code-graph component  --bound to-->  Artifact element
        (layer=technology, element_type=artifact, is_synthetic=true,
         architecture_element_realisation_bindings row of kind=coding_component
         keyed by (container, component_id))
    Artifact element      --realization-->  Application Component element

So a realized code dependency ``componentA → componentB`` resolves to a designed
dependency ``appCompA → appCompB`` via component → artifact → application_component.

Three deterministic measures (reproducible — identical inputs yield identical
output; the LLM never touches this computation):

* ``realization_coverage``  — which Application Components are realized by an
  Artifact (designed *and* built) vs not (designed, not built); which bound
  Artifacts realize nothing (built, undocumented); which code units are not yet
  bound into the model.
* ``dependency_conformance`` — realized code dependencies (lifted to the
  Application-Component level) vs designed ArchiMate dependencies: matched /
  missing (designed-not-realized) / extra (realized-not-designed) + a score.
* ``illegal_dependencies`` — realized dependencies that violate a layered
  "no upward dependency" policy applied to the code-graph's own layer names.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# ── ArchiMate identities for the bridge ──────────────────────────────────────

APP_COMPONENT_LAYER = "application"
APP_COMPONENT_TYPE = "application_component"
ARTIFACT_TYPE = "artifact"
REALIZATION = "realization"

# Directed ArchiMate relationship types that express a *designed* dependency
# between two Application Components. Tunable. Excludes ``association`` (too
# weak / undirected) and ``specialization`` (subtyping, not dependency).
DESIGNED_DEPENDENCY_TYPES: frozenset[str] = frozenset(
    {"serving", "flow", "triggering", "realization", "composition", "aggregation"}
)

# Default layered-architecture policy for illegal-dependency detection, applied
# to the code-graph's own free-form layer names (lower-cased). Position = rank:
# earlier = higher/shallower layer. A layered architecture permits dependencies
# to flow downward (a higher layer using a lower one) and same-layer; a
# dependency from a lower layer *up* to a higher one violates the policy.
# Unknown layer names are skipped (reported as ``unranked``), never flagged.
DEFAULT_LAYER_ORDER: tuple[str, ...] = (
    "presentation",
    "ui",
    "api",
    "application",
    "service",
    "business",
    "domain",
    "data",
    "persistence",
    "infrastructure",
)


# ── Small shared helpers ─────────────────────────────────────────────────────


def _elem_summary(elem: dict) -> dict[str, Any]:
    return {
        "id": str(elem.get("id", "")),
        "title": elem.get("title", ""),
        "layer": elem.get("layer", ""),
        "element_type": elem.get("element_type", ""),
    }


def _unit_key(container: Any, component_id: Any) -> tuple[str, str]:
    return (str(container or ""), str(component_id))


def _artifact_binding_keys(artifact: dict) -> list[tuple[str, str]]:
    """Every (container, component_id) an Artifact is bound to.

    Reads the resolved ``code_bindings`` list stamped on by
    ``architecture_tools._load_elements`` from the structured
    realisation-bindings table (the canonical source). An element may carry
    several bindings; each is evaluated, none is silently dropped.
    """
    keys: list[tuple[str, str]] = []
    for binding in artifact.get("code_bindings") or []:
        if not isinstance(binding, dict):
            continue
        container = binding.get("container")
        component_id = binding.get("component_id")
        if container and component_id is not None:
            keys.append(_unit_key(container, component_id))
    return keys


def _binding_entry(artifact: dict, key: tuple[str, str], **extra: Any) -> dict[str, Any]:
    container, component_id = key
    return {
        "id": str(artifact.get("id", "")),
        "name": artifact.get("name", ""),
        "container": container,
        "component_id": component_id,
        **extra,
    }


def _index(elements: list[dict]) -> tuple[list[dict], list[dict], set[str], set[str]]:
    """Split elements into Application Components and Artifacts (+ their id sets)."""
    app_components = [
        e
        for e in elements
        if e.get("layer") == APP_COMPONENT_LAYER and e.get("element_type") == APP_COMPONENT_TYPE
    ]
    artifacts = [e for e in elements if e.get("element_type") == ARTIFACT_TYPE]
    return (
        app_components,
        artifacts,
        {str(e["id"]) for e in app_components},
        {str(a["id"]) for a in artifacts},
    )


def _realization_map(
    relationships: list[dict], artifact_ids: set[str], app_ids: set[str]
) -> dict[str, set[str]]:
    """artifact_id → set(application_component_id) from realization edges."""
    out: dict[str, set[str]] = defaultdict(set)
    for rel in relationships:
        if rel.get("relationship_type") != REALIZATION:
            continue
        src = str(rel.get("source_id"))
        tgt = str(rel.get("target_id"))
        if src in artifact_ids and tgt in app_ids:
            out[src].add(tgt)
    return out


def _component_to_appcomps(
    artifacts: list[dict], realization: dict[str, set[str]]
) -> dict[tuple[str, str], set[str]]:
    """(container, component_id) → set(application_component_id), via the
    Artifact that the code unit is bound to and the App Component(s) it realizes."""
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for art in artifacts:
        for key in _artifact_binding_keys(art):
            for app_id in realization.get(str(art["id"]), set()):
                out[key].add(app_id)
    return out


# ── Measure 1: realization coverage ──────────────────────────────────────────


def realization_coverage(
    elements: list[dict],
    relationships: list[dict],
    code_units: list[dict],
    insufficient_containers: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    """Bidirectional realization coverage between Application Components and code.

    code_units: ``[{"id", "name", "layer", "container", "dependencies": [...]}]``
    (one per code-graph component, ``container`` attached by the caller).

    ``insufficient_containers``: bound containers the code graph could not be
    read for (coding-analysis 404). Their bindings are neither resolved nor
    dead: they are listed under ``insufficient_evidence``.
    """
    app_components, artifacts, app_ids, art_ids = _index(elements)
    realization = _realization_map(relationships, art_ids, app_ids)

    realized_app_ids: set[str] = set()
    for targets in realization.values():
        realized_app_ids |= targets

    realized = [e for e in app_components if str(e["id"]) in realized_app_ids]
    unrealized = [e for e in app_components if str(e["id"]) not in realized_app_ids]
    orphan_artifacts = [a for a in artifacts if str(a["id"]) not in realization]

    bound_keys = {k for a in artifacts for k in _artifact_binding_keys(a)}
    unbound = [
        {"id": str(u.get("id", "")), "name": u.get("name", ""), "container": u.get("container", "")}
        for u in code_units
        if _unit_key(u.get("container"), u.get("id")) not in bound_keys
    ]

    # The INVERSE of ``unbound``: a binding whose code component no longer exists —
    # the model still claims to describe something the codebase has deleted or renamed.
    # ``unbound`` alone reports built-but-undocumented; without this, drift is only ever
    # half-visible, and a model can rot indefinitely while every score stays healthy.
    # It is computed HERE because this is the one place both sides are already in hand:
    # ``bound_keys`` is the model's claim, ``code_units`` is what the graph actually holds.
    #
    # ⚠️ THE GUARD IS THE LOAD-BEARING PART. With NO code units the denominator is absent
    # and EVERY binding would key-miss, so an unreadable graph would be reported as total
    # model rot — the exact mirror of the fabricated 1.0 that ``coverage`` refuses above.
    # Return None ("not measured"), never a full list. A caller must treat None as no data.
    #
    # EVERY binding is evaluated: an Artifact with several bindings is backed by the
    # code only if ALL of them resolve, and each one that does not is listed here.
    # A binding into a container with no readable graph is NOT dead — that is
    # missing evidence, reported under ``insufficient_evidence`` instead.
    insufficient_evidence = [
        _binding_entry(a, key, status="insufficient_evidence")
        for a in artifacts
        for key in _artifact_binding_keys(a)
        if key[0] in insufficient_containers
    ]
    dead_bindings: list[dict[str, Any]] | None = None
    if code_units:
        unit_keys = {_unit_key(u.get("container"), u.get("id")) for u in code_units}
        dead_bindings = [
            # Built from the KEY the comparison used, so the report cannot drift from it.
            _binding_entry(a, key)
            for a in artifacts
            for key in _artifact_binding_keys(a)
            if key not in unit_keys and key[0] not in insufficient_containers
        ]

    total = len(app_components)
    # No Application Components designed => no coverage denominator => None
    # (honest "no data"), NOT a fabricated 1.0.
    coverage = round(len(realized) / total, 4) if total else None

    return {
        "realization_coverage": coverage,
        "total_application_components": total,
        "realized_count": len(realized),
        "realized": [_elem_summary(e) for e in realized],
        "unrealized": [_elem_summary(e) for e in unrealized],
        "orphan_artifacts": [_elem_summary(a) for a in orphan_artifacts],
        "unbound_code_units": unbound,
        "dead_bindings": dead_bindings,
        "insufficient_evidence": insufficient_evidence,
    }


# ── Measure 2: dependency conformance ────────────────────────────────────────


def dependency_conformance(
    elements: list[dict],
    relationships: list[dict],
    code_units: list[dict],
    insufficient_containers: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    """Designed vs realized dependencies, lifted to the Application-Component level.

    A designed dependency not seen in the code is ``missing`` only when the code
    for both ends could be read: one touching an Application Component realised
    through a container in ``insufficient_containers`` is ``insufficient_evidence``
    and kept out of the score.
    """
    app_components, artifacts, app_ids, art_ids = _index(elements)
    realization = _realization_map(relationships, art_ids, app_ids)
    comp_to_apps = _component_to_appcomps(artifacts, realization)
    app_by_id = {str(e["id"]): e for e in app_components}
    unknown_apps: set[str] = set()
    for key, apps in comp_to_apps.items():
        if key[0] in insufficient_containers:
            unknown_apps |= apps

    designed: set[tuple[str, str]] = set()
    for rel in relationships:
        if rel.get("relationship_type") not in DESIGNED_DEPENDENCY_TYPES:
            continue
        s, t = str(rel.get("source_id")), str(rel.get("target_id"))
        if s in app_ids and t in app_ids and s != t:
            designed.add((s, t))

    realized: set[tuple[str, str]] = set()
    unmappable = 0
    for unit in code_units:
        src_apps = comp_to_apps.get(_unit_key(unit.get("container"), unit.get("id")))
        for dep in unit.get("dependencies", []) or []:
            tgt_apps = comp_to_apps.get(_unit_key(unit.get("container"), dep))
            if not src_apps or not tgt_apps:
                unmappable += 1
                continue
            for sa in src_apps:
                for ta in tgt_apps:
                    if sa != ta:
                        realized.add((sa, ta))

    matched = designed & realized
    unjudged = {p for p in designed - realized if p[0] in unknown_apps or p[1] in unknown_apps}
    missing = designed - realized - unjudged
    extra = realized - designed
    union = (designed | realized) - unjudged
    # No designed and no realized dependencies => nothing to conform => None
    # (honest "no data"), NOT a fabricated 1.0.
    score = round(len(matched) / len(union), 4) if union else None

    def _pair(pair: tuple[str, str]) -> dict[str, Any]:
        s, t = pair
        return {
            "source": _elem_summary(app_by_id.get(s, {"id": s})),
            "target": _elem_summary(app_by_id.get(t, {"id": t})),
        }

    return {
        "conformance_score": score,
        "designed_count": len(designed),
        "realized_count": len(realized),
        "matched": [_pair(p) for p in sorted(matched)],
        "missing": [_pair(p) for p in sorted(missing)],
        "extra": [_pair(p) for p in sorted(extra)],
        "insufficient_evidence": [_pair(p) for p in sorted(unjudged)],
        "unmappable_realized_edges": unmappable,
    }


# ── Measure 3: illegal (upward) dependencies ─────────────────────────────────


def illegal_dependencies(
    code_units: list[dict],
    layer_order: tuple[str, ...] = DEFAULT_LAYER_ORDER,
) -> dict[str, Any]:
    """Realized dependencies that violate a layered "no upward dependency" policy.

    Operates on the code-graph's own layer names. A dependency from a lower
    layer up to a higher layer is illegal; same-layer and downward are allowed.
    Unknown layer names are skipped and counted as ``unranked``.
    """
    rank = {name: i for i, name in enumerate(layer_order)}
    by_key = {_unit_key(u.get("container"), u.get("id")): u for u in code_units}

    illegal: list[dict[str, Any]] = []
    unranked = 0
    for unit in code_units:
        src_layer = (unit.get("layer") or "").strip().lower()
        for dep in unit.get("dependencies", []) or []:
            target = by_key.get(_unit_key(unit.get("container"), dep))
            if target is None:
                continue
            tgt_layer = (target.get("layer") or "").strip().lower()
            if src_layer not in rank or tgt_layer not in rank:
                unranked += 1
                continue
            if rank[src_layer] > rank[tgt_layer]:
                illegal.append(
                    {
                        "source": {"id": str(unit.get("id", "")), "name": unit.get("name", "")},
                        "target": {"id": str(target.get("id", "")), "name": target.get("name", "")},
                        "source_layer": src_layer,
                        "target_layer": tgt_layer,
                        "reason": f"upward dependency: {src_layer} → {tgt_layer}",
                    }
                )

    return {
        "illegal_count": len(illegal),
        "illegal_dependencies": illegal,
        "unranked_edges": unranked,
        "layer_order": list(layer_order),
    }


# ── Orchestrator (single call for the governance dashboard) ───────────────────


def analyze_conformance(
    elements: list[dict],
    relationships: list[dict],
    code_units: list[dict],
    layer_order: tuple[str, ...] = DEFAULT_LAYER_ORDER,
    *,
    insufficient_containers: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    """Run all three measures over one load. Pure + reproducible.

    ``insufficient_containers``: bound containers whose code graph could not be
    read; see ``realization_coverage``.
    """
    coverage = realization_coverage(elements, relationships, code_units, insufficient_containers)
    deps = dependency_conformance(elements, relationships, code_units, insufficient_containers)
    illegal = illegal_dependencies(code_units, layer_order)
    return {
        "realization_coverage": coverage,
        "dependency_conformance": deps,
        "illegal_dependencies": illegal,
        "summary": {
            "realization_coverage": coverage["realization_coverage"],
            "conformance_score": deps["conformance_score"],
            "illegal_count": illegal["illegal_count"],
            "unrealized_components": len(coverage["unrealized"]),
            "orphan_artifacts": len(coverage["orphan_artifacts"]),
            # None (not a count) when there were no code units to compare against, so a
            # consumer can tell "no dead bindings" from "could not measure".
            "dead_binding_count": (
                None if coverage["dead_bindings"] is None else len(coverage["dead_bindings"])
            ),
            "insufficient_evidence_count": len(coverage["insufficient_evidence"]),
            # Bound containers coding-analysis had no graph for (404): their
            # bindings are unjudged, not dead and not unrealised.
            "insufficient_evidence_containers": sorted(insufficient_containers),
        },
    }
