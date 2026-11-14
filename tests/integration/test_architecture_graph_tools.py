"""Integration coverage for the three GRAPH tools under /architecture/tools.

``impact-analysis``, ``capability-heatmap`` and ``generate-view`` are among the
routes no test in this repo had ever called over HTTP. Each of the three shared
one defect shape — a **200 carrying a confident lie**:

* ``impact-analysis`` answered an unknown, stale or deleted ``element_id`` with
  ``{"source": {"title": "unknown"}, "affected": []}`` and HTTP 200, i.e. "this
  change is safe". It now 404s (the check lives in the ROUTER; the pure
  ``analyze_impact`` sentinel is pinned by ``tests/test_architecture.py``).
* ``capability-heatmap`` accepted any string as a dimension, because
  ``build_capability_heatmap`` has exactly three ``if "<dim>" in dims`` branches
  and no else. ``["bogus"]`` scored nothing and returned 200. The vocabulary is
  now closed in ``schemas/tools.py``.
* ``generate-view`` returns an empty view for a viewpoint absent from
  ``VIEWPOINT_FILTERS``; the Literal makes that unreachable today, and
  ``tests/test_viewpoint_filters_match_the_literal.py`` keeps it so.

⚠️ THE VACUITY TRAP THESE TESTS ARE WRITTEN AGAINST. ``_load_elements``
(``routers/architecture_tools.py``) filters ``tenant_id == … AND (project_id IS
NULL OR project_id == requested)``. Post a project_id the fixture did not seed
and the endpoint filters the whole fixture away, answers ``{"affected": []}`` /
an empty view with 200, and a shape-only test passes while measuring nothing.
So every assertion below names POSITIVE content that was seeded by id. A
filtered-away fixture FAILS here instead of passing.

⛔ A skip here is a defect, not a convenience: ``tests/integration/`` bans
``pytest.skip`` outright and rewrites any skip into a failure. Assert on the
status and the body instead.
"""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

TOOLS = "/architecture/tools"


def _uid():
    return uuid4().hex[:8]


def _make_element(req_client, project_id, layer, element_type, title=None):
    r = req_client.post(
        "/architecture/elements",
        json={
            "title": title or f"graph-{element_type}-{_uid()}",
            "layer": layer,
            "element_type": element_type,
            "project_id": project_id,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_relationship(req_client, project_id, source_id, target_id, relationship_type):
    r = req_client.post(
        "/architecture/relationships",
        json={
            "source_entity_type": "architecture_elements",
            "source_id": source_id,
            "target_entity_type": "architecture_elements",
            "target_id": target_id,
            "relationship_type": relationship_type,
            "project_id": project_id,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.fixture
def traceable_project(req_client):
    """A small project: 3 application elements chained to 1 technology element.

    Same shape as ``test_architecture_tools.py``'s fixture of the same name, so
    the two files describe one model. Chain is ids[0] -> ids[1] -> ids[2] ->
    ids[3], each edge an ``association``, so a BFS from ids[0] reaches ids[1] at
    distance 1 and ids[2] at distance 2.

    Yields ``(project_id, element_ids)`` and deletes everything afterwards.
    """
    project_id = f"itest-graph-{_uid()}"
    ids = [
        _make_element(req_client, project_id, layer, etype)
        for layer, etype in [
            ("application", "application_component"),
            ("application", "application_service"),
            ("application", "application_function"),
            ("technology", "node"),
        ]
    ]

    rel_ids = [
        _make_relationship(req_client, project_id, ids[src], ids[tgt], "association")
        for src, tgt in [(0, 1), (1, 2), (2, 3)]
    ]

    yield project_id, ids

    for rid in rel_ids:
        req_client.delete(f"/architecture/relationships/{rid}")
    for eid in ids:
        req_client.delete(f"/architecture/elements/{eid}")


@pytest.fixture
def capability_project(req_client):
    """One capability wired to everything the three scoring dimensions read.

    ``build_capability_heatmap`` scores a capability from its neighbours:
    ``maturity`` counts ``realization`` edges to application/technology types,
    ``strategic_importance`` counts goal/outcome neighbours, ``investment_need``
    counts ``gap`` neighbours against maturity. Seeding one of each makes every
    dimension produce a value a caller could act on, so "the key is present" is
    not the only thing under test.

    Yields ``(project_id, capability_id)``.
    """
    project_id = f"itest-heat-{_uid()}"
    capability_id = _make_element(req_client, project_id, "strategy", "capability")
    component_id = _make_element(req_client, project_id, "application", "application_component")
    goal_id = _make_element(req_client, project_id, "motivation", "goal")
    gap_id = _make_element(req_client, project_id, "implementation_migration", "gap")

    rel_ids = [
        _make_relationship(req_client, project_id, component_id, capability_id, "realization"),
        _make_relationship(req_client, project_id, capability_id, goal_id, "association"),
        _make_relationship(req_client, project_id, capability_id, gap_id, "association"),
    ]

    yield project_id, capability_id

    for rid in rel_ids:
        req_client.delete(f"/architecture/relationships/{rid}")
    for eid in (capability_id, component_id, goal_id, gap_id):
        req_client.delete(f"/architecture/elements/{eid}")


def _scores_for(body, capability_id):
    """The seeded capability's own score dict, asserted present.

    Reading ``averages`` instead would be the vacuity trap: it is populated from
    ``dims`` even when ZERO capabilities were loaded, so an empty result still
    carries all three keys. Only the per-capability entry proves the fixture was
    in scope.
    """
    entry = next(
        (c for c in body["capabilities"] if c["element"]["id"] == capability_id),
        None,
    )
    assert entry is not None, (
        f"seeded capability {capability_id} absent from "
        f"{[c['element']['id'] for c in body['capabilities']]} — the fixture was "
        f"filtered out of scope, so nothing below would be measuring the tool"
    )
    return entry["scores"]


class TestImpactAnalysis:
    def test_a_chained_element_two_hops_away_is_reported_as_affected(
        self, req_client, traceable_project
    ) -> None:
        """The whole point of the tool: transitive reach, named by id."""
        project_id, ids = traceable_project
        r = req_client.post(
            f"{TOOLS}/impact-analysis",
            json={"project_id": project_id, "element_id": ids[0], "max_depth": 3},
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["source"]["id"] == ids[0]
        assert body["source"]["title"] != "unknown", (
            "the tool's unknown-element sentinel reached the wire on a KNOWN id"
        )

        by_id = {a["element"]["id"]: a for a in body["affected"]}
        assert ids[1] in by_id, f"direct neighbour missing from {sorted(by_id)}"
        assert ids[2] in by_id, f"depth-2 element missing from {sorted(by_id)}"
        assert by_id[ids[1]]["distance"] == 1
        assert by_id[ids[2]]["distance"] == 2

    def test_max_depth_one_excludes_the_depth_two_element(
        self, req_client, traceable_project
    ) -> None:
        """The bound must actually bind — and must not empty the result.

        Asserting only the exclusion would pass against ``affected == []``,
        which is the failure mode this whole file exists to catch. So the
        direct neighbour is asserted PRESENT in the same breath.
        """
        project_id, ids = traceable_project
        r = req_client.post(
            f"{TOOLS}/impact-analysis",
            json={"project_id": project_id, "element_id": ids[0], "max_depth": 1},
        )
        assert r.status_code == 200, r.text
        reached = {a["element"]["id"] for a in r.json()["affected"]}

        assert ids[1] in reached, f"depth-1 neighbour missing from {sorted(reached)}"
        assert ids[2] not in reached, "max_depth=1 still returned the depth-2 element"
        assert all(a["distance"] <= 1 for a in r.json()["affected"])

    def test_an_unknown_element_id_is_404_not_an_empty_200(
        self, req_client, traceable_project
    ) -> None:
        """KILLING MUTATION: delete the element-existence check in
        ``impact_analysis_endpoint`` -> 200 with ``affected: []`` -> RED.

        A random UUID is the stale/deleted-id case. Answering it 200 tells the
        caller their change affects nothing, which is the most expensive
        possible wrong answer from an impact tool.
        """
        project_id, _ = traceable_project
        ghost = str(uuid4())
        r = req_client.post(
            f"{TOOLS}/impact-analysis",
            json={"project_id": project_id, "element_id": ghost, "max_depth": 3},
        )
        assert r.status_code == 404, r.text
        message = r.json()["error"]["message"]
        assert ghost in message, message

    def test_an_element_in_another_project_of_the_same_tenant_is_out_of_scope(
        self, req_client, traceable_project
    ) -> None:
        """The deliberate semantics of the 404, pinned rather than assumed.

        ``_load_elements`` filters ``project_id IS NULL OR project_id ==
        requested``, so an element in a SIBLING project of the same tenant is
        absent from the loaded set while existing perfectly well. This case must
        still refuse — an empty ``affected`` would be just as much a lie — but
        the message must say SCOPE, not "does not exist", or the operator goes
        hunting for a row that is sitting one project away.
        """
        this_project, _ = traceable_project
        other_project = f"itest-graph-other-{_uid()}"
        outsider = _make_element(req_client, other_project, "application", "application_component")
        try:
            # Control: the element really does exist and really is reachable in
            # its OWN project. Without this leg the 404 below could equally mean
            # "creation failed", and the test would pass for the wrong reason.
            own = req_client.post(
                f"{TOOLS}/impact-analysis",
                json={"project_id": other_project, "element_id": outsider},
            )
            assert own.status_code == 200, own.text
            assert own.json()["source"]["id"] == outsider

            r = req_client.post(
                f"{TOOLS}/impact-analysis",
                json={"project_id": this_project, "element_id": outsider},
            )
            assert r.status_code == 404, r.text
            message = r.json()["error"]["message"]
            assert "scope" in message.lower(), (
                f"the 404 must name the SCOPE, not merely deny existence: {message!r}"
            )
            assert this_project in message, message
        finally:
            req_client.delete(f"/architecture/elements/{outsider}")


class TestCapabilityHeatmap:
    def test_omitting_dimensions_scores_all_three(self, req_client, capability_project) -> None:
        project_id, capability_id = capability_project
        r = req_client.post(f"{TOOLS}/capability-heatmap", json={"project_id": project_id})
        assert r.status_code == 200, r.text
        scores = _scores_for(r.json(), capability_id)

        assert set(scores) == {"maturity", "strategic_importance", "investment_need"}
        # The seeded neighbours must actually move the numbers, otherwise "all
        # three keys present" would also hold for a capability wired to nothing.
        assert scores["maturity"] == 1.0, scores
        assert scores["strategic_importance"] == 1.0, scores

    def test_a_single_named_dimension_scores_only_that_one(
        self, req_client, capability_project
    ) -> None:
        """KILLING MUTATION: make ``build_capability_heatmap`` ignore ``dims``
        and always score three -> RED."""
        project_id, capability_id = capability_project
        r = req_client.post(
            f"{TOOLS}/capability-heatmap",
            json={"project_id": project_id, "dimensions": ["maturity"]},
        )
        assert r.status_code == 200, r.text
        scores = _scores_for(r.json(), capability_id)

        assert set(scores) == {"maturity"}, scores
        assert r.json()["averages"].keys() == {"maturity"}

    def test_an_unknown_dimension_is_422_not_an_empty_200(
        self, req_client, capability_project
    ) -> None:
        """KILLING MUTATION: revert ``dimensions`` to ``list[str] | None`` ->
        200 with an empty ``scores`` dict per capability -> RED.

        Empty scores do not mean "nothing scored well"; they mean the dimension
        does not exist. A 200 cannot say that, so the vocabulary is closed at
        the request boundary instead.
        """
        project_id, _ = capability_project
        r = req_client.post(
            f"{TOOLS}/capability-heatmap",
            json={"project_id": project_id, "dimensions": ["bogus"]},
        )
        assert r.status_code == 422, r.text
        assert "dimensions" in r.text, r.text

    def test_one_bad_dimension_rejects_the_whole_request(
        self, req_client, capability_project
    ) -> None:
        """A typo alongside two good values must not be silently dropped."""
        project_id, _ = capability_project
        r = req_client.post(
            f"{TOOLS}/capability-heatmap",
            json={"project_id": project_id, "dimensions": ["maturity", "maturaty"]},
        )
        assert r.status_code == 422, r.text

    def test_an_empty_list_still_means_the_defaults(self, req_client, capability_project) -> None:
        """Unchanged behaviour: ``dims = dimensions or DEFAULT_DIMENSIONS``.

        Closing the vocabulary must not turn ``[]`` into a 422 or into a
        capability with no scores — both are behaviour changes for existing
        callers that the fix never intended.
        """
        project_id, capability_id = capability_project
        r = req_client.post(
            f"{TOOLS}/capability-heatmap",
            json={"project_id": project_id, "dimensions": []},
        )
        assert r.status_code == 200, r.text
        scores = _scores_for(r.json(), capability_id)
        assert set(scores) == {"maturity", "strategic_importance", "investment_need"}

    def test_a_mis_cased_dimension_is_normalised_not_rejected(
        self, req_client, capability_project
    ) -> None:
        """KILLING MUTATION: drop ``_normalise_dimensions`` -> ``"Maturity"``
        422s -> RED.

        The AA conveyor forwards ``$json.step.parameters.*`` verbatim, so an
        LLM-authored ``"Maturity"`` must land the same way ``viewpoint=
        "Layered"`` does on the sibling endpoint. Closing a vocabulary must not
        make it case-sensitive where its twin is not.
        """
        project_id, capability_id = capability_project
        r = req_client.post(
            f"{TOOLS}/capability-heatmap",
            json={"project_id": project_id, "dimensions": ["  Maturity "]},
        )
        assert r.status_code == 200, r.text
        assert set(_scores_for(r.json(), capability_id)) == {"maturity"}


class TestGenerateView:
    def test_a_viewpoint_selects_exactly_the_application_elements(
        self, req_client, traceable_project
    ) -> None:
        """``application_cooperation`` filters ``layers: ["application"]``.

        Exact set equality, not a subset: the technology ``node`` must be
        EXCLUDED, and the three application elements must all be present. A
        subset assertion would pass against an empty view.
        """
        project_id, ids = traceable_project
        r = req_client.post(
            f"{TOOLS}/generate-view",
            json={"project_id": project_id, "viewpoint": "application_cooperation"},
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["viewpoint"] == "application_cooperation"
        selected = set(body["element_ids"])
        assert selected == {ids[0], ids[1], ids[2]}, (
            f"expected exactly the three application elements, got {sorted(selected)}"
        )
        assert ids[3] not in selected, "the technology element leaked into an application view"
        assert body["element_count"] == len(body["element_ids"])

    def test_the_technology_viewpoint_selects_the_technology_element_instead(
        self, req_client, traceable_project
    ) -> None:
        """The complement of the test above — a filter that only ever returned
        application elements would pass one of these and fail the other."""
        project_id, ids = traceable_project
        r = req_client.post(
            f"{TOOLS}/generate-view",
            json={"project_id": project_id, "viewpoint": "technology"},
        )
        assert r.status_code == 200, r.text
        selected = set(r.json()["element_ids"])

        assert selected == {ids[3]}, f"expected only the technology node, got {sorted(selected)}"

    def test_an_unknown_viewpoint_is_422_not_an_empty_view(
        self, req_client, traceable_project
    ) -> None:
        """The Literal is what keeps ``generate_view``'s empty-filter branch
        unreachable; ``tests/test_viewpoint_filters_match_the_literal.py`` keeps
        the Literal and the filter table in step."""
        project_id, _ = traceable_project
        r = req_client.post(
            f"{TOOLS}/generate-view",
            json={"project_id": project_id, "viewpoint": "not_a_real_viewpoint"},
        )
        assert r.status_code == 422, r.text
        assert "viewpoint" in r.text
