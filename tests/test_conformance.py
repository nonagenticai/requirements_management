"""Unit tests for architecture conformance (as-designed vs as-built).

All pure functions — no database. Plus a metamodel test for the deployment
assignment (Node → Artifact) the bridge relies on.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from clients.coding_analysis import (
    CodingAnalysisContainerNotFoundError,
    CodingAnalysisUnreadableBodyError,
)
from tools.architecture.conformance import (
    analyze_conformance,
    dependency_conformance,
    illegal_dependencies,
    realization_coverage,
)
from tools.architecture.metamodel import validate_relationship

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_ID = 0


def _next_id() -> str:
    global _ID
    _ID += 1
    return f"00000000-0000-0000-0000-{_ID:012d}"


def _app(title: str) -> dict:
    return {
        "id": _next_id(),
        "title": title,
        "layer": "application",
        "element_type": "application_component",
        "properties": {},
    }


def _artifact(container: str, component_id: str, title: str = "", *more: tuple) -> dict:
    # Mirrors the post-_load_elements shape: the resolved ``code_bindings``
    # list is sourced from the structured realisation-bindings table.
    bindings = [(container, component_id), *more]
    return {
        "id": _next_id(),
        "title": title or f"artifact:{component_id}",
        "layer": "technology",
        "element_type": "artifact",
        "is_synthetic": True,
        "code_bindings": [{"container": c, "component_id": cid} for c, cid in bindings],
        "properties": {},
    }


def _realizes(artifact: dict, app: dict) -> dict:
    return {
        "id": _next_id(),
        "source_entity_type": "architecture_elements",
        "source_id": artifact["id"],
        "target_entity_type": "architecture_elements",
        "target_id": app["id"],
        "relationship_type": "realization",
        "properties": {},
    }


def _serving(src: dict, tgt: dict, rel_type: str = "serving") -> dict:
    return {
        "id": _next_id(),
        "source_entity_type": "architecture_elements",
        "source_id": src["id"],
        "target_entity_type": "architecture_elements",
        "target_id": tgt["id"],
        "relationship_type": rel_type,
        "properties": {},
    }


def _unit(container: str, cid: str, layer: str, deps: list[str], name: str = "") -> dict:
    return {
        "id": cid,
        "name": name or cid,
        "layer": layer,
        "container": container,
        "dependencies": deps,
    }


# ===========================================================================
# Realization coverage
# ===========================================================================


class TestRealizationCoverage:
    def test_full_coverage(self) -> None:
        a, b = _app("A"), _app("B")
        art_a = _artifact("c", "ca")
        art_b = _artifact("c", "cb")
        rels = [_realizes(art_a, a), _realizes(art_b, b)]
        units = [_unit("c", "ca", "presentation", ["cb"]), _unit("c", "cb", "data", [])]
        out = realization_coverage([a, b, art_a, art_b], rels, units)
        assert out["realization_coverage"] == 1.0
        assert out["realized_count"] == 2
        assert out["unrealized"] == []
        assert out["orphan_artifacts"] == []
        assert out["unbound_code_units"] == []

    def test_unrealized_component(self) -> None:
        a, b = _app("A"), _app("B")  # B designed but never built
        art_a = _artifact("c", "ca")
        out = realization_coverage(
            [a, b, art_a], [_realizes(art_a, a)], [_unit("c", "ca", "x", [])]
        )
        assert out["realization_coverage"] == 0.5
        assert [e["title"] for e in out["unrealized"]] == ["B"]

    def test_orphan_artifact(self) -> None:
        a = _app("A")
        art_orphan = _artifact("c", "cz")  # bound, realizes nothing
        out = realization_coverage([a, art_orphan], [], [_unit("c", "cz", "x", [])])
        assert len(out["orphan_artifacts"]) == 1
        assert out["realization_coverage"] == 0.0

    def test_unbound_code_unit(self) -> None:
        a = _app("A")
        art_a = _artifact("c", "ca")
        # cb has no artifact binding it into the model
        units = [_unit("c", "ca", "x", []), _unit("c", "cb", "x", [])]
        out = realization_coverage([a, art_a], [_realizes(art_a, a)], units)
        assert [u["id"] for u in out["unbound_code_units"]] == ["cb"]

    def test_empty_model_is_not_applicable(self) -> None:
        # No Application Components => no coverage denominator. Honest "no data"
        # is None, NOT a fabricated 1.0 perfect coverage.
        out = realization_coverage([], [], [])
        assert out["realization_coverage"] is None
        assert out["total_application_components"] == 0


# ===========================================================================
# Dependency conformance
# ===========================================================================


class TestDependencyConformance:
    def _model(self):
        a, b = _app("A"), _app("B")
        art_a, art_b = _artifact("c", "ca"), _artifact("c", "cb")
        elements = [a, b, art_a, art_b]
        rels = [_realizes(art_a, a), _realizes(art_b, b)]
        return a, b, elements, rels

    def test_matched(self) -> None:
        a, b, elements, rels = self._model()
        rels.append(_serving(a, b))  # designed A → B
        units = [_unit("c", "ca", "x", ["cb"]), _unit("c", "cb", "x", [])]  # realized A → B
        out = dependency_conformance(elements, rels, units)
        assert out["conformance_score"] == 1.0
        assert len(out["matched"]) == 1
        assert out["missing"] == [] and out["extra"] == []

    def test_missing_designed_not_realized(self) -> None:
        a, b, elements, rels = self._model()
        rels.append(_serving(a, b))  # designed but code has no dep
        units = [_unit("c", "ca", "x", []), _unit("c", "cb", "x", [])]
        out = dependency_conformance(elements, rels, units)
        assert out["conformance_score"] == 0.0
        assert len(out["missing"]) == 1 and out["matched"] == []

    def test_extra_realized_not_designed(self) -> None:
        a, b, elements, rels = self._model()  # no designed edge
        units = [_unit("c", "ca", "x", ["cb"]), _unit("c", "cb", "x", [])]
        out = dependency_conformance(elements, rels, units)
        assert out["conformance_score"] == 0.0
        assert len(out["extra"]) == 1 and out["missing"] == []

    def test_unmappable_edge_counted(self) -> None:
        a, b, elements, rels = self._model()
        # cb depends on an unbound component cz → cannot lift to app level
        units = [_unit("c", "cb", "x", ["cz"])]
        out = dependency_conformance(elements, rels, units)
        assert out["unmappable_realized_edges"] == 1
        assert out["realized_count"] == 0

    def test_self_loop_ignored(self) -> None:
        a, b, elements, rels = self._model()
        units = [_unit("c", "ca", "x", ["ca"])]  # component depends on itself
        out = dependency_conformance(elements, rels, units)
        assert out["realized_count"] == 0

    def test_no_designed_no_realized_is_not_applicable(self) -> None:
        # No designed and no realized dependencies => empty union => nothing to
        # conform. Honest "no data" is None, NOT a fabricated 1.0.
        out = dependency_conformance([], [], [])
        assert out["conformance_score"] is None
        assert out["designed_count"] == 0
        assert out["realized_count"] == 0


# ===========================================================================
# Illegal dependencies (layered no-upward policy)
# ===========================================================================


class TestIllegalDependencies:
    def test_downward_is_allowed(self) -> None:
        units = [_unit("c", "ui", "presentation", ["db"]), _unit("c", "db", "data", [])]
        out = illegal_dependencies(units)
        assert out["illegal_count"] == 0

    def test_same_layer_is_allowed(self) -> None:
        units = [_unit("c", "x", "business", ["y"]), _unit("c", "y", "business", [])]
        assert illegal_dependencies(units)["illegal_count"] == 0

    def test_upward_is_illegal(self) -> None:
        units = [_unit("c", "db", "data", ["ui"]), _unit("c", "ui", "presentation", [])]
        out = illegal_dependencies(units)
        assert out["illegal_count"] == 1
        assert out["illegal_dependencies"][0]["reason"] == "upward dependency: data → presentation"

    def test_unknown_layer_skipped(self) -> None:
        units = [_unit("c", "a", "weird-layer", ["b"]), _unit("c", "b", "data", [])]
        out = illegal_dependencies(units)
        assert out["illegal_count"] == 0
        assert out["unranked_edges"] == 1

    def test_reproducible(self) -> None:
        units = [_unit("c", "db", "data", ["ui"]), _unit("c", "ui", "presentation", [])]
        assert illegal_dependencies(units) == illegal_dependencies(units)


# ===========================================================================
# Orchestrator
# ===========================================================================


def test_analyze_conformance_summary() -> None:
    a, b = _app("A"), _app("B")
    art_a, art_b = _artifact("c", "ca"), _artifact("c", "cb")
    elements = [a, b, art_a, art_b]
    rels = [_realizes(art_a, a), _realizes(art_b, b), _serving(a, b)]
    units = [_unit("c", "ca", "presentation", ["cb"]), _unit("c", "cb", "data", [])]
    out = analyze_conformance(elements, rels, units)
    assert out["summary"]["realization_coverage"] == 1.0
    assert out["summary"]["conformance_score"] == 1.0
    assert out["summary"]["illegal_count"] == 0
    assert set(out) == {
        "realization_coverage",
        "dependency_conformance",
        "illegal_dependencies",
        "summary",
    }


def test_analyze_conformance_empty_is_not_applicable() -> None:
    # The orchestrator passes the sub-tool scores straight into `summary`, so an
    # empty model must surface None (honest "no data") for both, NOT a fabricated 1.0.
    out = analyze_conformance([], [], [])
    assert out["summary"]["realization_coverage"] is None
    assert out["summary"]["conformance_score"] is None
    assert out["summary"]["illegal_count"] == 0


# ===========================================================================
# Metamodel — deployment assignment (Node → Artifact) the bridge needs
# ===========================================================================


class TestDeploymentAssignment:
    def test_node_assigned_to_artifact_is_valid(self) -> None:
        result = validate_relationship("technology", "node", "technology", "artifact", "assignment")
        assert result.valid, result.violations

    def test_active_to_behavior_assignment_still_valid(self) -> None:
        result = validate_relationship(
            "application",
            "application_component",
            "application",
            "application_function",
            "assignment",
        )
        assert result.valid, result.violations

    def test_passive_to_passive_assignment_invalid(self) -> None:
        # artifact → artifact is not a valid assignment (no deployment semantics)
        result = validate_relationship(
            "technology", "artifact", "technology", "artifact", "assignment"
        )
        assert not result.valid

    def test_artifact_realizes_application_component_is_valid(self) -> None:
        result = validate_relationship(
            "technology", "artifact", "application", "application_component", "realization"
        )
        assert result.valid, result.violations


# ===========================================================================
# Endpoint integration — POST /architecture/tools/conformance (DB-backed)
# ===========================================================================

_PROJECT = "conformance-test"


async def _seed_bridge(client: AsyncClient) -> dict:
    """Create one App Component + one bound Artifact realizing it. Returns the app."""
    app_resp = await client.post(
        "/architecture/elements",
        json={
            "title": "Order Service",
            "layer": "application",
            "element_type": "application_component",
            "architecture_state": "baseline",
            "project_id": _PROJECT,
        },
    )
    assert app_resp.status_code == 200, app_resp.text
    app = app_resp.json()

    art_resp = await client.post(
        "/architecture/elements",
        json={
            "title": "order-service artifact",
            "layer": "technology",
            "element_type": "artifact",
            "is_synthetic": True,
            "properties": {"code_binding": {"container": "demo", "component_id": "svc"}},
            "project_id": _PROJECT,
        },
    )
    # 201 since the synthetic-Artifact + code_binding payload now engages
    # the upsert path; 200 would mean an existing row was updated. Either is
    # a successful insert for this conformance fixture.
    assert art_resp.status_code in (200, 201), art_resp.text
    artifact = art_resp.json()

    rel_resp = await client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "architecture_elements",
            "source_id": artifact["id"],
            "target_entity_type": "architecture_elements",
            "target_id": app["id"],
            "relationship_type": "realization",
            "project_id": _PROJECT,
        },
    )
    assert rel_resp.status_code == 200, rel_resp.text
    return app


class TestConformanceEndpoint:
    async def test_caller_supplied_code_units(self, authenticated_client: AsyncClient) -> None:
        await _seed_bridge(authenticated_client)
        resp = await authenticated_client.post(
            "/architecture/tools/conformance",
            json={
                "project_id": _PROJECT,
                "code_units": [
                    {
                        "id": "svc",
                        "name": "svc",
                        "layer": "application",
                        "container": "demo",
                        "dependencies": [],
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["summary"]["realization_coverage"] == 1.0
        assert body["realization_coverage"]["realized_count"] == 1
        # The seeded model has one realization but NO designed or realized
        # dependencies (the code unit has empty `dependencies`), so the
        # dependency-conformance denominator is empty => honest "no data" is None,
        # not a fabricated 1.0.
        assert body["dependency_conformance"]["conformance_score"] is None
        assert body["illegal_dependencies"]["illegal_count"] == 0

    async def test_an_unreadable_coding_analysis_body_is_a_502_not_a_500(
        self, authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fault is coding-analysis's body, not this service's.

        `CodingAnalysisUnreadableBodyError` was caught by nothing on this path,
        so it reached the app-level handler in `main.py` and came back as a bare
        500 — which sends the operator to the wrong service's logs and loses the
        "which upstream" signal entirely. An operator meets ONE contract for an
        unreadable coding-* body.

        The whole request fails on purpose: `analyze_conformance` reads the
        accumulated `code_units` as the COMPLETE as-built picture, so continuing
        with the readable containers' units would report every element realized
        only by the unreadable one as an unrealized gap — a confident,
        complete-looking wrong answer.

        Deleting the `except CodingAnalysisUnreadableBodyError` clause makes this
        a 500 (or an escaped exception), never a 502.
        """
        await _seed_bridge(authenticated_client)

        async def _boom(container: str, *, tenant_id: str | None = None) -> list[dict]:
            raise CodingAnalysisUnreadableBodyError(
                f"coding-analysis returned an unreadable body for container {container!r}"
            )

        monkeypatch.setattr("routers.architecture_tools.fetch_code_units", _boom)

        resp = await authenticated_client.post(
            "/architecture/tools/conformance", json={"project_id": _PROJECT}
        )

        assert resp.status_code == 502, (
            f"got {resp.status_code} — an unreadable upstream body must be a BAD "
            "GATEWAY. A 500 blames this service and sends whoever is paged into the "
            f"wrong logs. Body: {resp.text}"
        )
        assert "demo" in resp.text, (
            "the failing container must reach the caller, or an operator cannot tell "
            f"WHICH bound container served the bad body. Body: {resp.text}"
        )


# ===========================================================================
# Dead bindings — the INVERSE of unbound_code_units
#
# `unbound_code_units` reports code with no model (built-but-undocumented).
# A binding whose component no longer exists — model claiming to describe code
# that was deleted or renamed — was never computed, so architectural drift was
# only ever half-visible: a model could rot indefinitely while every score
# stayed healthy. Both sides are already in hand inside realization_coverage,
# so this is a set subtraction, not a new data source.
# ===========================================================================
class TestDeadBindings:
    def test_binding_to_a_component_that_no_longer_exists_is_named(self) -> None:
        a = _app("A")
        art_live = _artifact("c", "ca")
        art_dead = _artifact("c", "cGONE")
        units = [_unit("c", "ca", "x", [])]  # cGONE is absent from the graph

        out = realization_coverage(
            [a, art_live, art_dead],
            [_realizes(art_live, a), _realizes(art_dead, a)],
            units,
        )

        assert out["dead_bindings"] is not None, "a readable roster must yield a verdict"
        assert [d["component_id"] for d in out["dead_bindings"]] == ["cGONE"], (
            "exactly the binding whose component is gone must be named — naming the "
            "live one too would make every harvest look rotten"
        )
        assert out["dead_bindings"][0]["container"] == "c"

    def test_all_bindings_resolving_yields_an_empty_list_not_none(self) -> None:
        a = _app("A")
        art = _artifact("c", "ca")
        out = realization_coverage([a, art], [_realizes(art, a)], [_unit("c", "ca", "x", [])])

        assert out["dead_bindings"] == [], (
            "'measured, none dead' must be an empty list — None is reserved for "
            "'not measured', and collapsing the two is what hides the difference"
        )

    # ⭐ THE GUARD. Without it an unreadable/empty code graph reports EVERY binding as
    # dead — the exact mirror of the fabricated 1.0 that `coverage` already refuses.
    def test_empty_roster_is_not_measured_rather_than_total_rot(self) -> None:
        a = _app("A")
        art_x, art_y = _artifact("c", "ca"), _artifact("c", "cb")

        out = realization_coverage([a, art_x, art_y], [_realizes(art_x, a)], [])

        assert out["dead_bindings"] is None, (
            "with no code units there is no denominator; reporting 2 dead bindings "
            "would turn 'I could not read the graph' into 'the model is entirely stale'"
        )

    def test_a_dead_binding_is_not_an_unbound_code_unit(self) -> None:
        """The two directions must not contaminate each other."""
        a = _app("A")
        art_dead = _artifact("c", "cGONE")
        units = [_unit("c", "cNEW", "x", [])]  # code present, model absent

        out = realization_coverage([a, art_dead], [_realizes(art_dead, a)], units)

        assert [u["id"] for u in out["unbound_code_units"]] == ["cNEW"]
        assert [d["component_id"] for d in out["dead_bindings"]] == ["cGONE"]

    def test_summary_count_distinguishes_none_from_zero(self) -> None:
        a = _app("A")
        art = _artifact("c", "ca")

        measured = analyze_conformance([a, art], [_realizes(art, a)], [_unit("c", "ca", "x", [])])
        assert measured["summary"]["dead_binding_count"] == 0

        unmeasured = analyze_conformance([a, art], [_realizes(art, a)], [])
        assert unmeasured["summary"]["dead_binding_count"] is None, (
            "a consumer thresholding on `dead_binding_count > 0` must not read "
            "'could not measure' as 'clean'"
        )


# ===========================================================================
# A09 — an element with SEVERAL bindings: every one is evaluated
# ===========================================================================
class TestMultiBinding:
    def test_an_artifact_is_backed_only_if_every_binding_resolves(self) -> None:
        a = _app("A")
        art = _artifact("c1", "ca", "", ("c2", "cGONE"))
        units = [_unit("c1", "ca", "x", []), _unit("c2", "other", "x", [])]

        out = realization_coverage([a, art], [_realizes(art, a)], units)

        assert [(d["container"], d["component_id"]) for d in out["dead_bindings"]] == [
            ("c2", "cGONE")
        ], "the one binding that does not resolve must be named, the other must not"
        assert [u["id"] for u in out["unbound_code_units"]] == ["other"]

    def test_dependencies_lift_through_every_binding(self) -> None:
        a, b = _app("A"), _app("B")
        art_a = _artifact("c1", "a1", "", ("c2", "a2"))
        art_b = _artifact("c2", "b1")
        units = [
            _unit("c1", "a1", "x", []),
            _unit("c2", "a2", "x", ["b1"]),  # only the SECOND binding of A depends on B
            _unit("c2", "b1", "x", []),
        ]
        out = dependency_conformance(
            [a, b, art_a, art_b],
            [_realizes(art_a, a), _realizes(art_b, b), _serving(a, b)],
            units,
        )
        assert out["conformance_score"] == 1.0, out
        assert out["missing"] == []


# ===========================================================================
# A 404 for a bound container is insufficient evidence, never "unrealised"
# ===========================================================================
class TestContainerNotFound:
    def test_bindings_into_an_unreadable_container_are_not_dead(self) -> None:
        a, b = _app("A"), _app("B")
        art_a, art_b = _artifact("good", "ga"), _artifact("gone", "gb")
        rels = [_realizes(art_a, a), _realizes(art_b, b), _serving(a, b)]
        units = [_unit("good", "ga", "x", [])]

        out = analyze_conformance(
            [a, b, art_a, art_b], rels, units, insufficient_containers={"gone"}
        )

        cov = out["realization_coverage"]
        assert cov["dead_bindings"] == []
        assert [(e["container"], e["status"]) for e in cov["insufficient_evidence"]] == [
            ("gone", "insufficient_evidence")
        ]
        deps = out["dependency_conformance"]
        assert deps["missing"] == [], "A->B cannot be judged missing without B's code"
        assert len(deps["insufficient_evidence"]) == 1
        assert out["summary"]["insufficient_evidence_containers"] == ["gone"]
        assert out["summary"]["insufficient_evidence_count"] == 1


_MULTI_PROJECT = f"conformance-multi-{uuid.uuid4().hex[:8]}"


async def _seed_multi_bound(client: AsyncClient) -> dict:
    """One App Component realised by one Artifact bound to TWO components."""
    app = (
        await client.post(
            "/architecture/elements",
            json={
                "title": f"multi-app-{uuid.uuid4().hex[:6]}",
                "layer": "application",
                "element_type": "application_component",
                "project_id": _MULTI_PROJECT,
            },
        )
    ).json()
    art = await client.post(
        "/architecture/elements",
        json={
            "title": f"multi-art-{uuid.uuid4().hex[:6]}",
            "layer": "technology",
            "element_type": "artifact",
            "is_synthetic": True,
            "project_id": _MULTI_PROJECT,
        },
    )
    assert art.status_code in (200, 201), art.text
    art = art.json()
    # Inserted in reverse order: the loader must still return both, sorted.
    for container, component_id in (("zeta", "z.one"), ("alpha", "a.one")):
        resp = await client.post(
            f"/architecture/elements/{art['id']}/bindings",
            json={
                "binding_kind": "coding_component",
                "container": container,
                "component_id": component_id,
            },
        )
        assert resp.status_code in (200, 201), resp.text
    rel = await client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "architecture_elements",
            "source_id": art["id"],
            "target_entity_type": "architecture_elements",
            "target_id": app["id"],
            "relationship_type": "realization",
            "project_id": _MULTI_PROJECT,
        },
    )
    assert rel.status_code == 200, rel.text
    return art


class TestMultiBindingEndpoint:
    async def test_a_404_container_is_insufficient_evidence_not_a_failure(
        self, authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_multi_bound(authenticated_client)

        async def _fake_fetch(container: str, *, tenant_id: str | None = None) -> list[dict]:
            if container == "zeta":
                raise CodingAnalysisContainerNotFoundError(container)
            return [_unit("alpha", "a.one", "x", [])]

        monkeypatch.setattr("routers.architecture_tools.fetch_code_units", _fake_fetch)
        resp = await authenticated_client.post(
            "/architecture/tools/conformance", json={"project_id": _MULTI_PROJECT}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["realization_coverage"]["dead_bindings"] == []
        assert [
            (e["container"], e["component_id"], e["status"])
            for e in body["realization_coverage"]["insufficient_evidence"]
        ] == [("zeta", "z.one", "insufficient_evidence")]
        assert body["summary"]["insufficient_evidence_containers"] == ["zeta"]
